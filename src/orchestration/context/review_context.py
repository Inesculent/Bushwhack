"""Bounded, structured review context gathering for the adversarial reviewer loop."""

from __future__ import annotations

import logging
import re
import threading
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from src.config import get_settings
from src.domain.interfaces import IASTParser, ICodeSearcher, IGitHubContextProvider
from src.domain.schemas import (
    CandidateFinding,
    CodeEntity,
    FocusedContextRequest,
    FocusedContextResult,
    RepoDocument,
    RepoDocsBundle,
    ReviewTask,
    SearchResult,
    StructuralTopologySummary,
)
from src.domain.state import GraphState
from src.infrastructure.sandbox import RepoSandbox, build_repo_sandbox
from src.infrastructure.sandbox_ast import collect_sandbox_file_entities, entities_from_sandbox_payload
from src.infrastructure.search.ripgrep import RipgrepSearcher
from src.orchestration.context.focused_query_sanitize import sanitize_focused_context_request
from src.tools.review_kb_tools import query_repository_kb
from src.orchestration.nodes.application.worker import ReviewTaskContext

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

MAX_FILES_PER_REQUEST = 5
MAX_TEXT_QUERIES = 5
MAX_SYMBOL_QUERIES = 5
MAX_SEARCH_RESULTS_PER_QUERY = 15
MAX_FILE_SLICE_CHARS = 16000
MAX_TOTAL_RESULT_CHARS = 48000
MAX_NEIGHBOR_NODES = 12
MAX_ENTITIES_FROM_GRAPH_PER_FILE = 48

MAX_CRITIQUER_STRUCT_CONTEXT_FILES = 8
MAX_CRITIQUER_STRUCT_NEIGHBORS = 8
MAX_CRITIQUER_COMMUNITY_PEER_FILES = 6
MAX_CRITIQUER_STRUCT_CONTEXT_CHARS = 4000


def _normalize_repo_path(path: str) -> str:
    return path.strip().replace("\\", "/")


def _parse_structural_topology(state: GraphState) -> Optional[StructuralTopologySummary]:
    raw = state.get("structural_topology")
    if raw is None:
        return None
    if isinstance(raw, StructuralTopologySummary):
        return raw
    if isinstance(raw, dict):
        try:
            return StructuralTopologySummary.model_validate(raw)
        except Exception:
            return None
    return None


def _ast_included_paths_normalized(metadata: Dict[str, Any]) -> Set[str]:
    raw = metadata.get("ast_included_files") or []
    if not isinstance(raw, list):
        return set()
    return {_normalize_repo_path(str(p)) for p in raw if isinstance(p, str) and str(p).strip()}


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
            fp = node["file_path"]
            nid = str(node.get("id", ""))
            if nid:
                file_to_id[fp] = nid
                file_to_id[_normalize_repo_path(fp)] = nid
    norm = _normalize_repo_path(file_path)
    node_id = file_to_id.get(file_path) or file_to_id.get(norm)
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


def structural_critiquer_context_excerpt(state: GraphState, target_files: Sequence[str]) -> str:
    """Bounded structural + topology excerpt for the general critiquer (no live sandbox required)."""
    graph_payload = state.get("structural_graph_node_link") or {}
    if not isinstance(graph_payload, dict):
        return ""
    nodes = graph_payload.get("nodes", [])
    edges = graph_payload.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list) or not nodes:
        return ""

    id_to_node = {str(n.get("id", "")): n for n in nodes if isinstance(n, dict) and n.get("id")}
    path_to_id: Dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("node_type") == "file" and isinstance(node.get("file_path"), str):
            fp = node["file_path"]
            nid = str(node.get("id", ""))
            if nid:
                path_to_id[fp] = nid
                path_to_id[_normalize_repo_path(fp)] = nid

    topology = _parse_structural_topology(state)

    deduped: List[str] = []
    seen: Set[str] = set()
    for raw in target_files:
        if not isinstance(raw, str) or not raw.strip():
            continue
        norm = _normalize_repo_path(raw)
        if norm in seen:
            continue
        seen.add(norm)
        deduped.append(norm)
    deduped = deduped[:MAX_CRITIQUER_STRUCT_CONTEXT_FILES]

    blocks: List[str] = []
    matched_any = False
    for norm_fp in deduped:
        node_id = path_to_id.get(norm_fp)
        if not node_id:
            continue
        matched_any = True

        rows: List[tuple[str, str]] = []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            src = str(edge.get("source", ""))
            tgt = str(edge.get("target", ""))
            et = str(edge.get("edge_type") or edge.get("relation") or "?")
            if src == node_id and tgt:
                rows.append((tgt, et))
            elif tgt == node_id and src:
                rows.append((src, et))
        rows.sort(key=lambda t: (t[1], t[0]))
        neighbor_lines: List[str] = []
        seen_other: Set[str] = set()
        for other_id, et in rows:
            if other_id in seen_other:
                continue
            seen_other.add(other_id)
            on = id_to_node.get(other_id, {})
            label = on.get("label") or on.get("name") or on.get("symbol_name") or other_id
            ntype = on.get("node_type", "")
            ofp = on.get("file_path", "")
            tail = f", {ofp}" if isinstance(ofp, str) and ofp else ""
            neighbor_lines.append(f"  - {label} ({ntype}{tail}) [{et}]")
            if len(neighbor_lines) >= MAX_CRITIQUER_STRUCT_NEIGHBORS:
                break

        cid: int | None = None
        if topology is not None:
            cid = topology.node_to_community.get(f"file:{norm_fp}")
        if cid is None:
            fn = id_to_node.get(node_id, {})
            raw_c = fn.get("community_id")
            if isinstance(raw_c, int):
                cid = raw_c
            elif raw_c is not None:
                try:
                    cid = int(raw_c)
                except (TypeError, ValueError):
                    cid = None

        peer_files: List[str] = []
        if cid is not None and cid >= 0:
            peer_set: Set[str] = set()
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                if n.get("node_type") != "file":
                    continue
                ofp = n.get("file_path")
                if not isinstance(ofp, str) or not ofp:
                    continue
                onorm = _normalize_repo_path(ofp)
                if onorm == norm_fp:
                    continue
                oid = str(n.get("id", ""))
                pcid: int | None = None
                if topology is not None and oid:
                    raw_pc = topology.node_to_community.get(oid)
                    if isinstance(raw_pc, int):
                        pcid = raw_pc
                if pcid is None:
                    rc = n.get("community_id")
                    if isinstance(rc, int):
                        pcid = rc
                    elif rc is not None:
                        try:
                            pcid = int(rc)
                        except (TypeError, ValueError):
                            pcid = None
                if pcid == cid:
                    peer_set.add(onorm)
            peer_files = sorted(peer_set)[:MAX_CRITIQUER_COMMUNITY_PEER_FILES]

        block = [f"- {norm_fp}"]
        if neighbor_lines:
            block.append("  neighbors (1-hop):")
            block.extend(neighbor_lines)
        else:
            block.append("  neighbors (1-hop): (none)")
        if peer_files:
            block.append(f"  community_peers (community_id={cid}):")
            for pf in peer_files:
                block.append(f"  - {pf}")
        blocks.append("\n".join(block))

    if not matched_any:
        return ""
    header = (
        "Structural context (bounded, 1-hop neighbors + same-community files if available):"
    )
    body = "\n".join([header, *blocks])
    return body[:MAX_CRITIQUER_STRUCT_CONTEXT_CHARS]


def entities_for_file_from_structural_graph(state: GraphState, file_path: str) -> List[CodeEntity]:
    """Build CodeEntity outlines from persisted structural graph (snapshot-safe, no live AST).

    Uses symbol nodes scoped to ``file_path`` plus ``imports`` edges to module nodes.
    Bodies are empty; suitable for the same prompts as lightweight AST outlines.
    """
    graph_payload = state.get("structural_graph_node_link") or {}
    if not isinstance(graph_payload, dict):
        return []
    nodes = graph_payload.get("nodes", [])
    edges = graph_payload.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return []

    norm = _normalize_repo_path(file_path)
    symbol_rows: List[tuple[str, Dict[str, Any]]] = []
    for node in nodes:
        if not isinstance(node, dict) or node.get("node_type") != "symbol":
            continue
        fp = node.get("file_path")
        if not isinstance(fp, str):
            continue
        if _normalize_repo_path(fp) != norm:
            continue
        sid = str(node.get("id", ""))
        if not sid:
            continue
        name = node.get("symbol_name")
        if not isinstance(name, str) or not name.strip():
            continue
        symbol_rows.append((sid, node))

    if not symbol_rows:
        return []

    id_to_node = {str(n.get("id", "")): n for n in nodes if isinstance(n, dict) and n.get("id")}
    imports_by_symbol: Dict[str, List[str]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        if str(edge.get("edge_type") or edge.get("relation") or "") != "imports":
            continue
        src = str(edge.get("source", ""))
        tgt = str(edge.get("target", ""))
        if not src or not tgt:
            continue
        mod = id_to_node.get(tgt, {})
        if mod.get("node_type") != "module":
            continue
        mod_name = mod.get("module_name")
        if isinstance(mod_name, str) and mod_name.strip():
            imports_by_symbol.setdefault(src, []).append(mod_name.strip())

    symbol_rows.sort(
        key=lambda row: (str(row[1].get("symbol_name") or ""), str(row[1].get("signature") or ""))
    )
    entities: List[CodeEntity] = []
    for sid, node in symbol_rows:
        raw_deps = imports_by_symbol.get(sid, [])
        deps = sorted(set(raw_deps))[:24]
        entities.append(
            CodeEntity(
                name=str(node.get("symbol_name") or ""),
                type=str(node.get("symbol_type") or "symbol"),
                signature=str(node.get("signature") or ""),
                body="",
                dependencies=deps,
                definition_line=node.get("definition_line"),
                definition_end_line=node.get("definition_end_line"),
            )
        )
        if len(entities) >= MAX_ENTITIES_FROM_GRAPH_PER_FILE:
            break
    return entities


def symbol_call_edges_for_file(state: GraphState, file_path: str) -> Dict[str, List[str]]:
    """Map symbol name -> outgoing call/reference target labels from the structural graph."""
    graph_payload = state.get("structural_graph_node_link") or {}
    if not isinstance(graph_payload, dict):
        return {}
    nodes = graph_payload.get("nodes", [])
    edges = graph_payload.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return {}

    norm = _normalize_repo_path(file_path)
    id_to_node = {str(n.get("id", "")): n for n in nodes if isinstance(n, dict) and n.get("id")}
    symbol_id_to_name: Dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict) or node.get("node_type") != "symbol":
            continue
        fp = node.get("file_path")
        if not isinstance(fp, str) or _normalize_repo_path(fp) != norm:
            continue
        sid = str(node.get("id", ""))
        name = node.get("symbol_name")
        if sid and isinstance(name, str) and name.strip():
            symbol_id_to_name[sid] = name.strip()

    outgoing: Dict[str, List[str]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        et = str(edge.get("edge_type") or edge.get("relation") or "")
        if et not in {"calls", "references"}:
            continue
        src = str(edge.get("source", ""))
        tgt = str(edge.get("target", ""))
        sym_name = symbol_id_to_name.get(src)
        if not sym_name or not tgt:
            continue
        target_node = id_to_node.get(tgt, {})
        label = (
            target_node.get("symbol_name")
            or target_node.get("label")
            or target_node.get("name")
            or tgt
        )
        if isinstance(label, str) and label.strip():
            outgoing.setdefault(sym_name, []).append(label.strip())

    for name in outgoing:
        outgoing[name] = sorted(set(outgoing[name]))[:16]
    return outgoing


class LazyReviewContextProvider:
    """Shared direct-context adapter for reviewer graph runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sandbox: Optional[RepoSandbox] = None
        self._searcher: Optional[ICodeSearcher] = None
        self._ast_parser: Optional[IASTParser] = None
        self._host_repo_path: Optional[str] = None
        self._startup_warnings: List[str] = []

    def collect_for_critique(self, state: GraphState, task: ReviewTask) -> ReviewTaskContext:
        """Task-scoped collection for adversarial critique (no generic repo search for non-security)."""
        return self._collect_task_context(state, task, critique_mode=True)

    def collect_for_task(self, state: GraphState, task: ReviewTask) -> ReviewTaskContext:
        """Full collection for legacy specialist workers."""
        return self._collect_task_context(state, task, critique_mode=False)

    def _collect_task_context(
        self,
        state: GraphState,
        task: ReviewTask,
        *,
        critique_mode: bool,
    ) -> ReviewTaskContext:
        self._ensure_started(state)
        settings = get_settings()
        warnings = list(self._startup_warnings)
        explored_files: List[str] = []
        file_snippets: Dict[str, str] = {}
        entities_by_file: Dict[str, List[CodeEntity]] = {}
        search_results: Dict[str, List[SearchResult]] = {}
        graph_call_edges_by_file: Dict[str, Dict[str, List[str]]] = {}

        target_files = task.target_files[:12]
        single_file_task = len(target_files) == 1
        if single_file_task:
            per_file_snippet_max = min(
                settings.reviewer_critiquer_single_file_max_chars,
                settings.review_full_file_max_chars,
            )
        elif critique_mode:
            packet_budget = int(settings.reviewer_critique_packet_max_chars)
            per_file_snippet_max = min(
                8000,
                max(2000, packet_budget // max(1, len(target_files))),
            )
        else:
            per_file_snippet_max = 5000

        for file_path in target_files:
            if single_file_task:
                snippet = self.read_full_file(
                    file_path,
                    max_chars=per_file_snippet_max,
                )
            else:
                slice_max = per_file_snippet_max if critique_mode else 20000
                snippet = self.read_file_slice(file_path, max_chars=slice_max)
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

        sandbox_ast_files = 0
        if (
            settings.ast_enabled
            and self._host_repo_path is None
            and self._sandbox is not None
        ):
            missing = [fp for fp in target_files if not entities_by_file.get(fp)]
            if missing:
                try:
                    payload = collect_sandbox_file_entities(self._sandbox, missing)
                    sandbox_entities = entities_from_sandbox_payload(payload)
                    for fp, ents in sandbox_entities.items():
                        entities_by_file[fp] = ents
                        sandbox_ast_files += 1
                    for gap in payload.get("gaps") or []:
                        if isinstance(gap, dict):
                            warnings.append(
                                f"sandbox_ast_gap:{gap.get('filepath', '?')}:{gap.get('reason', '?')}"
                            )
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"sandbox_ast_failed:{exc.__class__.__name__}: {exc}")

        outline_from_graph = False
        for file_path in target_files:
            if entities_by_file.get(file_path):
                continue
            graph_entities = entities_for_file_from_structural_graph(state, file_path)
            if graph_entities:
                entities_by_file[file_path] = graph_entities
                outline_from_graph = True
                call_edges = symbol_call_edges_for_file(state, file_path)
                if call_edges:
                    graph_call_edges_by_file[file_path] = call_edges
        if outline_from_graph:
            warnings.append("review_outline_source:structural_graph_fallback")

        if self._host_repo_path and self._ast_parser is not None:
            warnings.append("ast_capability:local_enabled")
        elif self._host_repo_path and settings.ast_enabled and self._ast_parser is None:
            warnings.append("ast_capability:local_unavailable")
        elif sandbox_ast_files > 0:
            if sandbox_ast_files < len(target_files):
                warnings.append("ast_capability:sandbox_partial")
            else:
                warnings.append("ast_capability:sandbox_enabled")
        elif self._host_repo_path is None and settings.ast_enabled:
            warnings.append("ast_capability:remote_unavailable")
        elif not settings.ast_enabled:
            warnings.append("ast_capability:disabled")

        if critique_mode:
            queries = self._queries_for_critique(task=task)
        else:
            queries = self._queries_for_task(task=task, entities_by_file=entities_by_file)
        for query in queries:
            if self._searcher is None:
                warnings.append("search_unavailable")
                break
            try:
                search_results[query] = self._searcher.search_text(query=query, repository_path="/repo")[:40]
            except Exception as exc:  # noqa: BLE001 - search is enrichment only
                warnings.append(f"search_failed:{query}:{exc.__class__.__name__}: {exc}")

        ast_included_files = sorted(
            {
                _normalize_repo_path(fp)
                for fp, ents in entities_by_file.items()
                if ents and isinstance(fp, str) and fp.strip()
            }
        )

        return ReviewTaskContext(
            explored_files=sorted(set(explored_files)),
            file_snippets=file_snippets,
            entities_by_file=entities_by_file,
            search_results=search_results,
            warnings=warnings,
            ast_included_files=ast_included_files,
            per_file_snippet_max_chars=per_file_snippet_max,
            graph_call_edges_by_file=graph_call_edges_by_file,
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

    def read_file_window(
        self,
        file_path: str,
        *,
        line_start: int,
        line_end: int,
        padding: int = 40,
        max_chars: int = 20000,
    ) -> str:
        """Read a bounded line window around a repository-relative file range."""
        normalized = file_path.replace("\\", "/").lstrip("/")
        if not normalized or ".." in normalized.split("/"):
            return ""
        try:
            start = max(1, int(line_start) - int(padding))
            end = max(start, int(line_end) + int(padding))
        except (TypeError, ValueError):
            return ""

        if self._sandbox is not None:
            script = f"sed -n '{start},{end}p' \"$1\""
            return self._sandbox.execute(
                ["sh", "-lc", script, "read-window", normalized],
                workdir="/repo",
            )[:max_chars]

        if self._host_repo_path is None:
            return ""

        repo_root = Path(self._host_repo_path).resolve()
        target = (repo_root / normalized).resolve()
        try:
            target.relative_to(repo_root)
        except ValueError:
            return ""
        if not target.is_file():
            return ""
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[start - 1 : end])[:max_chars]

    def read_full_file(self, file_path: str, *, max_chars: int) -> str:
        """Read a repository-relative file up to ``max_chars`` (whole-file reviews)."""
        normalized = file_path.replace("\\", "/").lstrip("/")
        if not normalized or ".." in normalized.split("/"):
            return ""

        if self._sandbox is not None:
            limit = max(1, int(max_chars))
            code = (
                "import pathlib\n"
                f"p = pathlib.Path('/repo') / {normalized!r}\n"
                "print(p.read_text(encoding='utf-8', errors='replace')[:"
                f"{limit}"
                "])\n"
            )
            return self._sandbox.execute(["python", "-c", code], workdir="/repo")

        if self._host_repo_path is None:
            return ""

        repo_root = Path(self._host_repo_path).resolve()
        target = (repo_root / normalized).resolve()
        try:
            target.relative_to(repo_root)
        except ValueError:
            return ""
        if not target.is_file():
            return ""
        return target.read_text(encoding="utf-8", errors="replace")[:max_chars]

    def search_bounded(
        self,
        query: str,
        *,
        max_hits: int,
        file_paths: Sequence[str] | None = None,
    ) -> List[SearchResult]:
        """Run a single bounded text search when the sandbox searcher is available."""
        if self._searcher is None:
            return []
        try:
            return self._searcher.search_text(
                query=query,
                repository_path="/repo",
                file_paths=file_paths,
            )[:max_hits]
        except Exception as exc:  # noqa: BLE001
            logger.warning("bounded search failed query=%r reason=%s", query, exc)
            return []

    def ast_entities_for_file(
        self, file_path: str, *, graph_state: GraphState | None = None
    ) -> tuple[List[CodeEntity], List[str]]:
        """Return AST entity summaries for one file plus warnings.

        When live tree-sitter AST is unavailable (e.g. remote sandbox), optional
        ``graph_state`` supplies symbol outlines from ``structural_graph_node_link``.
        """
        warnings: List[str] = []
        settings = get_settings()
        if self._ast_parser is not None and self._host_repo_path:
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
        if (
            settings.ast_enabled
            and self._host_repo_path is None
            and self._sandbox is not None
        ):
            try:
                payload = collect_sandbox_file_entities(self._sandbox, [file_path])
                sandbox_entities = entities_from_sandbox_payload(payload)
                if sandbox_entities.get(file_path):
                    return sandbox_entities[file_path], warnings
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"sandbox_ast_failed:{file_path}:{exc.__class__.__name__}: {exc}")
        if graph_state is not None:
            graph_entities = entities_for_file_from_structural_graph(graph_state, file_path)
            if graph_entities:
                return graph_entities, warnings
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
            sandbox: RepoSandbox | None = None

            try:
                # Review sandbox: clone or RO-mount at /repo for ripgrep + file reads (image must include git when cloning).
                sandbox = build_repo_sandbox(get_settings())
                if Path(repo_path).is_dir():
                    self._host_repo_path = str(Path(repo_path).resolve())
                    sandbox.start(self._host_repo_path)
                    if settings.ast_enabled:
                        try:
                            from src.infrastructure.factory import (
                                build_ast_parser,
                                build_cache_service,
                            )

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
                if sandbox is not None:
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
    def _queries_for_critique(task: ReviewTask) -> List[str]:
        """Repo search only for security tasks; avoids TODO/FIXME noise on logic paths."""
        if task.specialty != "security":
            return []
        return [
            "password|secret|token|credential|auth|permission|eval|exec|pickle|subprocess",
            "sql|query|deserialize|jwt|cookie|session|csrf|cors",
        ]

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


def _candidate_for_focused_request(state: GraphState, candidate_id: str) -> CandidateFinding | None:
    if not candidate_id:
        return None
    for raw in state.get("candidate_findings", []) or []:
        candidate: CandidateFinding | None = None
        if isinstance(raw, CandidateFinding):
            candidate = raw
        elif isinstance(raw, dict):
            try:
                candidate = CandidateFinding.model_validate(raw)
            except Exception:
                candidate = None
        if candidate is not None and candidate.candidate_id == candidate_id:
            return candidate
    return None


def _candidate_matches_focused_path(candidate: CandidateFinding | None, file_path: str) -> bool:
    if candidate is None:
        return False
    candidate_path = _normalize_repo_path(candidate.file_path).lstrip("/")
    focused_path = _normalize_repo_path(file_path).lstrip("/")
    return bool(
        candidate_path
        and focused_path
        and (candidate_path == focused_path or candidate_path.endswith("/" + focused_path))
    )


class BoundedReviewContextFulfiller:
    """Fulfill structured focused-context requests with hard caps (no arbitrary shell)."""

    def __init__(
        self,
        provider: LazyReviewContextProvider,
        *,
        github_provider: IGitHubContextProvider | None = None,
    ) -> None:
        self._provider = provider
        self._github_provider = github_provider
        # One docs bundle per (owner, repo, ref) per fulfiller instance (focused_context wave).
        self._github_docs_bundle_cache: Dict[tuple[str, str, str], RepoDocsBundle] = {}

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

        request = sanitize_focused_context_request(request)
        self._provider._ensure_started(state)  # noqa: SLF001 - intentional coupling
        warnings: List[str] = []
        file_snippets: Dict[str, str] = {}
        file_contents_full: Dict[str, str] = {}
        search_hits: Dict[str, List[SearchResult]] = {}
        total_chars = 0
        missing_files: List[str] = []

        settings = get_settings()
        max_total_budget = (
            settings.review_full_file_max_total_chars
            if request.file_read_mode == "full"
            else MAX_TOTAL_RESULT_CHARS
        )

        file_paths = request.file_paths[:MAX_FILES_PER_REQUEST]
        kb_topics = [
            t
            for t in [
                request.requested_by_specialty,
                "contract",
                "signature",
                "tensor-shape",
            ]
            if t
        ]
        kb_symbol = request.symbol_queries[0] if request.symbol_queries else None
        kb_query = " ".join(
            [
                request.reason,
                " ".join(request.symbol_queries[:MAX_SYMBOL_QUERIES]),
                " ".join(request.text_queries[:MAX_TEXT_QUERIES]),
            ]
        ).strip()
        if kb_query or file_paths or kb_symbol:
            kb_result = query_repository_kb(
                state=state,
                query=kb_query or "focused context repository contracts",
                path=file_paths[0] if file_paths else None,
                symbol=kb_symbol,
                topics=kb_topics,
                max_results=6,
                caller="focused_context",
            )
            if not kb_result.get("skipped") and str(kb_result.get("answer") or "").strip():
                file_snippets["repository_kb_context"] = str(kb_result["answer"])[:MAX_FILE_SLICE_CHARS]
                total_chars += len(file_snippets["repository_kb_context"])
            elif kb_result.get("skip_reason"):
                warnings.append(f"repository_kb_skipped:{kb_result.get('skip_reason')}")

        ast_done = _ast_included_paths_normalized(state.get("metadata") or {})
        candidate = _candidate_for_focused_request(state, request.candidate_id)
        for fp in file_paths:
            if request.file_read_mode == "full":
                body = self._provider.read_full_file(fp, max_chars=settings.review_full_file_max_chars)
                if body:
                    file_contents_full[fp] = body
                    total_chars += len(body)
                    if total_chars > max_total_budget:
                        warnings.append("truncated_total_chars")
                        break
                neighbor = structural_neighbor_summary(state, fp)
                entities, ast_warnings = self._provider.ast_entities_for_file(fp, graph_state=state)
                warnings.extend(ast_warnings)
                norm_fp = _normalize_repo_path(fp)
                if entities and norm_fp not in ast_done:
                    lines = [f"- {e.type} {e.name}: {e.signature}" for e in entities[:24]]
                    ast_block = "\n".join(lines)
                    snippet = f"--- ast entities ---\n{ast_block}"
                    if neighbor:
                        snippet = f"{snippet}\n--- structural neighbors ---\n{neighbor}"
                    file_snippets[fp] = snippet[:MAX_FILE_SLICE_CHARS]
                    total_chars += len(file_snippets[fp])
                elif neighbor:
                    file_snippets[fp] = f"--- structural neighbors ---\n{neighbor}"[:MAX_FILE_SLICE_CHARS]
                    total_chars += len(file_snippets[fp])
            else:
                body = ""
                read_window = getattr(self._provider, "read_file_window", None)
                if callable(read_window) and _candidate_matches_focused_path(candidate, fp):
                    body = read_window(
                        fp,
                        line_start=int(candidate.line_start),
                        line_end=int(candidate.line_end),
                        max_chars=MAX_FILE_SLICE_CHARS,
                    )
                if not body.strip():
                    body = self._provider.read_file_slice(fp, max_chars=MAX_FILE_SLICE_CHARS)
                if not body.strip():
                    read_full = getattr(self._provider, "read_full_file", None)
                    if callable(read_full):
                        body = read_full(
                            fp,
                            max_chars=min(settings.review_full_file_max_chars, MAX_FILE_SLICE_CHARS * 4),
                        )
                if body:
                    neighbor = structural_neighbor_summary(state, fp)
                    if neighbor:
                        body = f"{body}\n--- structural neighbors ---\n{neighbor}"
                    file_snippets[fp] = body
                    total_chars += len(body)
                entities, ast_warnings = self._provider.ast_entities_for_file(fp, graph_state=state)
                warnings.extend(ast_warnings)
                norm_fp = _normalize_repo_path(fp)
                if entities and norm_fp not in ast_done:
                    lines = [f"- {e.type} {e.name}: {e.signature}" for e in entities[:24]]
                    ast_block = "\n".join(lines)
                    merged = file_snippets.get(fp, "")
                    merged = f"{merged}\n--- ast entities ---\n{ast_block}" if merged else ast_block
                    file_snippets[fp] = merged[:MAX_FILE_SLICE_CHARS]
                    total_chars = sum(len(v) for v in file_snippets.values())

        missing_files = [fp for fp in file_paths if fp not in file_snippets and fp not in file_contents_full]

        focused_paths = request.file_paths[:MAX_FILES_PER_REQUEST] or None

        for sym in request.symbol_queries[:MAX_SYMBOL_QUERIES]:
            hits = self._provider.search_bounded(
                sym,
                max_hits=MAX_SEARCH_RESULTS_PER_QUERY,
                file_paths=focused_paths,
            )
            search_hits[sym] = hits
            total_chars += sum(len(h.content) for h in hits)
            if total_chars > max_total_budget:
                warnings.append("truncated_total_chars")
                break

        for tq in request.text_queries[:MAX_TEXT_QUERIES]:
            hits = self._provider.search_bounded(
                tq,
                max_hits=MAX_SEARCH_RESULTS_PER_QUERY,
                file_paths=focused_paths,
            )
            search_hits[tq] = hits
            total_chars += sum(len(h.content) for h in hits)
            if total_chars > max_total_budget:
                warnings.append("truncated_total_chars")
                break

        try:
            self._apply_github_fallback(
                state=state,
                request=request,
                file_snippets=file_snippets,
                search_hits=search_hits,
                warnings=warnings,
                missing_files=missing_files,
                total_chars=total_chars,
            )
        except Exception as exc:  # noqa: BLE001 - GitHub context is optional enrichment
            warnings.append(f"github_fallback_failed:{exc.__class__.__name__}: {exc}")
            logger.warning(
                "GitHub focused-context fallback failed run_id=%s request_id=%s reason=%s: %s",
                state.get("run_id", "unknown"),
                request.request_id,
                exc.__class__.__name__,
                exc,
            )

        result = FocusedContextResult(
            request_id=request.request_id,
            candidate_id=request.candidate_id,
            file_snippets=file_snippets,
            file_contents_full=file_contents_full,
            search_hits=search_hits,
            warnings=warnings,
        )
        if _trace_enabled(state):
            trace_logger.info(
                "TRACE focused_context_fulfilled run_id=%s request_id=%s files=%s queries=%s",
                state.get("run_id", "unknown"),
                request.request_id,
                list({*file_snippets.keys(), *file_contents_full.keys()}),
                list(search_hits),
            )
        return result

    def _read_paths_from_sandbox(self, paths: Sequence[str], *, max_chars: int) -> Dict[str, str]:
        """Read repository-relative paths from the cloned sandbox (no GitHub MCP)."""
        out: Dict[str, str] = {}
        for raw in paths:
            fp = _normalize_repo_path(str(raw))
            if not fp:
                continue
            body = self._provider.read_full_file(fp, max_chars=max_chars)
            if body.strip():
                out[fp] = body
        return out

    def _get_github_docs_bundle_cached(
        self,
        owner: str,
        repo: str,
        ref: str,
        paths: Sequence[str],
    ) -> RepoDocsBundle | None:
        if self._github_provider is None:
            return None
        requested = sorted({_normalize_repo_path(p) for p in paths if p and str(p).strip()})
        if not requested:
            return None
        cache_key = (owner, repo, ref)
        cached = self._github_docs_bundle_cache.get(cache_key)
        have = {doc.path for doc in cached.documents} if cached is not None else set()
        need_fetch = [p for p in requested if p not in have]
        if not need_fetch and cached is not None:
            return cached
        fetched = self._github_provider.get_repo_docs(owner, repo, ref, need_fetch)
        if cached is None:
            self._github_docs_bundle_cache[cache_key] = fetched
            return fetched
        merged_docs = list(cached.documents) + [
            doc for doc in fetched.documents if doc.path not in have
        ]
        merged = RepoDocsBundle(
            repo=cached.repo,
            ref=cached.ref,
            documents=merged_docs,
            warnings=list(cached.warnings) + list(fetched.warnings),
        )
        self._github_docs_bundle_cache[cache_key] = merged
        return merged

    def _apply_github_fallback(
        self,
        *,
        state: GraphState,
        request: FocusedContextRequest,
        file_snippets: Dict[str, str],
        search_hits: Dict[str, List[SearchResult]],
        warnings: List[str],
        missing_files: List[str],
        total_chars: int,
    ) -> None:
        settings = get_settings()
        if not settings.github_mcp_enabled or self._github_provider is None:
            return

        repo_identity = _resolve_repo_identity(state)
        if repo_identity is None:
            return
        owner, repo = repo_identity
        ref = _resolve_docs_ref(state)

        if missing_files:
            sandbox_hits = self._read_paths_from_sandbox(
                missing_files,
                max_chars=settings.review_full_file_max_chars,
            )
            for fp, body in sandbox_hits.items():
                file_snippets[fp] = body[:MAX_FILE_SLICE_CHARS]
            if sandbox_hits:
                warnings.append("sandbox_file_fallback")

            still_missing = [
                fp
                for fp in missing_files
                if fp not in file_snippets and fp not in sandbox_hits
            ]
            if still_missing:
                bundle = self._get_github_docs_bundle_cached(owner, repo, ref, still_missing)
                if bundle is not None:
                    for doc in bundle.documents:
                        file_snippets[doc.path] = doc.content[:MAX_FILE_SLICE_CHARS]
                    warnings.extend(bundle.warnings)
                    if bundle.documents:
                        warnings.append("github_file_fallback")

        missing_symbols = [
            sym
            for sym in request.symbol_queries[:MAX_SYMBOL_QUERIES]
            if not search_hits.get(sym)
        ]
        missing_text = [
            tq
            for tq in request.text_queries[:MAX_TEXT_QUERIES]
            if not search_hits.get(tq)
        ]
        if not missing_symbols and not missing_text:
            return

        if not settings.github_mcp_focused_context_doc_fallback:
            if missing_symbols or missing_text:
                warnings.append("sandbox_search_no_hits:github_doc_fallback_disabled")
            return

        doc_paths = settings.github_mcp_doc_paths
        if not doc_paths:
            return

        sandbox_docs = self._read_paths_from_sandbox(
            doc_paths,
            max_chars=settings.github_mcp_doc_max_chars,
        )
        documents = [
            RepoDocument(path=path, ref=ref, content=content, truncated=len(content) >= settings.github_mcp_doc_max_chars)
            for path, content in sandbox_docs.items()
        ]
        mcp_paths = [p for p in doc_paths if _normalize_repo_path(p) not in sandbox_docs]
        if mcp_paths:
            bundle = self._get_github_docs_bundle_cached(owner, repo, ref, mcp_paths)
            if bundle is not None:
                warnings.extend(bundle.warnings)
                documents.extend(bundle.documents)

        if not documents:
            return

        for sym in missing_symbols:
            hits = _search_docs_for_symbol(documents, sym, MAX_SEARCH_RESULTS_PER_QUERY)
            if hits:
                search_hits[sym] = hits
                total_chars += sum(len(h.content) for h in hits)
            if total_chars > MAX_TOTAL_RESULT_CHARS:
                warnings.append("truncated_total_chars")
                return

        for tq in missing_text:
            hits = _search_docs_for_text(documents, tq, MAX_SEARCH_RESULTS_PER_QUERY)
            if hits:
                search_hits[tq] = hits
                total_chars += sum(len(h.content) for h in hits)
            if total_chars > MAX_TOTAL_RESULT_CHARS:
                warnings.append("truncated_total_chars")
                return

        if missing_symbols or missing_text:
            warnings.append("github_docs_fallback")


def _resolve_repo_identity(state: GraphState) -> tuple[str, str] | None:
    metadata = state.get("metadata", {}) or {}
    candidates = [
        metadata.get("pr_repo"),
        metadata.get("review_repo_url"),
        state.get("repo_path"),
    ]
    for value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        slug = _parse_repo_slug(value.strip())
        if slug is not None:
            return slug
    return None


def _parse_repo_slug(value: str) -> tuple[str, str] | None:
    if "github.com" in value:
        parsed = urlparse(value)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2:
            return parts[0], parts[1]
        return None
    if ":" in value or "\\" in value or value.startswith("/"):
        return None
    if "/" not in value:
        return None
    owner, repo = value.split("/", maxsplit=1)
    if not owner or not repo:
        return None
    return owner, repo


def _resolve_docs_ref(state: GraphState) -> str:
    metadata = state.get("metadata", {}) or {}
    docs_meta = metadata.get("docs_prebrief", {}) if isinstance(metadata, dict) else {}
    if isinstance(docs_meta, dict):
        ref = docs_meta.get("ref")
        if isinstance(ref, str) and ref.strip():
            return ref.strip()
    return "main"


def _search_docs_for_symbol(
    docs: Sequence[RepoDocument],
    symbol: str,
    max_hits: int,
) -> List[SearchResult]:
    if not symbol:
        return []
    pattern = re.compile(r"\b" + re.escape(symbol) + r"\b", flags=re.IGNORECASE)
    return _search_docs_with_pattern(docs, pattern, max_hits)


def _search_docs_for_text(
    docs: Sequence[RepoDocument],
    query: str,
    max_hits: int,
) -> List[SearchResult]:
    if not query:
        return []
    try:
        pattern = re.compile(query, flags=re.IGNORECASE)
        return _search_docs_with_pattern(docs, pattern, max_hits)
    except re.error:
        return _search_docs_substring(docs, query, max_hits)


def _search_docs_with_pattern(
    docs: Sequence[RepoDocument],
    pattern: re.Pattern[str],
    max_hits: int,
) -> List[SearchResult]:
    results: List[SearchResult] = []
    for doc in docs:
        for index, line in enumerate(doc.content.splitlines(), start=1):
            if not pattern.search(line):
                continue
            line_text = line.strip()
            results.append(
                SearchResult(
                    file_path=doc.path,
                    line_number=index,
                    content=line_text,
                    context_lines=[line_text],
                )
            )
            if len(results) >= max_hits:
                return results
    return results


def _search_docs_substring(
    docs: Sequence[RepoDocument],
    query: str,
    max_hits: int,
) -> List[SearchResult]:
    needle = query.lower()
    results: List[SearchResult] = []
    for doc in docs:
        for index, line in enumerate(doc.content.splitlines(), start=1):
            if needle not in line.lower():
                continue
            line_text = line.strip()
            results.append(
                SearchResult(
                    file_path=doc.path,
                    line_number=index,
                    content=line_text,
                    context_lines=[line_text],
                )
            )
            if len(results) >= max_hits:
                return results
    return results
