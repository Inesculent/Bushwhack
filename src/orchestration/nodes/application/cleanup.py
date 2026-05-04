"""Deterministic promotion of accepted candidates into ReviewFinding objects."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Sequence

from src.domain.schemas import (
    CandidateFinding,
    FocusedContextResult,
    ReflectionReport,
    ReviewCategory,
    ReviewFinding,
)
from src.domain.state import GraphState

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

EXPECTED_REFLECTORS = {"security", "logic", "performance", "general"}
DOMAIN_REFLECTORS = {"security", "logic", "performance", "general"}


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _reports_by_candidate(reports: Sequence[Any]) -> Dict[str, List[ReflectionReport]]:
    grouped: Dict[str, List[ReflectionReport]] = {}
    for item in reports:
        report: ReflectionReport | None
        if isinstance(item, ReflectionReport):
            report = item
        elif isinstance(item, dict):
            try:
                report = ReflectionReport.model_validate(item)
            except Exception:
                report = None
        else:
            report = None
        if report is None:
            continue
        grouped.setdefault(report.candidate_id, []).append(report)
    return grouped


def _revision_map(metadata: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    block = metadata.get("critique_revision") or {}
    revisions = block.get("revisions") or []
    out: Dict[str, Mapping[str, Any]] = {}
    for entry in revisions:
        if isinstance(entry, dict) and entry.get("candidate_id"):
            out[str(entry["candidate_id"])] = entry
    return out


def _focused_hits_for_candidate(state: GraphState, candidate_id: str) -> bool:
    for raw in (state.get("focused_context_results", {}) or {}).values():
        if isinstance(raw, dict):
            result = FocusedContextResult.model_validate(raw)
        else:
            result = raw
        if getattr(result, "candidate_id", None) == candidate_id:
            if getattr(result, "file_snippets", None) or getattr(result, "search_hits", None):
                return True
    return False


def _final_category(candidate: CandidateFinding, reports: List[ReflectionReport]) -> ReviewCategory:
    category: ReviewCategory = candidate.suspected_category
    for report in reports:
        if report.verdict == "reclassify" and report.reclassified_category:
            category = report.reclassified_category
    return category


def _relevant_reflectors(candidate: CandidateFinding, category: ReviewCategory) -> set[str]:
    routed = {
        specialty
        for specialty in candidate.reflection_specialties
        if specialty in DOMAIN_REFLECTORS
    }
    if routed:
        return routed
    if category in DOMAIN_REFLECTORS:
        return {category}
    return set(DOMAIN_REFLECTORS)


def _category_to_feedback(category: ReviewCategory):
    if category == "security":
        return "defect_detection"
    if category == "logic":
        return "defect_detection"
    if category == "performance":
        return "optimization"
    if category == "general":
        return "code_improvement"
    return "other"


def make_adversarial_cleanup_node():
    node_name = "adversarial_cleanup"

    def adversarial_cleanup_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        candidates: List[CandidateFinding] = []
        for raw in state.get("candidate_findings", []) or []:
            if isinstance(raw, CandidateFinding):
                candidates.append(raw)
            elif isinstance(raw, dict):
                candidates.append(CandidateFinding.model_validate(raw))
        reports = list(state.get("reflection_reports", []) or [])
        metadata = dict(state.get("metadata", {}))
        revisions = _revision_map(metadata)

        if not candidates:
            return {
                "findings": [],
                "metadata": metadata,
                "node_history": [f"{node_name}:empty"],
            }

        by_cand = _reports_by_candidate(reports)
        promoted: List[ReviewFinding] = []
        dropped: List[str] = []
        ignored_rejections: Dict[str, List[str]] = {}
        ignored_context_requests: Dict[str, List[str]] = {}
        misrouted_candidates: Dict[str, List[Dict[str, str]]] = {}

        for candidate in candidates:
            cand_reports = by_cand.get(candidate.candidate_id, [])
            specialties = {r.reflector_specialty for r in cand_reports}
            missing = EXPECTED_REFLECTORS - specialties
            if missing and _trace_enabled(state):
                trace_logger.info(
                    "TRACE cleanup_missing_reflectors run_id=%s candidate=%s missing=%s",
                    run_id,
                    candidate.candidate_id,
                    sorted(missing),
                )

            category = _final_category(candidate, cand_reports)
            relevant_reflectors = _relevant_reflectors(candidate, category)
            relevant_reports = [
                report for report in cand_reports if report.reflector_specialty in relevant_reflectors
            ]
            off_domain_reports = [
                report for report in cand_reports if report.reflector_specialty not in relevant_reflectors
            ]
            off_domain_rejections = [
                report.reflector_specialty for report in off_domain_reports if report.verdict == "reject"
            ]
            if off_domain_rejections:
                ignored_rejections[candidate.candidate_id] = off_domain_rejections

            not_applicable_reports = [
                report for report in relevant_reports if report.verdict == "not_applicable"
            ]
            if not_applicable_reports:
                misrouted_candidates[candidate.candidate_id] = [
                    {
                        "reflector_specialty": report.reflector_specialty,
                        "rationale": report.rationale,
                    }
                    for report in not_applicable_reports
                ]
                dropped.append(candidate.candidate_id)
                continue

            if any(r.verdict == "reject" for r in relevant_reports):
                dropped.append(candidate.candidate_id)
                continue

            off_domain_context = [
                report.reflector_specialty for report in off_domain_reports if report.verdict == "needs_context"
            ]
            if off_domain_context:
                ignored_context_requests[candidate.candidate_id] = off_domain_context

            needs_context = any(r.verdict == "needs_context" for r in relevant_reports)
            if needs_context:
                rev = revisions.get(candidate.candidate_id) or {}
                verdict = str(rev.get("verdict", "")).lower()
                if verdict == "reject":
                    dropped.append(candidate.candidate_id)
                    continue
                if verdict != "accept" and not _focused_hits_for_candidate(state, candidate.candidate_id):
                    dropped.append(candidate.candidate_id)
                    continue

            feedback_type = _category_to_feedback(category)  # type: ignore[arg-type]
            evidence_extra = ""
            rev = revisions.get(candidate.candidate_id) or {}
            if isinstance(rev.get("updated_evidence_summary"), str) and rev["updated_evidence_summary"]:
                evidence_extra = f"\n\nPost-context evidence: {rev['updated_evidence_summary']}"

            promoted.append(
                ReviewFinding(
                    id=candidate.candidate_id,
                    file_path=candidate.file_path,
                    line_start=candidate.line_start,
                    line_end=candidate.line_end,
                    content=candidate.content + evidence_extra,
                    severity=candidate.severity,
                    feedback_type=feedback_type,  # type: ignore[arg-type]
                    recommendation=None,
                    references=[],
                )
            )

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE adversarial_cleanup run_id=%s promoted=%s dropped=%s",
                run_id,
                len(promoted),
                dropped,
            )

        cleanup_meta = {
            "promoted_count": len(promoted),
            "dropped_candidate_ids": dropped,
            "ignored_off_domain_rejections": ignored_rejections,
            "ignored_off_domain_context_requests": ignored_context_requests,
            "misrouted_candidate_ids": misrouted_candidates,
        }
        metadata["adversarial_cleanup"] = cleanup_meta

        return {
            "findings": promoted,
            "metadata": metadata,
            "node_history": [node_name],
        }

    return adversarial_cleanup_node
