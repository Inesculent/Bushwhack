"""Consolidate duplicate reflection reports."""

from __future__ import annotations

from typing import Any, Iterable, List, Sequence

from src.domain.schemas import CandidateFinding, ReflectionReport
from src.orchestration.routing.finding_dedupe import candidate_with_behavioral_metadata

REFLECTOR_SPECIALTIES = ("security", "logic", "performance", "general")

# Lower number = higher precedence when the same (candidate_id, specialty) has conflicting verdicts.
_VERDICT_PRECEDENCE: dict[str, int] = {
    "accept": 0,
    "reclassify": 0,
    "needs_verification": 1,
    "needs_context": 2,
    "reject": 3,
    "not_applicable": 4,
}

_LOCAL_DEFECT_SIGNATURES = frozenset(
    {
        ("missing_return", "dispatch"),
        ("data_loss", "indexing"),
        ("wrong_output", "indexing"),
        ("crash", "aggregation"),
        ("uncaught_exception", "exception_scope"),
        ("contract_mismatch", "contract"),
        ("data_loss", "serialization"),
        ("crash", "serialization"),
    }
)


def candidate_has_local_defect_signature(candidate: CandidateFinding) -> bool:
    """True when structured candidate metadata identifies a source-local defect."""
    if candidate.claim_type != "defect":
        return False
    normalized = candidate_with_behavioral_metadata(candidate)
    return (normalized.behavioral_symptom, normalized.root_operation) in _LOCAL_DEFECT_SIGNATURES


def _coerce_report(item: Any) -> ReflectionReport | None:
    if isinstance(item, ReflectionReport):
        return item
    if isinstance(item, dict):
        try:
            return ReflectionReport.model_validate(item)
        except Exception:
            return None
    return None


def _verdict_rank(verdict: str) -> int:
    return _VERDICT_PRECEDENCE.get((verdict or "").strip().lower(), 99)


def pick_preferred_report(a: ReflectionReport, b: ReflectionReport) -> ReflectionReport:
    """Return the report that should win for the same candidate_id + specialty."""
    ra, rb = _verdict_rank(a.verdict), _verdict_rank(b.verdict)
    if ra != rb:
        return a if ra < rb else b
    return b


def consolidate_reflection_reports(reports: Sequence[Any]) -> List[ReflectionReport]:
    """One report per (candidate_id, reflector_specialty); higher-precedence verdict wins."""
    by_key: dict[tuple[str, str], ReflectionReport] = {}
    order: List[tuple[str, str]] = []
    for item in reports:
        report = _coerce_report(item)
        if report is None:
            continue
        key = (report.candidate_id, report.reflector_specialty)
        if key not in by_key:
            order.append(key)
            by_key[key] = report
        else:
            by_key[key] = pick_preferred_report(by_key[key], report)
    return [by_key[k] for k in order]


def dedupe_batch_reports_per_candidate(
    reports: List[ReflectionReport],
) -> tuple[List[ReflectionReport], List[str]]:
    """Keep last report per candidate_id within one batch; warn on duplicates."""
    warnings: List[str] = []
    by_id: dict[str, ReflectionReport] = {}
    order: List[str] = []
    for report in reports:
        cid = report.candidate_id
        if cid in by_id:
            warnings.append(f"reflection_duplicate_report:{cid}")
        else:
            order.append(cid)
        by_id[cid] = report
    return [by_id[cid] for cid in order], warnings


def coerce_reports_list(reports: Iterable[Any]) -> List[ReflectionReport]:
    out: List[ReflectionReport] = []
    for item in reports:
        r = _coerce_report(item)
        if r is not None:
            out.append(r)
    return out
