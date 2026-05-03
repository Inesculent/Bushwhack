from __future__ import annotations

from typing import Any, Dict, List

from src.domain.schemas import ReviewFinding
from src.domain.state import GraphState


def _finding_key(finding: ReviewFinding) -> tuple[str, int, int, str]:
    normalized_content = " ".join(finding.content.lower().split())[:160]
    return (finding.file_path, finding.line_start, finding.line_end, normalized_content)


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
    metadata = dict(state.get("metadata", {}))
    metadata["review_synthesizer"] = {
        "worker_count": len(reports),
        "raw_finding_count": len(findings),
        "final_finding_count": len(deduped),
        "dropped_duplicate_ids": dropped_ids,
        "worker_reports": [report.model_dump() for report in reports],
    }

    return {
        "final_findings": deduped,
        "metadata": metadata,
        "node_history": ["review_synthesizer"],
        "next_step": "finalize",
    }
