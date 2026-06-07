from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from src.config import get_settings
from src.domain.interfaces import IASTParser
from src.domain.interfaces import IPreflightService
from src.domain.schemas import (
    CodeEntity,
    PreflightRequest,
    PreflightSummary,
    RunMetadata,
    StructuralExtractionGap,
)
from src.domain.state import GraphState
from src.infrastructure.remote_review_workflow import collect_structural_entities
from src.infrastructure.factory import (
    build_ast_parser,
    build_cache_service,
    build_github_context_provider,
    build_preflight_service,
    build_snapshot_pointer_store,
    build_snapshot_writer,
)
from src.infrastructure.redis_checkpoint import (
    assert_redis_checkpoint_writable,
    delete_checkpoint_thread,
    redis_checkpoint_saver,
)
from src.infrastructure.llm.langsmith import configure_langsmith_environment
from src.infrastructure.sandbox import RepoSandbox
from src.infrastructure.structural_graph import StructuralGraphBuilder
from src.infrastructure.structural_topology import (
    apply_community_attributes,
    build_topology_summary,
    run_structural_topology,
)
from src.orchestration.context.review_context import LazyReviewContextProvider
from src.orchestration.nodes.application.cleanup import make_adversarial_cleanup_node
from src.orchestration.nodes.application.review_adjudicator import make_review_adjudicator_node
from src.orchestration.nodes.application.review_evidence_triage import make_review_evidence_triage_node
from src.orchestration.nodes.application.critique_revision import (
    _needs_revision_candidates,
    critique_revision_digests_complete,
    make_critique_revision_digest_node,
    make_critique_revision_reduce_node,
    plan_critique_revision_shards,
    revision_inputs_ready,
    revision_ready_candidate_ids,
)
from src.orchestration.nodes.application.actor_critic_planner import (
    make_draft_planner_node,
    make_mandate_finalize_node,
    make_plan_critic_node,
    make_plan_emit_node,
    make_plan_revision_node,
    route_plan_critic,
)
from src.orchestration.nodes.application.critique_pipeline import build_critique_review_subgraph
from src.orchestration.nodes.application.focused_context import make_focused_context_node
from src.orchestration.nodes.application.planner import make_review_planner_node
from src.orchestration.nodes.application.reflection import make_adversarial_reflection_node
from src.orchestration.nodes.application.synthesizer import synthesizer_node
from src.orchestration.nodes.application.worker import make_specialist_worker_node
from src.orchestration.nodes.exploration.community_semantic_agent import make_community_semantic_agent_node
from src.orchestration.nodes.exploration.docs_prebrief import make_docs_prebrief_node
from src.orchestration.nodes.exploration.phase2_routing import semantic_phase2_should_run
from src.orchestration.nodes.exploration.semantic_dispatch import (
    make_semantic_dispatch_node,
    route_semantic_dispatch,
)
from src.orchestration.nodes.exploration.semantic_merge import make_semantic_merge_node
from src.orchestration.nodes.exploration.snapshot_pin import make_snapshot_pin_node
from src.orchestration.nodes.mental_model import make_intent_extractor_node
from src.orchestration.nodes.review_history_context import make_review_history_context_node
from src.orchestration.nodes.mandate_explorer_node import (
    make_mandate_explorer_bootstrap_node,
    make_mandate_explorer_targeted_node,
)
from src.orchestration.nodes.mandate_patch_node import make_mandate_patch_node
from src.orchestration.routing.mandate_plan_coupling import (
    route_after_intent,
    route_after_mandate_patch,
)
from src.orchestration.nodes.exploration.structural_extractor import make_structural_extractor_node
from src.orchestration.nodes.exploration.unverified_call_resolver import (
    make_unverified_call_resolver_node,
    route_after_unverified_call_resolver,
)
from src.orchestration.routing.adversarial_after_reflection import (
    final_adversarial_review_node,
    route_focused_after_reflection,
)
from src.orchestration.routing.send_payload import payload_for_send
from src.orchestration.routing.verifier_fanout import (
    collect_verifier_send_payloads,
    collect_source_only_verifier_updates,
    make_verifier_subgraph_node,
)

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
        payload = payload_for_send(state, current_task_id=task_id)
        sends.append(Send("critique_review_subgraph", payload))
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
    return sends or "review_evidence_triage"


def _make_post_reflection_evidence_pass_node():
    """No-op join point so verifier/critique routing runs even when the second focused_context fetch is skipped."""

    node_name = "post_reflection_evidence_pass"

    def post_reflection_evidence_pass_node(state: GraphState) -> Dict[str, Any]:
        metadata = dict(state.get("metadata") or {})
        slot = dict(metadata.get(node_name) or {})
        slot["entered"] = True
        metadata[node_name] = slot
        source_only_update = collect_source_only_verifier_updates({**state, "metadata": metadata})
        if metadata.get("review_trace_enabled"):
            trace_logger.info(
                "TRACE %s run_id=%s needs_revision=%s",
                node_name,
                state.get("run_id", "unknown"),
                _needs_revision_candidates(state),
            )
        if source_only_update:
            source_history = list(source_only_update.get("node_history") or [])
            return {
                **source_only_update,
                "node_history": [node_name, *source_history],
            }
        return {"metadata": metadata, "node_history": [node_name]}

    return post_reflection_evidence_pass_node


def _route_after_focused_context(state: GraphState):
    """Optionally fan out verifier branches, then fall through to critique revision routing."""
    sends = collect_verifier_send_payloads(state)
    if sends:
        return sends
    return _route_critique_revision(state)


def post_verifier_gate_node(state: GraphState) -> Dict[str, Any]:
    """Join point after all parallel ``verifier_subgraph`` Send branches complete."""
    metadata = state.get("metadata", {}) or {}
    if metadata.get("review_trace_enabled"):
        reports = state.get("verifier_reports", []) or []
        trace_logger.info(
            "TRACE post_verifier_gate run_id=%s verifier_reports=%s",
            state.get("run_id", "unknown"),
            len(reports),
        )
    return {"node_history": ["post_verifier_gate"]}


def _route_after_critique_revision_digest(state: GraphState):
    """Run reduce only after the last digest shard lands (map-reduce barrier)."""
    metadata = state.get("metadata", {}) or {}
    if critique_revision_digests_complete(state):
        if metadata.get("review_trace_enabled"):
            digests = state.get("critique_revision_digests", {}) or {}
            trace_logger.info(
                "TRACE critique_revision_digest_barrier run_id=%s digests=%s route=reduce",
                state.get("run_id", "unknown"),
                len(digests),
            )
        return "critique_revision_reduce"
    if metadata.get("review_trace_enabled"):
        trace_logger.info(
            "TRACE critique_revision_digest_barrier run_id=%s route=await_sibling_shards",
            state.get("run_id", "unknown"),
        )
    return END


def _route_critique_revision(state: GraphState):
    """Fan out digest workers when revision work exists; otherwise skip to final review.

    Invoked from ``post_verifier_gate`` (after all verifier branches join) or when no
  verifiers were scheduled. Must not be wired directly off each verifier branch — that
    caused premature ``adversarial_cleanup`` while siblings were still running.
    """
    metadata = state.get("metadata", {}) or {}
    all_revision_ids = _needs_revision_candidates(state)
    candidate_ids = revision_ready_candidate_ids(state, all_revision_ids)
    if not all_revision_ids:
        if metadata.get("review_trace_enabled"):
            trace_logger.info(
                "TRACE dispatch_critique_revision run_id=%s route=%s",
                state.get("run_id", "unknown"),
                "final_review_no_candidates",
            )
        return final_adversarial_review_node()
    if not revision_inputs_ready(state, all_revision_ids):
        skipped = sorted(set(all_revision_ids) - set(candidate_ids))
        if metadata.get("review_trace_enabled"):
            trace_logger.info(
                "TRACE dispatch_critique_revision run_id=%s route=%s ready=%s skipped=%s",
                state.get("run_id", "unknown"),
                "final_review_no_revision_inputs",
                candidate_ids,
                skipped,
            )
        return final_adversarial_review_node()
    if metadata.get("review_trace_enabled") and len(candidate_ids) < len(all_revision_ids):
        trace_logger.info(
            "TRACE dispatch_critique_revision run_id=%s partial_ready ready=%s skipped=%s",
            state.get("run_id", "unknown"),
            candidate_ids,
            sorted(set(all_revision_ids) - set(candidate_ids)),
        )
    settings = get_settings()
    shards = plan_critique_revision_shards(
        state,
        candidate_ids,
        max_shard_chars=settings.reviewer_critique_revision_max_shard_chars,
        max_candidate_chars=settings.reviewer_critique_revision_max_candidate_chars,
    )
    if not shards:
        if metadata.get("review_trace_enabled"):
            trace_logger.info(
                "TRACE dispatch_critique_revision run_id=%s route=%s",
                state.get("run_id", "unknown"),
                "final_review_no_shards",
            )
        return final_adversarial_review_node()
    sends: List[Send] = []
    for shard in shards:
        payload = payload_for_send(state, critique_revision_shard=shard.model_dump(mode="json"))
        sends.append(Send("critique_revision_digest", payload))
    if metadata.get("review_trace_enabled"):
        trace_logger.info(
            "TRACE dispatch_critique_revision run_id=%s shards=%s",
            state.get("run_id", "unknown"),
            len(shards),
        )
    return sends


def _route_initial_context(state: GraphState) -> str:
    metadata = state.get("metadata", {}) or {}
    repo_path = str(state.get("repo_path", "") or "")
    settings = get_settings()
    if state.get("preflight_summary") and state.get("structural_graph_node_link"):
        if semantic_phase2_should_run(state, settings):
            route = "semantic_dispatch"
        elif (
            not settings.reviewer_legacy_planner_mode
            and state.get("snapshot_source") in {"loaded", "explore"}
        ):
            # Exploration context is ready, regardless of whether it was loaded or built live.
            # Phase 0 + actor-critic run on the normalized graph/summaries without re-running semantic_merge.
            route = "intent_extractor"
        else:
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


def _docs_prebrief_done(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    docs_meta = metadata.get("docs_prebrief", {})
    if isinstance(docs_meta, dict):
        return bool(docs_meta.get("status"))
    return False


def _route_start(state: GraphState) -> str:
    settings = get_settings()
    if settings.docs_prebrief_enabled and not _docs_prebrief_done(state):
        return "docs_prebrief"
    return _route_initial_context(state)


def _route_after_structural(state: GraphState) -> str:
    if semantic_phase2_should_run(state):
        return "semantic_dispatch"
    return "review_planner"


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
        payload = payload_for_send(state, current_task_id=task_id)
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


def _route_after_semantic_merge(state: GraphState) -> str:
    if get_settings().reviewer_legacy_planner_mode:
        return "snapshot_pin"
    return "intent_extractor"


def _route_after_snapshot_pin(state: GraphState):
    if get_settings().reviewer_legacy_planner_mode:
        return "review_planner"
    return _route_after_planner(state)


def _route_after_planner(state: GraphState):
    """After any planner node emits tasks, fan out to workers or adversarial critique."""
    if get_settings().reviewer_use_legacy_specialist_workers:
        return _route_review_tasks(state)
    return _route_critique_tasks(state)


def _make_cleanup_synthesizer(context_provider: LazyReviewContextProvider):
    def cleanup_synthesizer_node(state: GraphState) -> Dict[str, Any]:
        try:
            return synthesizer_node(state)
        finally:
            context_provider.stop()

    return cleanup_synthesizer_node


def build_graph(
    checkpointer: Any = None,
    context_provider: LazyReviewContextProvider | None = None,
):
    settings = get_settings()
    configure_langsmith_environment(settings)
    context_provider = context_provider or LazyReviewContextProvider()
    preflight_service = build_preflight_service()
    cache = build_cache_service()
    ast_parser: IASTParser | None = None

    if settings.ast_enabled:
        try:
            ast_parser = build_ast_parser(settings=settings, cache=cache)
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

    github_provider = build_github_context_provider(settings=settings, cache=cache)

    snapshot_writer = build_snapshot_writer(settings)
    pointer_store = build_snapshot_pointer_store(settings)

    builder = StateGraph(GraphState)
    builder.add_node(
        "docs_prebrief",
        make_docs_prebrief_node(github_provider=github_provider, settings=settings),
    )
    builder.add_node("structural_extractor", structural_extractor_node)
    builder.add_node(
        "sandbox_structural_extractor",
        _make_sandbox_structural_extractor_node(
            context_provider=context_provider,
            preflight_service=preflight_service,
        ),
    )
    builder.add_node("semantic_dispatch", make_semantic_dispatch_node(settings))
    if settings.semantic_legacy_community_agents_enabled:
        builder.add_node("community_semantic_agent", make_community_semantic_agent_node(settings=settings))
    builder.add_node(
        "unverified_call_resolver",
        make_unverified_call_resolver_node(ast_parser=ast_parser, settings=settings),
    )
    builder.add_node("semantic_merge", make_semantic_merge_node(settings=settings))
    builder.add_node("intent_extractor", make_intent_extractor_node(settings=settings))
    builder.add_node(
        "review_history_context",
        make_review_history_context_node(github_provider=github_provider, settings=settings),
    )
    builder.add_node(
        "mandate_explorer",
        make_mandate_explorer_bootstrap_node(context_provider, settings=settings),
    )
    builder.add_node(
        "mandate_explorer_targeted",
        make_mandate_explorer_targeted_node(context_provider, settings=settings),
    )
    builder.add_node("mandate_patch", make_mandate_patch_node(settings=settings))
    builder.add_node(
        "mandate_finalize",
        make_mandate_finalize_node(settings=settings, context_provider=context_provider),
    )
    builder.add_node(
        "snapshot_pin",
        make_snapshot_pin_node(snapshot_writer, pointer_store, settings=settings),
    )
    builder.add_node("review_planner", make_review_planner_node())
    builder.add_node("draft_planner", make_draft_planner_node(settings=settings))
    builder.add_node("plan_critic", make_plan_critic_node(settings=settings))
    builder.add_node("plan_revision", make_plan_revision_node(settings=settings))
    builder.add_node("plan_emit", make_plan_emit_node())
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
        critique_review_subgraph = build_critique_review_subgraph(
            context_provider,
            github_provider=github_provider,
        )
        builder.add_node("critique_review_subgraph", critique_review_subgraph)
        builder.add_node("review_evidence_triage", make_review_evidence_triage_node())
        builder.add_node("adversarial_reflection", make_adversarial_reflection_node())
        builder.add_node(
            "initial_focused_context",
            make_focused_context_node(context_provider, github_provider=github_provider),
        )
        builder.add_node(
            "focused_context",
            make_focused_context_node(context_provider, github_provider=github_provider),
        )
        builder.add_node("critique_revision_digest", make_critique_revision_digest_node())
        builder.add_node("critique_revision_reduce", make_critique_revision_reduce_node())
        builder.add_node("post_verifier_gate", post_verifier_gate_node)
        builder.add_node("adversarial_cleanup", make_adversarial_cleanup_node())
        builder.add_node("review_adjudicator", make_review_adjudicator_node())
        builder.add_node("verifier_subgraph", make_verifier_subgraph_node())
        builder.add_node("post_reflection_evidence_pass", _make_post_reflection_evidence_pass_node())
        builder.add_conditional_edges("review_planner", _route_after_planner)
        builder.add_edge("critique_review_subgraph", "initial_focused_context")
        builder.add_edge("initial_focused_context", "review_evidence_triage")
        builder.add_edge("review_evidence_triage", "adversarial_reflection")
        builder.add_conditional_edges(
            "adversarial_reflection",
            route_focused_after_reflection,
            {
                "focused_context": "focused_context",
                "post_reflection_evidence_pass": "post_reflection_evidence_pass",
                "adversarial_cleanup": "adversarial_cleanup",
                "review_adjudicator": "review_adjudicator",
            },
        )
        builder.add_conditional_edges("focused_context", _route_after_focused_context)
        builder.add_conditional_edges("post_reflection_evidence_pass", _route_after_focused_context)
        builder.add_edge("verifier_subgraph", "post_verifier_gate")
        builder.add_conditional_edges("post_verifier_gate", _route_critique_revision)
        # Always fan in to reduce; reduce waits until all digest shards are merged (operator.or_).
        # Routing digest -> END left the graph without cleanup when parallel shards finished out of order.
        builder.add_edge("critique_revision_digest", "critique_revision_reduce")
        builder.add_edge("critique_revision_reduce", final_adversarial_review_node())
        builder.add_edge("adversarial_cleanup", "review_synthesizer")
        builder.add_edge("review_adjudicator", "review_synthesizer")

    builder.add_conditional_edges(
        START,
        _route_start,
        {
            "docs_prebrief": "docs_prebrief",
            "structural_extractor": "structural_extractor",
            "sandbox_structural_extractor": "sandbox_structural_extractor",
            "review_planner": "review_planner",
            "intent_extractor": "intent_extractor",
            "semantic_dispatch": "semantic_dispatch",
        },
    )
    builder.add_conditional_edges(
        "docs_prebrief",
        _route_initial_context,
        {
            "structural_extractor": "structural_extractor",
            "sandbox_structural_extractor": "sandbox_structural_extractor",
            "review_planner": "review_planner",
            "intent_extractor": "intent_extractor",
            "semantic_dispatch": "semantic_dispatch",
        },
    )
    builder.add_conditional_edges(
        "structural_extractor",
        _route_after_structural,
        {
            "semantic_dispatch": "semantic_dispatch",
            "review_planner": "review_planner",
        },
    )
    builder.add_conditional_edges(
        "sandbox_structural_extractor",
        _route_after_structural,
        {
            "semantic_dispatch": "semantic_dispatch",
            "review_planner": "review_planner",
        },
    )
    if settings.semantic_legacy_community_agents_enabled:
        builder.add_conditional_edges("semantic_dispatch", route_semantic_dispatch)
        builder.add_edge("community_semantic_agent", "semantic_dispatch")
    else:
        builder.add_edge("semantic_dispatch", "unverified_call_resolver")
    builder.add_conditional_edges(
        "unverified_call_resolver",
        route_after_unverified_call_resolver,
        {
            "unverified_call_resolver": "unverified_call_resolver",
            "semantic_merge": "semantic_merge",
        },
    )
    builder.add_conditional_edges(
        "semantic_merge",
        _route_after_semantic_merge,
        {
            "snapshot_pin": "snapshot_pin",
            "intent_extractor": "intent_extractor",
        },
    )
    builder.add_edge("intent_extractor", "review_history_context")
    builder.add_conditional_edges(
        "review_history_context",
        route_after_intent,
        {
            "mandate_explorer": "mandate_explorer",
            "mandate_patch": "mandate_patch",
            "snapshot_pin": "snapshot_pin",
        },
    )
    builder.add_edge("mandate_explorer", "mandate_patch")
    builder.add_conditional_edges(
        "mandate_patch",
        route_after_mandate_patch,
        {
            "draft_planner": "draft_planner",
            "plan_revision": "plan_revision",
        },
    )
    builder.add_edge("draft_planner", "plan_critic")
    builder.add_conditional_edges(
        "plan_critic",
        route_plan_critic,
        {
            "mandate_finalize": "mandate_finalize",
            "mandate_explorer_targeted": "mandate_explorer_targeted",
            "plan_revision": "plan_revision",
        },
    )
    builder.add_edge("mandate_explorer_targeted", "mandate_patch")
    builder.add_edge("mandate_finalize", "plan_emit")
    builder.add_edge("plan_emit", "snapshot_pin")
    snapshot_pin_routes = {
        "review_planner": "review_planner",
        "draft_planner": "draft_planner",
    }
    if settings.reviewer_use_legacy_specialist_workers:
        snapshot_pin_routes["review_synthesizer"] = "review_synthesizer"
    else:
        snapshot_pin_routes["review_evidence_triage"] = "review_evidence_triage"
    builder.add_conditional_edges(
        "snapshot_pin",
        _route_after_snapshot_pin,
        snapshot_pin_routes,
    )
    builder.add_edge("plan_revision", "plan_critic")
    builder.add_edge("review_synthesizer", END)

    if checkpointer is None:
        return builder.compile()
    return builder.compile(checkpointer=checkpointer)


def run_reviewer(state: GraphState) -> Dict[str, Any]:
    settings = get_settings()
    if not settings.redis_enabled:
        context_provider = LazyReviewContextProvider()
        try:
            graph = build_graph(context_provider=context_provider)
            return graph.invoke(state)
        finally:
            context_provider.stop()

    thread_id = state.get("run_id", "reviewer_graph")
    context_provider = LazyReviewContextProvider()
    try:
        assert_redis_checkpoint_writable(
            settings.redis_url,
            namespace=settings.redis_namespace,
        )
        with redis_checkpoint_saver(settings) as checkpointer:
            graph = build_graph(
                checkpointer=checkpointer,
                context_provider=context_provider,
            )
            return graph.invoke(
                state,
                config={"configurable": {"thread_id": thread_id}},
            )
    except Exception as exc:
        context_provider.stop()
        try:
            delete_checkpoint_thread(settings, thread_id)
        except Exception as cleanup_exc:  # noqa: BLE001
            logger.warning(
                "Checkpoint cleanup before retry failed for thread_id=%s: %s: %s",
                thread_id,
                cleanup_exc.__class__.__name__,
                cleanup_exc,
            )
        logger.warning(
            "Checkpointed reviewer run failed; retrying without checkpointing: %s: %s",
            exc.__class__.__name__,
            exc,
        )
        context_provider = LazyReviewContextProvider()
        graph = build_graph(context_provider=context_provider)
        try:
            result = graph.invoke(state)
            metadata = dict(result.get("metadata", {}))
            metadata["checkpoint_warning"] = (
                f"Checkpointed run failed; retried without checkpointing: {exc.__class__.__name__}: {exc}"
            )
            result["metadata"] = metadata
            return result
        finally:
            context_provider.stop()
    finally:
        context_provider.stop()


graph = build_graph()
