from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.domain.schemas import ReviewFinding
from src.domain.state import GraphState
from src.orchestration.routing.finding_dedupe import (
    dedupe_review_findings_by_signature,
    ensure_unique_finding_ids,
    is_resolution_only_finding,
)
from src.orchestration.routing.review_obligations import recall_audit_for_final_findings

trace_logger = logging.getLogger("research_pipeline.reviewer_trace")


def _finding_passes_quality_gate(finding: ReviewFinding) -> bool:
    return not is_resolution_only_finding(
        finding.content,
        finding.recommendation or "",
    )


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def synthesizer_node(state: GraphState) -> Dict[str, Any]:
    findings = state.get("findings", []) or []
    dropped_resolution_ids: List[str] = []
    passed_gate: List[ReviewFinding] = []

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    for finding in sorted(
        findings,
        key=lambda item: (
            severity_rank.get(item.severity, 99),
            item.file_path,
            item.line_start,
            item.id,
        ),
    ):
        if not _finding_passes_quality_gate(finding):
            dropped_resolution_ids.append(finding.id)
            continue
        passed_gate.append(finding)

    passed_gate = ensure_unique_finding_ids(passed_gate)
    deduped, duplicate_map = dedupe_review_findings_by_signature(passed_gate)
    deduped = ensure_unique_finding_ids(deduped)
    dropped_ids = [fid for ids in duplicate_map.values() for fid in ids]

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
    lifecycle = cleanup_meta.get("candidate_lifecycle", {}) if isinstance(cleanup_meta, dict) else {}
    promoted_candidate_ids = {
        str(candidate_id)
        for candidate_id, entry in lifecycle.items()
        if isinstance(entry, dict) and entry.get("decision") == "promoted"
    }
    final_ids = {finding.id for finding in deduped}
    equivalent_keeper = {
        str(dropped): str(keeper)
        for keeper, ids in duplicate_map.items()
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
        "final_finding_count": len(deduped),
        "dropped_duplicate_ids": dropped_ids,
        "semantic_dedupe_duplicates": duplicate_map,
        "dropped_resolution_only_ids": dropped_resolution_ids,
        "lost_promoted_candidate_ids": lost_promoted_ids,
        "recall_audit": recall_audit_for_final_findings(
            obligations_by_task=coverage_by_task,
            candidates=candidates,
            final_findings=deduped,
            duplicate_map=duplicate_map,
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
            len(deduped),
            dropped_ids,
        )
        for finding in deduped:
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
        "final_findings": deduped,
        "metadata": metadata,
        "node_history": ["review_synthesizer"],
        "next_step": "finalize",
    }
