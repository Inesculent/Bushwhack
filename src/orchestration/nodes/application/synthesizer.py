from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.domain.schemas import ReviewFinding
from src.domain.state import GraphState
from src.orchestration.routing.finding_dedupe import ensure_unique_finding_ids
from src.orchestration.routing.review_obligations import recall_audit_for_final_findings

trace_logger = logging.getLogger("research_pipeline.reviewer_trace")


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _merge_duplicate_maps(*maps: Any) -> Dict[str, List[str]]:
    merged: Dict[str, List[str]] = {}
    for raw_map in maps:
        if not isinstance(raw_map, dict):
            continue
        for raw_keeper, raw_ids in raw_map.items():
            keeper = str(raw_keeper)
            if not isinstance(raw_ids, list):
                continue
            for raw_id in raw_ids:
                dropped = str(raw_id)
                if dropped == keeper:
                    continue
                merged.setdefault(keeper, [])
                if dropped not in merged[keeper]:
                    merged[keeper].append(dropped)
    return merged


def _canonicalize_duplicate_map(
    duplicate_map: Dict[str, List[str]],
    final_ids: set[str],
) -> Dict[str, List[str]]:
    parent: Dict[str, str] = {}
    for keeper, dropped_ids in duplicate_map.items():
        for dropped in dropped_ids:
            if dropped != keeper:
                parent[dropped] = keeper

    def resolve(item: str) -> str:
        seen: set[str] = set()
        current = item
        while current in parent and current not in seen:
            seen.add(current)
            next_item = parent[current]
            if next_item in final_ids:
                return next_item
            current = next_item
        for candidate in seen:
            if candidate in final_ids:
                return candidate
        return current

    canonical: Dict[str, List[str]] = {}
    for dropped in sorted(parent):
        keeper = resolve(dropped)
        if keeper == dropped:
            continue
        canonical.setdefault(keeper, [])
        if dropped not in canonical[keeper]:
            canonical[keeper].append(dropped)
    return canonical


def synthesizer_node(state: GraphState) -> Dict[str, Any]:
    findings = state.get("findings", []) or []

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    sorted_findings = sorted(
        findings,
        key=lambda item: (
            severity_rank.get(item.severity, 99),
            item.file_path,
            item.line_start,
            item.id,
        ),
    )
    final = ensure_unique_finding_ids(sorted_findings)

    reports = state.get("reviewer_worker_reports", []) or []
    reflection_reports = state.get("reflection_reports", []) or []
    candidates = state.get("candidate_findings", []) or []
    metadata = dict(state.get("metadata", {}))
    coverage_by_task = {}
    pipe = metadata.get("critique_pipeline") if isinstance(metadata.get("critique_pipeline"), dict) else {}
    by_task = pipe.get("by_task") if isinstance(pipe.get("by_task"), dict) else {}
    for task_id, slot in by_task.items():
        if isinstance(slot, dict) and isinstance(slot.get("coverage_evaluation"), dict):
            coverage_by_task[str(task_id)] = slot["coverage_evaluation"]
    cleanup_meta = metadata.get("adversarial_cleanup")
    adjudicator_meta = metadata.get("review_adjudicator")
    lifecycle = cleanup_meta.get("candidate_lifecycle", {}) if isinstance(cleanup_meta, dict) else {}
    adjudicator_lifecycle = (
        adjudicator_meta.get("candidate_lifecycle", {})
        if isinstance(adjudicator_meta, dict)
        else {}
    )
    if adjudicator_lifecycle:
        lifecycle = adjudicator_lifecycle
    cleanup_candidate_duplicates = (
        cleanup_meta.get("semantic_dedupe_duplicates", {}) if isinstance(cleanup_meta, dict) else {}
    )
    cleanup_finding_duplicates = (
        cleanup_meta.get("semantic_dedupe_finding_duplicates", {})
        if isinstance(cleanup_meta, dict)
        else {}
    )
    cleanup_claim_cluster_duplicates = (
        cleanup_meta.get("semantic_claim_cluster_duplicates", {})
        if isinstance(cleanup_meta, dict)
        else {}
    )
    adjudicator_merge_map = (
        adjudicator_meta.get("merge_map", {}) if isinstance(adjudicator_meta, dict) else {}
    )
    raw_combined_duplicate_map = _merge_duplicate_maps(
        cleanup_candidate_duplicates,
        cleanup_finding_duplicates,
        cleanup_claim_cluster_duplicates,
        adjudicator_merge_map,
    )
    promoted_candidate_ids = {
        str(candidate_id)
        for candidate_id, entry in lifecycle.items()
        if isinstance(entry, dict) and entry.get("decision") == "promoted"
    }
    final_ids = {finding.id for finding in final}
    combined_duplicate_map = _canonicalize_duplicate_map(raw_combined_duplicate_map, final_ids)
    dropped_duplicate_ids = sorted(
        {
            dropped
            for dropped_ids in combined_duplicate_map.values()
            for dropped in dropped_ids
        }
    )
    equivalent_keeper = {
        str(dropped): str(keeper)
        for keeper, ids in combined_duplicate_map.items()
        for dropped in ids
        if str(dropped) != str(keeper)
    }
    lost_promoted_ids = sorted(
        cid
        for cid in promoted_candidate_ids
        if cid not in final_ids and equivalent_keeper.get(cid) not in final_ids
    )
    metadata["review_synthesizer"] = {
        "worker_count": len(reports),
        "reflection_report_count": len(reflection_reports),
        "raw_finding_count": len(findings),
        "final_finding_count": len(final),
        "dropped_duplicate_ids": dropped_duplicate_ids,
        "semantic_dedupe_duplicates": combined_duplicate_map,
        "dropped_resolution_only_ids": [],
        "lost_promoted_candidate_ids": lost_promoted_ids,
        "recall_audit": recall_audit_for_final_findings(
            obligations_by_task=coverage_by_task,
            candidates=candidates,
            final_findings=final,
            duplicate_map=combined_duplicate_map,
        ),
        "worker_reports": [report.model_dump() for report in reports],
        "reflection_reports": [
            r.model_dump() if hasattr(r, "model_dump") else r for r in reflection_reports
        ],
    }

    if _trace_enabled(state):
        trace_logger.info(
            "TRACE synthesizer run_id=%s workers=%s reflections=%s raw_findings=%s final_findings=%s dropped_duplicates=%s",
            state.get("run_id", "unknown"),
            len(reports),
            len(reflection_reports),
            len(findings),
            len(final),
            dropped_duplicate_ids,
        )
        for finding in final:
            trace_logger.info(
                "TRACE final_finding run_id=%s id=%s severity=%s file=%s lines=%s-%s",
                state.get("run_id", "unknown"),
                finding.id,
                finding.severity,
                finding.file_path,
                finding.line_start,
                finding.line_end,
            )

    return {
        "final_findings": final,
        "metadata": metadata,
        "node_history": ["review_synthesizer"],
        "next_step": "finalize",
    }
