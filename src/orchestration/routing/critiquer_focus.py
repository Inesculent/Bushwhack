"""Deterministic focused-context requests derived from critiquer candidates (no LLM)."""

from __future__ import annotations

from typing import List

from src.domain.schemas import CandidateFinding, FocusedContextRequest, ReviewTask
from src.orchestration.context.focused_query_sanitize import sanitize_text_query
from src.orchestration.routing.finding_dedupe import extract_subject_class

HIGH_RISK_CONTEXT_TERMS = (
    "auth",
    "authorization",
    "permission",
    "tenant",
    "injection",
    "sql",
    "delete",
    "unsafe",
    "caller",
    "contract",
    "n+1",
    "quadratic",
    "unbounded",
    "memory",
)


def candidate_needs_auto_context(candidate: CandidateFinding) -> bool:
    if candidate.claim_type in {"positive_observation", "uncertain"}:
        return False
    if candidate.claim_type == "defect" and candidate.required_context:
        return True
    text = " ".join(
        [
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            " ".join(candidate.required_context),
            candidate.suspected_category,
        ]
    ).lower()
    high_risk = candidate.claim_type in {
        "security_risk",
        "performance_regression",
    } or any(term in text for term in HIGH_RISK_CONTEXT_TERMS)
    return high_risk and (candidate.required_context or not candidate.evidence_summary.strip())


def auto_focus_requests(task: ReviewTask, candidates: List[CandidateFinding]) -> List[FocusedContextRequest]:
    """Emit bounded auto-requests for candidates with required_context or high-risk missing evidence."""
    requests: List[FocusedContextRequest] = []
    for index, candidate in enumerate(candidates, start=1):
        if not candidate_needs_auto_context(candidate):
            continue
        text_queries: list[str] = []
        if candidate.file_path.strip():
            text_queries.append(candidate.file_path.strip())
        for source in (candidate.failure_mode, candidate.content, *candidate.required_context):
            cleaned = sanitize_text_query(source)
            if cleaned and cleaned not in text_queries:
                text_queries.append(cleaned)
        subject = extract_subject_class(
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            candidate.recommendation or "",
        )
        symbol_queries = [subject] if subject else []
        reason = (
            "Deterministic gather for critiquer required_context questions."
            if candidate.claim_type == "defect" and candidate.required_context
            else (
                "Deterministic context requirement for a high-risk or externally dependent "
                f"{candidate.claim_type} candidate."
            )
        )
        requests.append(
            FocusedContextRequest(
                request_id=f"{candidate.candidate_id}:auto-context:{index}",
                candidate_id=candidate.candidate_id,
                requested_by_specialty=(
                    candidate.reflection_specialties[0]
                    if candidate.reflection_specialties
                    else "general"
                ),
                file_paths=[candidate.file_path] + [path for path in task.target_files if path != candidate.file_path],
                symbol_queries=symbol_queries,
                text_queries=text_queries[:3],
                reason=reason,
            )
        )
    return requests
