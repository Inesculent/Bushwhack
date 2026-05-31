"""Global semantic synthesis and deterministic diagnostics (Phase 2)."""

from __future__ import annotations

import logging
from collections import Counter
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

_KB_INTELLIGENCE_PROFILES: Dict[str, Dict[str, int]] = {
    "lean": {"chars": 6000, "boundary": 6, "community": 4, "shard": 0},
    "standard": {"chars": 12000, "boundary": 12, "community": 8, "shard": 0},
    "deep": {"chars": 32000, "boundary": 32, "community": 24, "shard": 8},
    "offline": {"chars": 120000, "boundary": 96, "community": 96, "shard": 48},
}

_SEMANTIC_MERGE_COMPACT_RETRY_APPENDIX = (
    "\n\n## OUTPUT BUDGET (retry - required)\n"
    "Return the smallest valid JSON only. Keep global_summary under 180 words. "
    "Mention major subsystems, coverage gaps, and where reviewers should query exact KB/source evidence."
)


def _global_topology_appendix(
    *,
    kb_summary_records: List[ReviewKBRecord],
    diagnostics: Any,
    metadata: Dict[str, Any],
) -> str:
    sp2 = metadata.get("semantic_phase2", {}) if isinstance(metadata.get("semantic_phase2"), dict) else {}
    review_kb = sp2.get("review_kb") if isinstance(sp2, dict) else {}
    coverage = dict(review_kb.get("distillation_coverage") or {}) if isinstance(review_kb, dict) else {}
    if not coverage:
        coverage = {"summary_records": len(kb_summary_records)}
    gap_count = len(getattr(diagnostics, "knowledge_gaps", []) or [])
    topology_summary = next((r for r in kb_summary_records if r.id == "summary:repo:topology"), None)
    if topology_summary is not None and topology_summary.summary:
        topology = topology_summary.summary
    else:
        topology = "Static topology is available from graph diagnostics."
    return f"{topology}\nSemantic coverage: {coverage}. Structural diagnostics report {gap_count} knowledge gaps."


def _format_global_summary_sections(
    *,
    understanding: str,
    topology_appendix: str,
) -> str:
    if "Repository Understanding" in understanding and "Static Topology And Coverage" in understanding:
        return understanding
    return "\n\n".join(
        [
            "Repository Understanding\n" + understanding.strip(),
            "Operating Model / Core Workflows\n"
            "Use the repository understanding and community summaries for high-level workflow context; "
            "query exact KB facts, symbols, and source slices for implementation proof.",
            "Review Mental Model\n"
            "Treat Repository KB summaries as navigation and contract context. Exact source, AST slices, "
            "focused context, and verifier output outrank summary prose.",
            "Static Topology And Coverage\n" + topology_appendix,
        ]
    )


def _deterministic_global_summary(
    *,
    repo_summary: ReviewKBRecord | None,
    summaries: List[CommunitySemanticSummary],
    kb_summary_records: List[ReviewKBRecord],
    diagnostics: Any,
    metadata: Dict[str, Any],
) -> str:
    if repo_summary is not None and repo_summary.summary and repo_summary.confidence == "llm_synthesized":
        understanding = repo_summary.summary
    elif repo_summary is not None and repo_summary.summary:
        understanding = (
            "KB-backed repository understanding has not been synthesized yet. "
            "Use the deterministic topology, community summaries, Repository KB queries, and source slices for navigation."
        )
    elif summaries:
        top = sorted(summaries, key=lambda s: s.community_id)[:8]
        rendered = "; ".join(f"community {s.community_id}: {s.label or s.purpose[:80]}" for s in top)
        understanding = f"Repository understanding is available from {len(summaries)} community summaries. {rendered}"
    else:
        understanding = "Repository understanding is available from deterministic structural graph and Repository KB records."

    return _format_global_summary_sections(
        understanding=understanding,
        topology_appendix=_global_topology_appendix(
            kb_summary_records=kb_summary_records,
            diagnostics=diagnostics,
            metadata=metadata,
        ),
    )


def _metadata_int(record: ReviewKBRecord, key: str, default: int = -1) -> int:
    value = record.metadata.get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _profile_limits(settings: Settings) -> Dict[str, int]:
    profile = str(getattr(settings, "repository_kb_intelligence_profile", "standard") or "standard")
    limits = dict(_KB_INTELLIGENCE_PROFILES.get(profile, _KB_INTELLIGENCE_PROFILES["standard"]))
    limits["chars"] = min(int(settings.semantic_merge_max_prompt_chars), int(limits["chars"]))
    if settings.repository_kb_distillation_mode == "full" and profile in {"lean", "standard"}:
        limits["boundary"] *= 2
        limits["community"] *= 2
        limits["shard"] = max(limits["shard"], 8)
    return limits


def _record_scope(record: ReviewKBRecord) -> str:
    return str(record.metadata.get("summary_scope") or "unknown")


def _record_selected_reason(record: ReviewKBRecord) -> str:
    scope = _record_scope(record)
    if record.id == "summary:repo":
        return "repo_orientation"
    if record.id == "summary:repo:topology":
        return "topology_coverage"
    if scope == "boundary":
        return "boundary_contract_cascade"
    if scope == "community":
        return "central_community_orientation"
    if scope == "community_shard":
        return "full_mode_shard_detail"
    return "semantic_summary"


def _summary_line(record: ReviewKBRecord) -> str:
    meta = record.metadata
    scope = _record_scope(record)
    parts = [
        f"id={record.id}",
        f"scope={scope}",
        f"confidence={record.confidence}",
    ]
    for key in ("boundary_scope", "community_id", "file_path", "fan_in", "fan_out"):
        value = meta.get(key)
        if value not in (None, "", []):
            parts.append(f"{key}={value}")
    deps = meta.get("dependency_community_ids")
    if isinstance(deps, list) and deps:
        parts.append("deps=" + ",".join(str(x) for x in deps[:8]))
    selected_tags = [tag for tag in record.tags if tag in {"boundary", "contract", "signature", "cascade", "risk-surface"}]
    if selected_tags:
        parts.append("tags=" + ",".join(selected_tags[:8]))
    if scope == "boundary":
        text_limit = 650
    elif record.id.startswith("summary:repo"):
        text_limit = 750
    elif scope == "community_shard":
        text_limit = 260
    else:
        text_limit = 420
    return f"- {'; '.join(parts)}: {record.summary[:text_limit]}"


def _record_score(record: ReviewKBRecord, dependency_communities: set[int]) -> int:
    scope = _record_scope(record)
    if record.id == "summary:repo":
        return 10000
    if record.id == "summary:repo:topology":
        return 9500

    score = 0
    if scope == "boundary":
        score += 5000
        if record.metadata.get("boundary_scope") == "file":
            score += 750
        if record.metadata.get("file_path"):
            score += 300
        score += min(600, _metadata_int(record, "fan_in", 0) * 20)
        score += min(600, _metadata_int(record, "fan_out", 0) * 20)
        cross = record.metadata.get("cross_community_edge_ids") or []
        if isinstance(cross, list):
            score += min(500, len(cross) * 50)
    elif scope == "community":
        score += 1200
        if _metadata_int(record, "community_id") in dependency_communities:
            score += 700
    elif scope == "community_shard":
        score += 350

    tags = {str(tag).lower() for tag in record.tags}
    for tag, weight in (
        ("boundary", 500),
        ("contract", 450),
        ("signature", 350),
        ("cascade", 350),
        ("risk-surface", 300),
        ("api", 250),
        ("config", 200),
    ):
        if tag in tags:
            score += weight

    for key in ("retrieval_count", "retrieval_hits", "query_hits"):
        score += min(250, _metadata_int(record, key, 0) * 25)
    return score


def _semantic_merge_packet(
    *,
    records: List[ReviewKBRecord],
    settings: Settings,
) -> tuple[List[ReviewKBRecord], Dict[str, Any]]:
    limits = _profile_limits(settings)
    mode = settings.repository_kb_distillation_mode
    include_shards = mode == "full" or str(settings.repository_kb_intelligence_profile) in {"deep", "offline"}
    dependency_communities: set[int] = set()
    for record in records:
        if _record_scope(record) != "boundary":
            continue
        deps = record.metadata.get("dependency_community_ids") or []
        if isinstance(deps, list):
            for dep in deps:
                try:
                    dependency_communities.add(int(dep))
                except (TypeError, ValueError):
                    continue

    required_ids = {"summary:repo", "summary:repo:topology"}
    required = [r for r in records if r.id in required_ids]
    candidates = [r for r in records if r.id not in required_ids]
    prefiltered_scope_counts: Counter[str] = Counter()
    if not include_shards:
        prefiltered_scope_counts.update(_record_scope(r) for r in candidates if _record_scope(r) == "community_shard")
        candidates = [r for r in candidates if _record_scope(r) != "community_shard"]

    candidates = sorted(candidates, key=lambda r: (-_record_score(r, dependency_communities), r.id))
    selected: List[ReviewKBRecord] = []
    selected_ids: set[str] = set()
    selected_scope_counts: Counter[str] = Counter()
    omitted_scope_counts: Counter[str] = Counter()
    prompt_chars = 0

    def try_add(record: ReviewKBRecord) -> bool:
        nonlocal prompt_chars
        scope = _record_scope(record)
        if record.id in selected_ids:
            return True
        if scope == "boundary" and selected_scope_counts[scope] >= limits["boundary"]:
            omitted_scope_counts[scope] += 1
            return False
        if scope == "community" and selected_scope_counts[scope] >= limits["community"]:
            omitted_scope_counts[scope] += 1
            return False
        if scope == "community_shard" and selected_scope_counts[scope] >= limits["shard"]:
            omitted_scope_counts[scope] += 1
            return False
        line_len = len(_summary_line(record)) + 1
        if selected and prompt_chars + line_len > limits["chars"]:
            omitted_scope_counts[scope] += 1
            return False
        selected.append(record)
        selected_ids.add(record.id)
        selected_scope_counts[scope] += 1
        prompt_chars += line_len
        return True

    for record in sorted(required, key=lambda r: 0 if r.id == "summary:repo" else 1):
        try_add(record)
    for record in candidates:
        try_add(record)

    diagnostics = {
        "profile": str(settings.repository_kb_intelligence_profile),
        "mode": mode,
        "target_prompt_chars": limits["chars"],
        "selected_record_count": len(selected),
        "selected_scope_counts": dict(selected_scope_counts),
        "omitted_scope_counts": dict(omitted_scope_counts + prefiltered_scope_counts),
        "selected_community_ids": sorted(
            {
                _metadata_int(record, "community_id")
                for record in selected
                if _metadata_int(record, "community_id") >= 0
            }
        )[:32],
        "selected_reasons": dict(Counter(_record_selected_reason(record) for record in selected)),
        "include_shards": include_shards,
        "estimated_prompt_chars": prompt_chars,
    }
    return selected, diagnostics


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

        semantic_packet: List[ReviewKBRecord] = []
        packet_diagnostics: Dict[str, Any] = {}
        if kb_summary_records:
            semantic_packet, packet_diagnostics = _semantic_merge_packet(
                records=kb_summary_records,
                settings=resolved_settings,
            )
            sp2 = dict(meta.get("semantic_phase2", {}))
            sp2["global_summary_intelligence"] = packet_diagnostics
            meta["semantic_phase2"] = sp2

        should_synthesize = (
            use_llm
            and resolved_settings.repository_kb_distillation_mode != "off"
            and ((kb_summary_records and semantic_packet) or summaries)
        )
        if repo_summary is not None and repo_summary.confidence == "llm_synthesized":
            should_synthesize = resolved_settings.repository_kb_distillation_mode == "full"

        if should_synthesize:
            prompt = (
                render_semantic_merge_from_kb_prompt(
                    semantic_packet,
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
                    global_summary = _format_global_summary_sections(
                        understanding=parsed.global_summary,
                        topology_appendix=_global_topology_appendix(
                            kb_summary_records=kb_summary_records,
                            diagnostics=diagnostics,
                            metadata=meta,
                        ),
                    )
                llm_tokens += call_tokens
                sp2 = dict(meta.get("semantic_phase2", {}))
                sp2["global_summary_llm_status"] = "ok_retry" if retried else "ok"
                sp2["semantic_merge_tokens_used"] = call_tokens
                meta["semantic_phase2"] = sp2
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                logger.warning("semantic_merge synthesis failed: %s", exc)
                sp2 = dict(meta.get("semantic_phase2", {}))
                sp2["global_summary_llm_status"] = (
                    "timeout_failed" if is_timeout_exception(exc) else f"failed:{exc.__class__.__name__}"
                )
                sp2["semantic_merge_tokens_used"] = 0
                meta["semantic_phase2"] = sp2
        else:
            sp2 = dict(meta.get("semantic_phase2", {}))
            if not use_llm:
                sp2["global_summary_llm_status"] = "disabled"
            elif resolved_settings.repository_kb_distillation_mode == "off":
                sp2["global_summary_llm_status"] = "disabled:repository_kb_off"
            elif kb_summary_records and not semantic_packet:
                sp2["global_summary_llm_status"] = "skipped_no_packet"
            elif repo_summary is not None and repo_summary.confidence == "llm_synthesized":
                sp2["global_summary_llm_status"] = "reused_repo_summary"
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
