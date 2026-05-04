from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.domain.schemas import ReviewFinding
from src.domain.state import GraphState

trace_logger = logging.getLogger("research_pipeline.reviewer_trace")


def _finding_key(finding: ReviewFinding) -> tuple[str, int, int, str]:
    normalized_content = " ".join(finding.content.lower().split())[:160]
    return (finding.file_path, finding.line_start, finding.line_end, normalized_content)


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def synthesizer_node(state: GraphState) -> Dict[str, Any]:
    findings = state.get("findings", []) or []
    deduped: List[ReviewFinding] = []
    seen: set[tuple[str, int, int, str]] = set()
    dropped_ids: List[str] = []

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
        key = _finding_key(finding)
        if key in seen:
            dropped_ids.append(finding.id)
            continue
        seen.add(key)
        deduped.append(finding)

    reports = state.get("reviewer_worker_reports", []) or []
    reflection_reports = state.get("reflection_reports", []) or []
    metadata = dict(state.get("metadata", {}))
    metadata["review_synthesizer"] = {
        "worker_count": len(reports),
        "reflection_report_count": len(reflection_reports),
        "raw_finding_count": len(findings),
        "final_finding_count": len(deduped),
        "dropped_duplicate_ids": dropped_ids,
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
