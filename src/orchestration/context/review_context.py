"""Bounded, structured review context gathering for the adversarial reviewer loop."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.config import get_settings
from src.domain.interfaces import IASTParser, ICodeSearcher
from src.domain.interfaces import IPreflightService
from src.domain.schemas import CodeEntity, FocusedContextRequest, FocusedContextResult, ReviewTask, SearchResult
from src.domain.state import GraphState
from src.infrastructure.factory import build_ast_parser, build_cache_service
from src.infrastructure.sandbox import RepoSandbox
from src.infrastructure.search.ripgrep import RipgrepSearcher
from src.orchestration.nodes.application.worker import ReviewTaskContext

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

MAX_FILES_PER_REQUEST = 5
MAX_TEXT_QUERIES = 5
MAX_SYMBOL_QUERIES = 5
MAX_SEARCH_RESULTS_PER_QUERY = 15
MAX_FILE_SLICE_CHARS = 8000
MAX_TOTAL_RESULT_CHARS = 24000
MAX_NEIGHBOR_NODES = 12


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def structural_neighbor_summary(state: GraphState, file_path: str) -> str:
    """Summarize 1-hop structural graph neighbors for a file node."""
    graph_payload = state.get("structural_graph_node_link") or {}
    if not isinstance(graph_payload, dict):
        return ""
    nodes = graph_payload.get("nodes", [])
    edges = graph_payload.get("edges", [])
    file_to_id: Dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("node_type") == "file" and isinstance(node.get("file_path"), str):
            file_to_id[node["file_path"]] = str(node.get("id", ""))
    node_id = file_to_id.get(file_path)
    if not node_id:
        return ""
    neighbor_ids: Set[str] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        src, tgt = edge.get("source"), edge.get("target")
        if src == node_id and isinstance(tgt, str):
            neighbor_ids.add(tgt)
        if tgt == node_id and isinstance(src, str):
            neighbor_ids.add(src)
    id_to_node = {str(n.get("id", "")): n for n in nodes if isinstance(n, dict) and n.get("id")}
    lines: List[str] = []
    for other_id in list(neighbor_ids)[:MAX_NEIGHBOR_NODES]:
        node = id_to_node.get(other_id, {})
        label = node.get("label") or node.get("name") or other_id
        ntype = node.get("node_type", "")
        fp = node.get("file_path", "")
        lines.append(f"{label} ({ntype}{f', {fp}' if fp else ''})")
    return "; ".join(lines)


class LazyReviewContextProvider:
    """Shared direct-context adapter for reviewer graph runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sandbox: Optional[RepoSandbox] = None
        self._searcher: Optional[ICodeSearcher] = None
        self._ast_parser: Optional[IASTParser] = None
        self._host_repo_path: Optional[str] = None
        self._startup_warnings: List[str] = []

    def collect_for_task(self, state: GraphState, task: ReviewTask) -> ReviewTaskContext:
        self._ensure_started(state)
        warnings = list(self._startup_warnings)
        explored_files: List[str] = []
        file_snippets: Dict[str, str] = {}
        entities_by_file: Dict[str, List[CodeEntity]] = {}
        search_results: Dict[str, List[SearchResult]] = {}

        for file_path in task.target_files[:12]:
            snippet = self.read_file_slice(file_path, max_chars=20000)
            if snippet:
                file_snippets[file_path] = snippet
                explored_files.append(file_path)
            if self._ast_parser is not None and self._host_repo_path:
                try:
                    entities_by_file[file_path] = self._ast_parser.get_file_structure(
                        repository_path=self._host_repo_path,
                        file_path=file_path,
                    )
                except Exception as exc:  # noqa: BLE001 - AST is enrichment only
                    warnings.append(f"ast_failed:{file_path}:{exc.__class__.__name__}: {exc}")

        for query in self._queries_for_task(task=task, entities_by_file=entities_by_file):
            if self._searcher is None:
                warnings.append("search_unavailable")
                break
            try:
                search_results[query] = self._searcher.search_text(query=query, repository_path="/repo")[:40]
            except Exception as exc:  # noqa: BLE001 - search is enrichment only
                warnings.append(f"search_failed:{query}:{exc.__class__.__name__}: {exc}")

        return ReviewTaskContext(
            explored_files=sorted(set(explored_files)),
            file_snippets=file_snippets,
            entities_by_file=entities_by_file,
            search_results=search_results,
            warnings=warnings,
        )

    def stop(self) -> None:
        with self._lock:
            if self._sandbox is not None:
                self._sandbox.stop()
                self._sandbox = None

    def get_sandbox(self, state: GraphState) -> RepoSandbox:
        self._ensure_started(state)
        if self._sandbox is None:
            raise RuntimeError("Review sandbox is unavailable.")
        return self._sandbox

    def read_file_slice(self, file_path: str, *, max_chars: int = 20000) -> str:
        """Read a bounded prefix of a repository-relative file path."""
        return self._read_file(file_path)[:max_chars]

    def search_bounded(self, query: str, *, max_hits: int) -> List[SearchResult]:
        """Run a single bounded text search when the sandbox searcher is available."""
        if self._searcher is None:
            return []
        try:
            return self._searcher.search_text(query=query, repository_path="/repo")[:max_hits]
        except Exception as exc:  # noqa: BLE001
            logger.warning("bounded search failed query=%r reason=%s", query, exc)
            return []

    def ast_entities_for_file(self, file_path: str) -> tuple[List[CodeEntity], List[str]]:
        """Return AST entity summaries for one file plus warnings."""
        warnings: List[str] = []
        if self._ast_parser is None or not self._host_repo_path:
            return [], warnings
        try:
            return (
                self._ast_parser.get_file_structure(
                    repository_path=self._host_repo_path,
                    file_path=file_path,
                ),
                warnings,
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"ast_failed:{file_path}:{exc.__class__.__name__}: {exc}")
            return [], warnings

    def _ensure_started(self, state: GraphState) -> None:
        if self._searcher is not None:
            return

        with self._lock:
            if self._searcher is not None:
                return

            settings = get_settings()
            repo_path = str(state.get("repo_path", "") or "")
            metadata = state.get("metadata", {}) or {}
            sandbox = RepoSandbox()

            try:
                if Path(repo_path).is_dir():
                    self._host_repo_path = str(Path(repo_path).resolve())
                    sandbox.start(self._host_repo_path)
                    if settings.ast_enabled:
                        try:
                            self._ast_parser = build_ast_parser(
                                settings=settings,
                                cache=build_cache_service(),
                            )
                        except Exception as exc:
                            if not settings.ast_fallback_to_search:
                                raise
                            self._startup_warnings.append(
                                f"ast_startup_failed:{exc.__class__.__name__}: {exc}"
                            )
                else:
                    repo_url = str(metadata.get("review_repo_url") or repo_path)
                    checkout_ref = str(metadata.get("review_checkout_ref") or "")
                    pr_number = metadata.get("pr_number") or metadata.get("review_pr_number")
                    if not checkout_ref and pr_number:
                        checkout_ref = f"pull/{pr_number}/head"
                    if not checkout_ref:
                        checkout_ref = "HEAD"
                        self._startup_warnings.append(
                            "remote_checkout_ref_missing:reviewing repository default HEAD"
                        )
                    sandbox.start_from_remote_ref(repo_url=repo_url, ref=checkout_ref)
                    self._host_repo_path = None
                    if metadata.get("review_trace_enabled"):
                        trace_logger.info(
                            "TRACE context_ast_unavailable run_id=%s reason=remote_sandbox repo_url=%s ref=%s",
                            state.get("run_id", "unknown"),
                            repo_url,
                            checkout_ref,
                        )

                self._sandbox = sandbox
                self._searcher = RipgrepSearcher(sandbox=sandbox)
            except Exception as exc:
                sandbox.stop()
                self._startup_warnings.append(
                    f"sandbox_startup_failed:{exc.__class__.__name__}: {exc}"
                )
                logger.warning(
                    "review context sandbox unavailable run_id=%s reason=%s: %s",
                    state.get("run_id", "unknown"),
                    exc.__class__.__name__,
                    exc,
                )

    def _read_file(self, file_path: str) -> str:
        if self._sandbox is not None:
            script = "sed -n '1,220p' \"$1\""
            return self._sandbox.execute(["sh", "-lc", script, "read-file", file_path], workdir="/repo")[:20000]

        if self._host_repo_path is None:
            return ""

        repo_root = Path(self._host_repo_path).resolve()
        target = (repo_root / file_path).resolve()
        try:
            target.relative_to(repo_root)
        except ValueError:
            return ""
        if not target.is_file():
            return ""
        return target.read_text(encoding="utf-8", errors="replace")[:20000]

    @staticmethod
    def _queries_for_task(
        task: ReviewTask,
        entities_by_file: Dict[str, List[CodeEntity]],
    ) -> List[str]:
        entity_names = [
            entity.name
            for entities in entities_by_file.values()
            for entity in entities[:8]
            if entity.name and "@" not in entity.name
        ]
        specialty_queries = {
            "security": [
                "password|secret|token|credential|auth|permission|eval|exec|pickle|subprocess",
                "sql|query|deserialize|jwt|cookie|session|csrf|cors",
            ],
            "logic": ["TODO|FIXME|raise|except|return None|default|fallback"],
            "performance": ["for .* in .*for|sleep|cache|memo|batch|bulk|timeout|retry"],
            "general": ["test|error|warning|deprecated|compat|migration"],
        }
        queries = specialty_queries.get(task.specialty, specialty_queries["general"]) + entity_names
        deduped: List[str] = []
        seen: set[str] = set()
        for query in queries:
            if query in seen:
                continue
            seen.add(query)
            deduped.append(query)
        return deduped[:12]


class BoundedReviewContextFulfiller:
    """Fulfill structured focused-context requests with hard caps (no arbitrary shell)."""

    def __init__(self, provider: LazyReviewContextProvider) -> None:
        self._provider = provider

    def fulfill(
        self,
        state: GraphState,
        request: FocusedContextRequest,
        *,
        existing_result: FocusedContextResult | None = None,
    ) -> FocusedContextResult:
        """Build a capped FocusedContextResult for one request."""
        if existing_result is not None:
            return existing_result

        self._provider._ensure_started(state)  # noqa: SLF001 - intentional coupling
        warnings: List[str] = []
        file_snippets: Dict[str, str] = {}
        search_hits: Dict[str, List[SearchResult]] = {}
        total_chars = 0

        file_paths = request.file_paths[:MAX_FILES_PER_REQUEST]
        for fp in file_paths:
            body = self._provider.read_file_slice(fp, max_chars=MAX_FILE_SLICE_CHARS)
            if body:
                neighbor = structural_neighbor_summary(state, fp)
                if neighbor:
                    body = f"{body}\n--- structural neighbors ---\n{neighbor}"
                file_snippets[fp] = body
                total_chars += len(body)
            entities, ast_warnings = self._provider.ast_entities_for_file(fp)
            warnings.extend(ast_warnings)
            if entities:
                lines = [f"- {e.type} {e.name}: {e.signature}" for e in entities[:24]]
                ast_block = "\n".join(lines)
                merged = file_snippets.get(fp, "")
                merged = f"{merged}\n--- ast entities ---\n{ast_block}" if merged else ast_block
                file_snippets[fp] = merged[:MAX_FILE_SLICE_CHARS]
                total_chars = sum(len(v) for v in file_snippets.values())

        for sym in request.symbol_queries[:MAX_SYMBOL_QUERIES]:
            hits = self._provider.search_bounded(sym, max_hits=MAX_SEARCH_RESULTS_PER_QUERY)
            search_hits[sym] = hits
            total_chars += sum(len(h.content) for h in hits)
            if total_chars > MAX_TOTAL_RESULT_CHARS:
                warnings.append("truncated_total_chars")
                break

        for tq in request.text_queries[:MAX_TEXT_QUERIES]:
            hits = self._provider.search_bounded(tq, max_hits=MAX_SEARCH_RESULTS_PER_QUERY)
            search_hits[tq] = hits
            total_chars += sum(len(h.content) for h in hits)
            if total_chars > MAX_TOTAL_RESULT_CHARS:
                warnings.append("truncated_total_chars")
                break

        result = FocusedContextResult(
            request_id=request.request_id,
            candidate_id=request.candidate_id,
            file_snippets=file_snippets,
            search_hits=search_hits,
            warnings=warnings,
        )
        if _trace_enabled(state):
            trace_logger.info(
                "TRACE focused_context_fulfilled run_id=%s request_id=%s files=%s queries=%s",
                state.get("run_id", "unknown"),
                request.request_id,
                list(file_snippets),
                list(search_hits),
            )
        return result
