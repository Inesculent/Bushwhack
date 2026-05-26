"""Consolidate duplicate reflection reports and shared Tier-1 defect markers."""

from __future__ import annotations

from typing import Any, Iterable, List, Sequence

from src.domain.schemas import ReflectionReport

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

TIER1_LOCALIZED_MARKERS = (
    "redos",
    "backtrack",
    "catastrophic backtracking",
    "regex",
    "re.search",
    "re.match",
    "re.sub",
    "re.compile",
    "re.fullmatch",
    "re.findall",
    "len(",
    "nonetype",
    "none",
    "typeerror",
    "attributeerror",
    "indexerror",
    "keyerror",
    "zerodivision",
    "off-by-one",
    "group 0",
    "capture group",
    "division by zero",
    "missing return",
    "missing else",
    "implicit none",
    "wrong output",
    "data loss",
    "silent",
    "n+1",
    "nested loop",
    "quadratic",
    "o(n^2)",
    "memory leak",
)


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


def candidate_has_tier1_localized_markers(*text_blobs: str) -> bool:
    blob = " ".join(t for t in text_blobs if t).lower()
    return any(marker in blob for marker in TIER1_LOCALIZED_MARKERS)


def coerce_reports_list(reports: Iterable[Any]) -> List[ReflectionReport]:
    out: List[ReflectionReport] = []
    for item in reports:
        r = _coerce_report(item)
        if r is not None:
            out.append(r)
    return out
