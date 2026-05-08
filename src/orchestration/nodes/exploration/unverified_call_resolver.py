"""Resolve cross-community unverified call targets after all community summaries exist."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple

from src.config import Settings, get_settings
from src.domain.interfaces import IASTParser
from src.domain.schemas import (
    CommunitySemanticSummary,
    KnowledgeGap,
    ResolverSymbolSummaryOutput,
    UnverifiedCallTarget,
)
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import extract_total_tokens_from_llm_result, parse_structured_output
from src.infrastructure.structural_graph import StructuralGraphBuilder
from src.orchestration.prompts.exploration_prompts import render_unverified_call_resolver_prompt

logger = logging.getLogger(__name__)


def _symbol_name_from_graph(graph, node_id: str) -> str:
    if not graph.has_node(node_id):
        return ""
    return str(graph.nodes[node_id].get("symbol_name") or "")


def _build_name_indexes(
    community_summaries: List[CommunitySemanticSummary],
    graph,
) -> Tuple[Dict[str, str], Dict[str, Tuple[str, str]]]:
    """Map lowercased symbol name -> symbol_node_id and -> (purpose, one-line)."""
    node_by_name: Dict[str, str] = {}
    summary_by_node: Dict[str, Tuple[str, str]] = {}
    for node_id in graph.nodes:
        if graph.nodes[node_id].get("node_type") != "symbol":
            continue
        name = _symbol_name_from_graph(graph, node_id)
        if name:
            node_by_name.setdefault(name.lower(), str(node_id))
    for cs in community_summaries:
        for sym in cs.symbol_summaries:
            summary_by_node[sym.symbol_node_id] = (sym.purpose, sym.rationale or "")
    return node_by_name, summary_by_node


def _collect_all_targets(
    state: GraphState,
) -> List[UnverifiedCallTarget]:
    rows: List[UnverifiedCallTarget] = []
    seen: Set[Tuple[str, str, int]] = set()
    for raw in state.get("unverified_call_targets", []) or []:
        if isinstance(raw, UnverifiedCallTarget):
            t = raw
        elif isinstance(raw, dict):
            try:
                t = UnverifiedCallTarget.model_validate(raw)
            except Exception:
                continue
        else:
            continue
        key = (t.source_symbol_id, t.target_name, t.source_community_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(t)
    for raw in state.get("community_summaries", []) or []:
        cs = raw if isinstance(raw, CommunitySemanticSummary) else None
        if cs is None and isinstance(raw, dict):
            try:
                cs = CommunitySemanticSummary.model_validate(raw)
            except Exception:
                cs = None
        if cs is None:
            continue
        for call in cs.unverified_calls:
            key = (call.source_symbol_id, call.target_name, cs.community_id)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                call.model_copy(
                    update={"source_community_id": call.source_community_id or cs.community_id}
                )
            )
    return rows


def make_unverified_call_resolver_node(
    *,
    ast_parser: IASTParser | None,
    settings: Settings | None = None,
    model_key: str | None = None,
    use_llm: bool = True,
):
    """Tiered resolution: summary lookup, optional AST+LLM, then permanent gaps."""

    def unverified_call_resolver_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        resolved_settings = settings or get_settings()
        graph_payload = state.get("structural_graph_node_link") or {}
        if not isinstance(graph_payload, dict):
            return {"node_history": ["unverified_call_resolver:skipped"]}

        graph = StructuralGraphBuilder.deserialize(graph_payload)
        summaries = [
            s if isinstance(s, CommunitySemanticSummary) else CommunitySemanticSummary.model_validate(s)
            for s in (state.get("community_summaries") or [])
            if s is not None
        ]
        node_by_name, summary_by_node = _build_name_indexes(summaries, graph)

        all_targets = _collect_all_targets(state)
        # Deduplicate resolution work by target_name
        by_name: Dict[str, List[UnverifiedCallTarget]] = defaultdict(list)
        for t in all_targets:
            by_name[t.target_name.lower()].append(t)

        llm_tokens = 0
        new_gaps: List[KnowledgeGap] = []
        merged: List[UnverifiedCallTarget] = []

        selected_model = model_key or resolved_settings.reviewer_worker_model_key

        for _, group in sorted(by_name.items(), key=lambda kv: kv[0]):
            representative = group[0]
            name_key = representative.target_name.lower()
            resolved_id: str | None = None
            resolution_summary: str | None = None
            resolved_flag = False

            # Tier 1: summary lookup by symbol name -> node id mapping + summaries
            node_id = node_by_name.get(name_key)
            if node_id and node_id in summary_by_node:
                purpose, rationale = summary_by_node[node_id]
                resolved_flag = True
                resolved_id = node_id
                resolution_summary = purpose if not rationale else f"{purpose} ({rationale})"

            # Tier 2: graph symbol exists but not summarized
            if not resolved_flag and node_id and ast_parser is not None:
                fp = str(graph.nodes[node_id].get("file_path") or "")
                entity_name = _symbol_name_from_graph(graph, node_id)
                if fp and entity_name and use_llm:
                    repo = str(state.get("repo_path", "") or "")
                    try:
                        body = ast_parser.get_entity_details(repo, fp, entity_name)
                        body_text = body.body if body is not None else ""
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("resolver ast read failed run_id=%s sym=%s err=%s", run_id, entity_name, exc)
                        body_text = ""
                    if body_text:
                        try:
                            llm = Models.worker(ResolverSymbolSummaryOutput, model_key=selected_model)
                            prompt = render_unverified_call_resolver_prompt(
                                symbol_node_id=node_id,
                                body_text=body_text,
                            )
                            invoke_result = llm.invoke(prompt)
                            parsed = parse_structured_output(invoke_result, ResolverSymbolSummaryOutput)
                            llm_tokens += extract_total_tokens_from_llm_result(invoke_result)
                            resolved_flag = True
                            resolved_id = node_id
                            resolution_summary = parsed.one_line_summary
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("resolver LLM failed run_id=%s sym=%s err=%s", run_id, entity_name, exc)

            # Tier 3: unresolved external
            if not resolved_flag:
                new_gaps.append(
                    KnowledgeGap(
                        gap_type="unverified_call",
                        description=f"Could not resolve callee `{representative.target_name}`.",
                        affected_node_ids=[representative.source_symbol_id],
                        community_id=representative.source_community_id,
                        severity="medium",
                    )
                )

            for t in group:
                merged.append(
                    t.model_copy(
                        update={
                            "resolved": resolved_flag,
                            "resolved_target_id": resolved_id,
                            "resolution_summary": resolution_summary,
                        }
                    )
                )

        meta = dict(state.get("metadata", {}))
        sp2 = dict(meta.get("semantic_phase2", {}))
        sp2["resolver"] = {
            "unique_target_names": len(by_name),
            "total_rows": len(merged),
        }
        meta["semantic_phase2"] = sp2

        return {
            "resolved_unverified_calls": merged,
            "knowledge_gaps": new_gaps,
            "metadata": meta,
            "node_history": ["unverified_call_resolver"],
            "token_usage": llm_tokens,
        }

    return unverified_call_resolver_node


def route_after_unverified_call_resolver(state: GraphState) -> str:
    """Optional self-loop (disabled by default until new targets are surfaced)."""
    meta = (state.get("metadata") or {}).get("semantic_phase2", {})
    if meta.get("resolver_continue"):
        return "unverified_call_resolver"
    return "semantic_merge"
