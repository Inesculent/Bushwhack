from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from langgraph.checkpoint.redis import RedisSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from src.config import get_settings
from src.domain.interfaces import IASTParser
from src.domain.interfaces import IPreflightService
from src.domain.schemas import (
    CodeEntity,
    PreflightRequest,
    PreflightSummary,
    ReflectionReport,
    RunMetadata,
    StructuralExtractionGap,
)
from src.domain.state import GraphState
from src.infrastructure.remote_review_workflow import collect_structural_entities
from src.infrastructure.factory import build_ast_parser, build_cache_service, build_preflight_service
from src.infrastructure.sandbox import RepoSandbox
from src.infrastructure.structural_graph import StructuralGraphBuilder
from src.infrastructure.structural_topology import (
    apply_community_attributes,
    build_topology_summary,
    run_structural_topology,
)
from src.orchestration.context.review_context import LazyReviewContextProvider
from src.orchestration.nodes.application.cleanup import make_adversarial_cleanup_node
from src.orchestration.nodes.application.critique_revision import make_critique_revision_node
from src.orchestration.nodes.application.critiquer import make_general_critiquer_node
from src.orchestration.nodes.application.focused_context import make_focused_context_node
from src.orchestration.nodes.application.planner import make_review_planner_node
from src.orchestration.nodes.application.reflection import make_adversarial_reflection_node
from src.orchestration.nodes.application.synthesizer import synthesizer_node
from src.orchestration.nodes.application.worker import make_specialist_worker_node
from src.orchestration.nodes.exploration.structural_extractor import make_structural_extractor_node

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

WORKER_NODE_BY_SPECIALTY = {
    "security": "security_worker",
    "logic": "logic_worker",
    "performance": "performance_worker",
    "general": "general_worker",
}


def _route_critique_tasks(state: GraphState):
    registry = state.get("task_registry", {}) or {}
    root_task_id = state.get("root_task_id")
    metadata = state.get("metadata", {}) or {}
    sends: List[Send] = []

    for task_id, task in sorted(registry.items()):
        if task_id == root_task_id:
            continue
        if state.get("task_status_by_id", {}).get(task_id) == "completed":
            continue
        payload = dict(state)
        payload["current_task_id"] = task_id
        sends.append(Send("general_critiquer", payload))
        if metadata.get("review_trace_enabled"):
            trace_logger.info(
                "TRACE dispatch_critiquer run_id=%s task_id=%s files=%s",
                state.get("run_id", "unknown"),
                task_id,
                task.target_files,
            )

    if not sends and metadata.get("review_trace_enabled"):
        trace_logger.info(
            "TRACE dispatch_adversarial_reflection run_id=%s reason=no_pending_tasks",
            state.get("run_id", "unknown"),
        )
    return sends or "adversarial_reflection"


def _route_focused_after_reflection(state: GraphState) -> str:
    for raw in state.get("reflection_reports", []) or []:
        report: ReflectionReport | None
        if isinstance(raw, ReflectionReport):
            report = raw
        elif isinstance(raw, dict):
            try:
                report = ReflectionReport.model_validate(raw)
            except Exception:
                report = None
        else:
            report = None
        if report is not None and report.verdict == "needs_context" and report.focused_request is not None:
            return "focused_context"
    return "adversarial_cleanup"


def _route_initial_context(state: GraphState) -> str:
    metadata = state.get("metadata", {}) or {}
    repo_path = str(state.get("repo_path", "") or "")
    if state.get("preflight_summary") and state.get("structural_graph_node_link"):
        route = "review_planner"
    elif Path(repo_path).is_dir():
        route = "structural_extractor"
    else:
        route = "sandbox_structural_extractor"
    if metadata.get("review_trace_enabled"):
        trace_logger.info(
            "TRACE route_initial run_id=%s route=%s",
            state.get("run_id", "unknown"),
            route,
        )
    return route


def _make_sandbox_structural_extractor_node(
    context_provider: LazyReviewContextProvider,
    preflight_service: IPreflightService,
):
    def sandbox_structural_extractor_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        repo_path = str(state.get("repo_path", "") or "")
        git_diff = state.get("git_diff", "") or "\n"
        settings = get_settings()

        manifest = preflight_service.build_diff_manifest(
            PreflightRequest(
                run_metadata=RunMetadata(
                    repo=repo_path,
                    base_sha="unknown",
                    head_sha=run_id,
                    run_id=run_id,
                ),
                raw_diff=git_diff,
            )
        )

        sandbox = context_provider.get_sandbox(state)
        sandbox_entities = collect_structural_entities(sandbox)
        entities_by_file = {
            filepath: [CodeEntity.model_validate(entity) for entity in raw_entities]
            for filepath, raw_entities in sandbox_entities.get("files", {}).items()
        }
        extraction_gaps = [
            StructuralExtractionGap.model_validate(gap)
            for gap in sandbox_entities.get("gaps", [])
        ]
        file_languages = sandbox_entities.get("file_languages", {}) or {}

        build_result = StructuralGraphBuilder.build_from_entities(
            entities_by_file=entities_by_file,
            file_languages=file_languages,
            extraction_gaps=extraction_gaps,
        )

        topology_summary = None
        if settings.structural_topology_enabled and build_result.graph.number_of_nodes() > 0:
            topo = run_structural_topology(
                build_result.graph,
                max_fraction=settings.community_max_fraction,
                min_split_size=settings.community_min_split_size,
                max_files=settings.community_max_files,
                max_symbols=settings.community_max_symbols,
                louvain_seed=settings.louvain_seed,
            )
            apply_community_attributes(build_result.graph, topo.partition)
            topology_summary = build_topology_summary(
                topo,
                build_result.graph,
                {
                    "structural_topology_enabled": settings.structural_topology_enabled,
                    "community_max_fraction": settings.community_max_fraction,
                    "community_min_split_size": settings.community_min_split_size,
                    "community_max_files": settings.community_max_files,
                    "community_max_symbols": settings.community_max_symbols,
                    "louvain_seed": settings.louvain_seed,
                },
            )

        graph_payload = StructuralGraphBuilder.serialize(build_result.graph)
        metadata = dict(state.get("metadata", {}))
        structural_meta: Dict[str, Any] = {
            "mode": "sandbox_entities",
            "files_attempted": build_result.files_attempted,
            "files_parsed": build_result.files_parsed,
            "gap_count": len(build_result.gaps),
            "node_count": build_result.graph.number_of_nodes(),
            "edge_count": build_result.graph.number_of_edges(),
        }
        if topology_summary is not None:
            structural_meta["topology_algorithm"] = topology_summary.algorithm
            structural_meta["community_count"] = topology_summary.community_count
            structural_meta["topology_splits_applied"] = topology_summary.splits_applied
        metadata["structural_extractor"] = structural_meta

        preflight_summary = PreflightSummary(
            manifest_id=manifest.manifest_id,
            total_files_changed=manifest.aggregate_metrics.total_files_changed,
            total_hunks=manifest.aggregate_metrics.total_hunks,
            total_additions=manifest.aggregate_metrics.total_additions,
            total_deletions=manifest.aggregate_metrics.total_deletions,
            has_errors=bool(manifest.errors),
            has_ambiguity=bool(manifest.ambiguity_flags),
        )

        if metadata.get("review_trace_enabled"):
            trace_logger.info(
                "TRACE sandbox_structural_extractor run_id=%s files_parsed=%s nodes=%s edges=%s gaps=%s",
                run_id,
                build_result.files_parsed,
                build_result.graph.number_of_nodes(),
                build_result.graph.number_of_edges(),
                len(build_result.gaps),
            )

        out: Dict[str, Any] = {
            "diff_manifest_ref": manifest.manifest_id,
            "preflight_summary": preflight_summary,
            "preflight_errors": manifest.errors,
            "preflight_warnings": manifest.warnings,
            "structural_graph_node_link": graph_payload,
            "structural_extraction_gaps": build_result.gaps,
            "metadata": metadata,
            "node_history": ["sandbox_structural_extractor"],
            "next_step": "plan",
        }
        if topology_summary is not None:
            out["structural_topology"] = topology_summary
        return out

    return sandbox_structural_extractor_node


def _route_review_tasks(state: GraphState):
    registry = state.get("task_registry", {}) or {}
    root_task_id = state.get("root_task_id")
    metadata = state.get("metadata", {}) or {}
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
        if metadata.get("review_trace_enabled"):
            trace_logger.info(
                "TRACE dispatch_worker run_id=%s task_id=%s specialty=%s node=%s files=%s",
                state.get("run_id", "unknown"),
                task_id,
                specialty,
                WORKER_NODE_BY_SPECIALTY[specialty],
                task.target_files,
            )

    if not sends and metadata.get("review_trace_enabled"):
        trace_logger.info(
            "TRACE dispatch_synthesizer run_id=%s reason=no_pending_tasks",
            state.get("run_id", "unknown"),
        )
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
    builder.add_node(
        "sandbox_structural_extractor",
        _make_sandbox_structural_extractor_node(
            context_provider=context_provider,
            preflight_service=preflight_service,
        ),
    )
    builder.add_node("review_planner", make_review_planner_node())
    builder.add_node("review_synthesizer", _make_cleanup_synthesizer(context_provider))

    if settings.reviewer_use_legacy_specialist_workers:
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
        builder.add_conditional_edges("review_planner", _route_review_tasks)
        for worker_node in WORKER_NODE_BY_SPECIALTY.values():
            builder.add_edge(worker_node, "review_synthesizer")
    else:
        builder.add_node(
            "general_critiquer",
            make_general_critiquer_node(context_provider=context_provider),
        )
        builder.add_node("adversarial_reflection", make_adversarial_reflection_node())
        builder.add_node("focused_context", make_focused_context_node(context_provider))
        builder.add_node("critique_revision", make_critique_revision_node())
        builder.add_node("adversarial_cleanup", make_adversarial_cleanup_node())
        builder.add_conditional_edges("review_planner", _route_critique_tasks)
        builder.add_edge("general_critiquer", "adversarial_reflection")
        builder.add_conditional_edges(
            "adversarial_reflection",
            _route_focused_after_reflection,
            {
                "focused_context": "focused_context",
                "adversarial_cleanup": "adversarial_cleanup",
            },
        )
        builder.add_edge("focused_context", "critique_revision")
        builder.add_edge("critique_revision", "adversarial_cleanup")
        builder.add_edge("adversarial_cleanup", "review_synthesizer")

    builder.add_conditional_edges(
        START,
        _route_initial_context,
        {
            "structural_extractor": "structural_extractor",
            "sandbox_structural_extractor": "sandbox_structural_extractor",
            "review_planner": "review_planner",
        },
    )
    builder.add_edge("structural_extractor", "review_planner")
    builder.add_edge("sandbox_structural_extractor", "review_planner")
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
