"""Global semantic synthesis and deterministic diagnostics (Phase 2)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.config import Settings, get_settings
from src.domain.schemas import (
    CommunitySemanticSummary,
    GlobalSemanticSynthesisOutput,
    KnowledgeGap,
    ReviewKBRecord,
    StructuralTopologySummary,
)
from src.domain.state import GraphState
from src.infrastructure.graph_diagnostics import build_snapshot_diagnostics
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.local_status import (
    is_local_model,
    is_timeout_exception,
    local_llm_server_active,
    sleep_for_retry,
)
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import trace_from_exception, trace_llm_call
from src.infrastructure.semantic_annotator import SemanticGraphAnnotator
from src.infrastructure.structural_graph import StructuralGraphBuilder
from src.orchestration.prompts.exploration_prompts import (
    render_semantic_merge_from_kb_prompt,
    render_semantic_merge_prompt,
)

logger = logging.getLogger(__name__)

_SEMANTIC_MERGE_COMPACT_RETRY_APPENDIX = (
    "\n\n## OUTPUT BUDGET (retry - required)\n"
    "Return the smallest valid JSON only. Keep global_summary under 180 words. "
    "Mention major subsystems, coverage gaps, and where reviewers should query exact KB/source evidence."
)


def _deterministic_global_summary(
    *,
    repo_summary: ReviewKBRecord | None,
    summaries: List[CommunitySemanticSummary],
    kb_summary_records: List[ReviewKBRecord],
    diagnostics: Any,
    metadata: Dict[str, Any],
) -> str:
    if repo_summary is not None and repo_summary.summary:
        base = repo_summary.summary
    elif summaries:
        top = sorted(summaries, key=lambda s: s.community_id)[:8]
        rendered = "; ".join(f"community {s.community_id}: {s.label or s.purpose[:80]}" for s in top)
        base = f"Repository understanding is available from {len(summaries)} community summaries. {rendered}"
    else:
        base = "Repository understanding is available from deterministic structural graph and Repository KB records."

    sp2 = metadata.get("semantic_phase2", {}) if isinstance(metadata.get("semantic_phase2"), dict) else {}
    review_kb = sp2.get("review_kb") if isinstance(sp2, dict) else {}
    coverage = dict(review_kb.get("distillation_coverage") or {}) if isinstance(review_kb, dict) else {}
    if not coverage:
        coverage = {
            "summary_records": len(kb_summary_records),
            "community_summaries": len(summaries),
        }
    gap_count = len(getattr(diagnostics, "knowledge_gaps", []) or [])
    return (
        f"{base}\n\n"
        f"Semantic coverage: {coverage}. Structural diagnostics report {gap_count} knowledge gaps. "
        "Use Repository KB queries, AST/source slices, focused context, and verifier output for exact proof."
    )


def _invoke_global_synthesis(
    *,
    prompt: str,
    settings: Settings,
    model_key: str,
    state: GraphState,
) -> tuple[object, bool, int, List[Dict[str, Any]]]:
    llm = Models.synthesizer(
        GlobalSemanticSynthesisOutput,
        model_key=model_key,
        max_completion_tokens=settings.semantic_merge_max_completion_tokens,
    )
    try:
        traced = trace_llm_call(
            llm,
            prompt,
            state=state,
            node_name="semantic_merge",
            model_key=model_key,
            schema_name="GlobalSemanticSynthesisOutput",
            request_label="primary",
        )
        return traced.result, False, traced.tokens, traced.trace_records
    except Exception as exc:
        if not (is_timeout_exception(exc) and is_local_model(model_key)):
            raise
        llm_trace = trace_from_exception(exc)
        active, _status = local_llm_server_active(settings)
        if not active:
            raise
        sleep_for_retry(settings.semantic_agent_retry_backoff_seconds, 1)
        traced = trace_llm_call(
            llm,
            prompt + _SEMANTIC_MERGE_COMPACT_RETRY_APPENDIX,
            state=state,
            node_name="semantic_merge",
            model_key=model_key,
            schema_name="GlobalSemanticSynthesisOutput",
            request_label="compact_retry",
        )
        return traced.result, True, traced.tokens, llm_trace + traced.trace_records


def make_semantic_merge_node(
    *,
    settings: Settings | None = None,
    model_key: str | None = None,
    use_llm: bool = True,
):
    """Global synthesis + deterministic diagnostics + graph annotation."""

    def semantic_merge_node(state: GraphState) -> Dict[str, Any]:
        resolved_settings = settings or get_settings()
        topo = state.get("structural_topology")
        if not isinstance(topo, StructuralTopologySummary):
            topo = StructuralTopologySummary.model_validate(topo) if topo else None
        graph_payload = dict(state.get("structural_graph_node_link") or {})
        graph = StructuralGraphBuilder.deserialize(graph_payload)

        summaries = [
            s if isinstance(s, CommunitySemanticSummary) else CommunitySemanticSummary.model_validate(s)
            for s in (state.get("community_summaries") or [])
        ]

        annotator = SemanticGraphAnnotator()
        for cs in summaries:
            for fs in cs.file_summaries:
                if graph.has_node(fs.file_node_id):
                    annotator.annotate_node(
                        graph,
                        fs.file_node_id,
                        purpose=fs.purpose,
                        confidence=fs.confidence,
                    )
            for sym in cs.symbol_summaries:
                if graph.has_node(sym.symbol_node_id):
                    annotator.annotate_node(
                        graph,
                        sym.symbol_node_id,
                        purpose=sym.purpose,
                        rationale=sym.rationale,
                        confidence=sym.confidence,
                    )

        partition = dict(topo.node_to_community) if topo is not None else {}
        diagnostics = build_snapshot_diagnostics(
            graph,
            partition,
            topo.communities if topo is not None else [],
            god_top_n=resolved_settings.diagnostics_god_nodes_top_n,
            bridge_top_n=resolved_settings.diagnostics_bridge_nodes_top_n,
            cross_edge_top_n=resolved_settings.diagnostics_cross_community_edges_top_n,
            low_cohesion_threshold=resolved_settings.diagnostics_low_cohesion_threshold,
        )

        merged_gaps: List[KnowledgeGap] = list(diagnostics.knowledge_gaps)
        for gap in state.get("knowledge_gaps", []) or []:
            g = gap if isinstance(gap, KnowledgeGap) else KnowledgeGap.model_validate(gap)
            merged_gaps.append(g)
        diagnostics = diagnostics.model_copy(update={"knowledge_gaps": merged_gaps[:500]})

        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []
        meta = dict(state.get("metadata", {}))
        global_summary = "Semantic enrichment disabled."
        kb_summary_records: List[ReviewKBRecord] = []
        for raw in state.get("repository_kb_summary_records") or []:
            try:
                record = raw if isinstance(raw, ReviewKBRecord) else ReviewKBRecord.model_validate(raw)
            except Exception:
                continue
            if record.kind == "summary":
                kb_summary_records.append(record)

        repo_summary = next((r for r in kb_summary_records if r.id == "summary:repo"), None)
        global_summary = _deterministic_global_summary(
            repo_summary=repo_summary,
            summaries=summaries,
            kb_summary_records=kb_summary_records,
            diagnostics=diagnostics,
            metadata=meta,
        )

        should_synthesize = use_llm and (kb_summary_records or summaries)
        if repo_summary is not None and repo_summary.confidence == "llm_synthesized":
            should_synthesize = resolved_settings.repository_kb_distillation_mode == "full"

        if should_synthesize:
            prompt = (
                render_semantic_merge_from_kb_prompt(
                    kb_summary_records,
                    max_chars=resolved_settings.semantic_merge_max_prompt_chars,
                )
                if kb_summary_records
                else render_semantic_merge_prompt(summaries)
            )
            try:
                selected = model_key or resolved_settings.semantic_merge_model_key
                invoke_result, retried, call_tokens, call_trace = _invoke_global_synthesis(
                    prompt=prompt,
                    settings=resolved_settings,
                    model_key=selected,
                    state=state,
                )
                llm_trace.extend(call_trace)
                parsed = parse_structured_output(invoke_result, GlobalSemanticSynthesisOutput)
                if parsed.global_summary:
                    global_summary = parsed.global_summary
                llm_tokens += call_tokens
                sp2 = dict(meta.get("semantic_phase2", {}))
                sp2["global_summary_llm_status"] = "ok_retry" if retried else "ok"
                meta["semantic_phase2"] = sp2
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                logger.warning("semantic_merge synthesis failed: %s", exc)
                sp2 = dict(meta.get("semantic_phase2", {}))
                sp2["global_summary_llm_status"] = (
                    "timeout_failed" if is_timeout_exception(exc) else f"failed:{exc.__class__.__name__}"
                )
                meta["semantic_phase2"] = sp2

        enriched_payload = StructuralGraphBuilder.serialize(graph)
        meta["snapshot_diagnostics"] = diagnostics.model_dump(mode="json")

        return {
            "structural_graph_node_link": enriched_payload,
            "global_summary": global_summary,
            "metadata": meta,
            "node_history": ["semantic_merge"],
            "token_usage": llm_tokens,
            "llm_trace": llm_trace,
        }

    return semantic_merge_node
