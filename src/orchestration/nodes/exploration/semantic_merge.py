"""Global semantic synthesis and deterministic diagnostics (Phase 2)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.config import Settings, get_settings
from src.domain.schemas import (
    CommunitySemanticSummary,
    GlobalSemanticSynthesisOutput,
    KnowledgeGap,
    StructuralTopologySummary,
)
from src.domain.state import GraphState
from src.infrastructure.graph_diagnostics import build_snapshot_diagnostics
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import extract_total_tokens_from_llm_result, parse_structured_output
from src.infrastructure.semantic_annotator import SemanticGraphAnnotator
from src.infrastructure.structural_graph import StructuralGraphBuilder
from src.orchestration.prompts.exploration_prompts import render_semantic_merge_prompt

logger = logging.getLogger(__name__)


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
        global_summary = "Semantic enrichment disabled."
        if use_llm and summaries:
            prompt = render_semantic_merge_prompt(summaries)
            try:
                selected = model_key or resolved_settings.semantic_merge_model_key
                llm = Models.synthesizer(GlobalSemanticSynthesisOutput, model_key=selected)
                invoke_result = llm.invoke(prompt)
                parsed = parse_structured_output(invoke_result, GlobalSemanticSynthesisOutput)
                global_summary = parsed.global_summary or global_summary
                llm_tokens += extract_total_tokens_from_llm_result(invoke_result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("semantic_merge synthesis failed: %s", exc)
                global_summary = f"Global synthesis failed: {exc.__class__.__name__}"

        enriched_payload = StructuralGraphBuilder.serialize(graph)
        meta = dict(state.get("metadata", {}))
        meta["snapshot_diagnostics"] = diagnostics.model_dump(mode="json")

        return {
            "structural_graph_node_link": enriched_payload,
            "global_summary": global_summary,
            "metadata": meta,
            "node_history": ["semantic_merge"],
            "token_usage": llm_tokens,
        }

    return semantic_merge_node
