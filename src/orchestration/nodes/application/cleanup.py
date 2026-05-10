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
from src.config import Settings, get_settings
from src.domain.state import GraphState

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

EXPECTED_REFLECTORS = {"security", "logic", "performance", "general"}
DOMAIN_REFLECTORS = {"security", "logic", "performance", "general"}
PROMOTABLE_CLAIM_TYPES = {"defect", "security_risk", "performance_regression", "missing_test"}
CONTEXT_REQUIRED_CLAIM_TYPES = frozenset({"security_risk", "performance_regression"})

# Tier 2: claims that typically need cross-file / framework evidence before promotion.
_TIER2_EXTERNAL_CONTEXT_MARKERS = (
    "caller",
    "callers",
    "calling ",
    "middleware",
    "decorator",
    "upstream",
    "downstream",
    "authorization",
    "authorize",
    "authenticated",
    "permission",
    "tenant ",
    "tenant_",
    "isolation",
    "integration",
    "external api",
    "remote ",
    "framework ",
    "orm ",
    "database ",
    "service ",
    "contract",
)

# Tier 1: localized defect signals — do not force focused-context gathers solely for claim_type.
_TIER1_LOCALIZED_MARKERS = (
    "redos",
    "backtrack",
    "catastrophic backtracking",
    "regex",
    "re.search",
    "re.match",
    "re.sub",
    "re.compile",
    "re.fullmatch",
    "len(",
    "nonetype",
    "attributeerror",
    "indexerror",
    "keyerror",
    "zerodivision",
    "off-by-one",
    "group 0",
    "capture group",
    "division by zero",
    "n+1",
    "nested loop",
    "quadratic",
    "o(n^2)",
    "memory leak",
)


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


def _candidate_has_actionability(candidate: CandidateFinding) -> bool:
    return bool(
        candidate.failure_mode.strip()
        and candidate.evidence_summary.strip()
        and (candidate.recommendation or "").strip()
    )


def _candidate_evidence_blob(candidate: CandidateFinding) -> str:
    return " ".join(
        [
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            " ".join(candidate.required_context),
        ]
    ).lower()


def _high_risk_claim_needs_external_context(candidate: CandidateFinding) -> bool:
    """When claim_type is security_risk or performance_regression, decide if promotion requires focused hits."""
    blob = _candidate_evidence_blob(candidate)
    if any(marker in blob for marker in _TIER2_EXTERNAL_CONTEXT_MARKERS):
        return True
    if any(marker in blob for marker in _TIER1_LOCALIZED_MARKERS):
        return False
    return True


def _candidate_requires_context(candidate: CandidateFinding) -> bool:
    if candidate.required_context:
        return True
    if candidate.claim_type not in CONTEXT_REQUIRED_CLAIM_TYPES:
        return False
    return _high_risk_claim_needs_external_context(candidate)


def make_adversarial_cleanup_node(settings: Settings | None = None):
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
        verifier_hints: Dict[str, Any] = dict(metadata.get("verifier_hints") or {})

        if not candidates:
            return {
                "findings": [],
                "metadata": metadata,
                "node_history": [f"{node_name}:empty"],
            }

        by_cand = _reports_by_candidate(reports)
        cleanup_settings = settings or get_settings()
        promoted: List[ReviewFinding] = []
        dropped: List[str] = []
        ignored_rejections: Dict[str, List[str]] = {}
        ignored_context_requests: Dict[str, List[str]] = {}
        missing_required_reflections: Dict[str, List[str]] = {}
        misrouted_candidates: Dict[str, List[Dict[str, str]]] = {}
        lifecycle: Dict[str, Dict[str, Any]] = {}

        def drop(candidate: CandidateFinding, reason: str, details: Dict[str, Any] | None = None) -> None:
            dropped.append(candidate.candidate_id)
            lifecycle[candidate.candidate_id] = {
                "decision": "dropped",
                "reason": reason,
                "claim_type": candidate.claim_type,
                "suspected_category": candidate.suspected_category,
                **(details or {}),
            }

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
            missing_relevant = relevant_reflectors - specialties

            relevant_reports = [
                report for report in cand_reports if report.reflector_specialty in relevant_reflectors
            ]

            abstaining_reflectors: frozenset[str] | None = None
            if missing_relevant:
                require_full = cleanup_settings.reviewer_cleanup_require_full_reflection_quorum
                if require_full or not relevant_reports:
                    missing_required_reflections[candidate.candidate_id] = sorted(missing_relevant)
                    drop(
                        candidate,
                        "missing_required_reflection",
                        {"expected_reflectors": sorted(relevant_reflectors)},
                    )
                    continue
                abstaining_reflectors = frozenset(missing_relevant)
                if _trace_enabled(state):
                    trace_logger.info(
                        "TRACE cleanup_partial_reflection_quorum run_id=%s candidate=%s abstaining=%s "
                        "reports_from=%s",
                        run_id,
                        candidate.candidate_id,
                        sorted(missing_relevant),
                        sorted({r.reflector_specialty for r in relevant_reports}),
                    )

            relevant_needs_verification = any(
                report.verdict == "needs_verification" for report in relevant_reports
            )
            off_domain_reports = [
                report for report in cand_reports if report.reflector_specialty not in relevant_reflectors
            ]
            off_domain_rejections = [
                report.reflector_specialty for report in off_domain_reports if report.verdict == "reject"
            ]
            if off_domain_rejections:
                ignored_rejections[candidate.candidate_id] = off_domain_rejections

            if not relevant_reports:
                drop(
                    candidate,
                    "missing_relevant_reflection",
                    {"expected_reflectors": sorted(relevant_reflectors)},
                )
                continue

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
                drop(
                    candidate,
                    "misrouted_not_applicable",
                    {"reports": misrouted_candidates[candidate.candidate_id]},
                )
                continue

            if any(r.verdict == "reject" for r in relevant_reports):
                drop(
                    candidate,
                    "relevant_reflector_reject",
                    {
                        "rejecting_reflectors": [
                            report.reflector_specialty
                            for report in relevant_reports
                            if report.verdict == "reject"
                        ]
                    },
                )
                continue

            if not any(
                r.verdict in {"accept", "reclassify", "needs_context", "needs_verification"}
                for r in relevant_reports
            ):
                drop(
                    candidate,
                    "no_relevant_acceptance",
                    {"verdicts": [report.verdict for report in relevant_reports]},
                )
                continue

            if candidate.claim_type not in PROMOTABLE_CLAIM_TYPES:
                drop(candidate, "non_promotable_claim_type")
                continue

            if not _candidate_has_actionability(candidate):
                drop(
                    candidate,
                    "missing_actionability_fields",
                    {
                        "has_failure_mode": bool(candidate.failure_mode.strip()),
                        "has_evidence_summary": bool(candidate.evidence_summary.strip()),
                        "has_recommendation": bool((candidate.recommendation or "").strip()),
                    },
                )
                continue

            off_domain_context = [
                report.reflector_specialty for report in off_domain_reports if report.verdict == "needs_context"
            ]
            if off_domain_context:
                ignored_context_requests[candidate.candidate_id] = off_domain_context

            needs_context = any(r.verdict == "needs_context" for r in relevant_reports)
            if _candidate_requires_context(candidate) and not _focused_hits_for_candidate(state, candidate.candidate_id):
                drop(candidate, "required_context_not_gathered")
                continue

            if needs_context or relevant_needs_verification:
                rev = revisions.get(candidate.candidate_id) or {}
                verdict = str(rev.get("verdict", "")).lower()
                if verdict == "reject":
                    drop(candidate, "revision_reject")
                    continue
                hint = verifier_hints.get(candidate.candidate_id)
                verified_hint = (
                    isinstance(hint, dict) and str(hint.get("verdict", "")).lower() == "verified"
                )
                if (
                    verdict != "accept"
                    and not _focused_hits_for_candidate(state, candidate.candidate_id)
                    and not (relevant_needs_verification and verified_hint)
                ):
                    drop(candidate, "needs_context_without_supporting_revision")
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
                    recommendation=candidate.recommendation,
                    references=[],
                )
            )
            lifecycle[candidate.candidate_id] = {
                "decision": "promoted",
                "reason": "accepted_by_relevant_reflectors",
                "claim_type": candidate.claim_type,
                "final_category": category,
                "relevant_reflectors": sorted(relevant_reflectors),
                "had_focused_context": _focused_hits_for_candidate(state, candidate.candidate_id),
            }
            if abstaining_reflectors:
                lifecycle[candidate.candidate_id]["abstaining_reflectors"] = sorted(abstaining_reflectors)
            if candidate.candidate_id in verifier_hints:
                lifecycle[candidate.candidate_id]["verifier_advisory"] = verifier_hints[candidate.candidate_id]

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
            "missing_required_reflections": missing_required_reflections,
            "misrouted_candidate_ids": misrouted_candidates,
            "candidate_lifecycle": lifecycle,
        }
        metadata["adversarial_cleanup"] = cleanup_meta

        return {
            "findings": promoted,
            "metadata": metadata,
            "node_history": [node_name],
        }

    return adversarial_cleanup_node
