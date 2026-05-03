from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from langgraph.checkpoint.redis import RedisSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from src.config import get_settings
from src.domain.interfaces import IASTParser, ICodeSearcher
from src.domain.schemas import CodeEntity, ReviewTask, SearchResult
from src.domain.state import GraphState
from src.infrastructure.factory import build_ast_parser, build_cache_service, build_preflight_service
from src.infrastructure.sandbox import RepoSandbox
from src.infrastructure.search.ripgrep import RipgrepSearcher
from src.orchestration.nodes.application.planner import make_review_planner_node
from src.orchestration.nodes.application.synthesizer import synthesizer_node
from src.orchestration.nodes.application.worker import (
    ReviewTaskContext,
    make_specialist_worker_node,
)
from src.orchestration.nodes.exploration.structural_extractor import make_structural_extractor_node

logger = logging.getLogger(__name__)

WORKER_NODE_BY_SPECIALTY = {
    "security": "security_worker",
    "logic": "logic_worker",
    "performance": "performance_worker",
    "general": "general_worker",
}


class LazyReviewContextProvider:
    """Shared direct-context adapter for all reviewer workers in one graph run."""

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
            snippet = self._read_file(file_path)
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
                    self._startup_warnings.append("ast_unavailable_for_remote_sandbox")

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


def _route_initial_context(state: GraphState) -> str:
    if state.get("preflight_summary") and state.get("structural_graph_node_link"):
        return "review_planner"
    return "structural_extractor"


def _route_review_tasks(state: GraphState):
    registry = state.get("task_registry", {}) or {}
    root_task_id = state.get("root_task_id")
    sends: List[Send] = []

    for task_id, task in sorted(registry.items()):
        if task_id == root_task_id:
            continue
        if state.get("task_status_by_id", {}).get(task_id) == "completed":
            continue
        specialty = task.specialty if task.specialty in WORKER_NODE_BY_SPECIALTY else "general"
        payload = dict(state)
        payload["current_task_id"] = task_id
        sends.append(Send(WORKER_NODE_BY_SPECIALTY[specialty], payload))

    return sends or "review_synthesizer"


def _make_cleanup_synthesizer(context_provider: LazyReviewContextProvider):
    def cleanup_synthesizer_node(state: GraphState) -> Dict[str, Any]:
        try:
            return synthesizer_node(state)
        finally:
            context_provider.stop()

    return cleanup_synthesizer_node


def build_graph(checkpointer: Any = None):
    settings = get_settings()
    context_provider = LazyReviewContextProvider()
    preflight_service = build_preflight_service()
    ast_parser: IASTParser | None = None

    if settings.ast_enabled:
        try:
            ast_parser = build_ast_parser(settings=settings, cache=build_cache_service())
        except Exception as exc:
            if not settings.ast_fallback_to_search:
                raise
            logger.warning(
                "AST parser startup failed for reviewer structural extraction; continuing degraded. reason=%s",
                exc,
            )

    structural_extractor_node = make_structural_extractor_node(
        preflight_service=preflight_service,
        ast_parser=ast_parser,
    )

    builder = StateGraph(GraphState)
    builder.add_node("structural_extractor", structural_extractor_node)
    builder.add_node("review_planner", make_review_planner_node())
    builder.add_node(
        "security_worker",
        make_specialist_worker_node("security", context_provider=context_provider),
    )
    builder.add_node(
        "logic_worker",
        make_specialist_worker_node("logic", context_provider=context_provider),
    )
    builder.add_node(
        "performance_worker",
        make_specialist_worker_node("performance", context_provider=context_provider),
    )
    builder.add_node(
        "general_worker",
        make_specialist_worker_node("general", context_provider=context_provider),
    )
    builder.add_node("review_synthesizer", _make_cleanup_synthesizer(context_provider))

    builder.add_conditional_edges(
        START,
        _route_initial_context,
        {
            "structural_extractor": "structural_extractor",
            "review_planner": "review_planner",
        },
    )
    builder.add_edge("structural_extractor", "review_planner")
    builder.add_conditional_edges("review_planner", _route_review_tasks)
    for worker_node in WORKER_NODE_BY_SPECIALTY.values():
        builder.add_edge(worker_node, "review_synthesizer")
    builder.add_edge("review_synthesizer", END)

    if checkpointer is None:
        return builder.compile()
    return builder.compile(checkpointer=checkpointer)


def run_reviewer(state: GraphState) -> Dict[str, Any]:
    settings = get_settings()
    if not settings.redis_enabled:
        graph = build_graph()
        return graph.invoke(state)

    thread_id = state.get("run_id", "reviewer_graph")
    try:
        with RedisSaver.from_conn_string(settings.redis_url) as checkpointer:
            graph = build_graph(checkpointer=checkpointer)
            return graph.invoke(
                state,
                config={"configurable": {"thread_id": thread_id}},
            )
    except Exception as exc:
        logger.warning(
            "Redis checkpoint unavailable for reviewer run; continuing without checkpointing: %s: %s",
            exc.__class__.__name__,
            exc,
        )
        graph = build_graph()
        result = graph.invoke(state)
        metadata = dict(result.get("metadata", {}))
        metadata["checkpoint_warning"] = (
            f"Redis checkpoint unavailable; ran without checkpointing: {exc.__class__.__name__}: {exc}"
        )
        result["metadata"] = metadata
        return result


graph = build_graph()
