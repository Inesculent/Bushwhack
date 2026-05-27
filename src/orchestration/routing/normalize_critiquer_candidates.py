"""Normalize critiquer LLM output: ids/patch task + single reflection specialty hardcap."""

from __future__ import annotations

import re
from typing import List, Mapping

from src.domain.schemas import CandidateFinding, ReviewTask
from src.orchestration.routing.candidate_line_anchor import apply_line_anchor_policy
from src.orchestration.routing.candidate_reflection_specialty import (
    correct_specialty_before_hardcap,
    with_single_reflection_specialty,
)
from src.orchestration.routing.finding_dedupe import (
    candidate_with_behavioral_metadata,
    dedupe_candidates_by_signature,
    defect_family,
    ensure_unique_candidate_ids,
    extract_subject_class,
    is_security_or_unbounded_pattern_claim,
)

_STRUCTURED_EXTRACTION_TASK_IDS = frozenset({"review-logic-structured-extraction"})
_STRUCTURED_TASK_TITLE_MARKER = "structured extraction"

_CLAIM_MISSING_RETURN_RE = re.compile(
    r"missing\s+return|implicit\s+none|no\s+return",
    re.IGNORECASE,
)
_ADD_RETURN_RE = re.compile(r"add\b.*\breturn\b", re.IGNORECASE)
_EVIDENCE_RETURN_RE = re.compile(r"\breturn\b", re.IGNORECASE)
_EVIDENCE_ELIF_RE = re.compile(r"\belif\b", re.IGNORECASE)
_HEDGE_PHRASES = (
    "appears correct",
    "consider adding",
    "document the expected",
    "consider documenting",
)
_STRUCTURED_TRUNCATION_MARKERS = (
    "findall",
    "finditer",
    "matches[0]",
    "match[0]",
    "m[0]",
    "row[0]",
    "rows[0]",
    "[0]",
    "tuple",
    "capture group",
    "first element",
    "first slot",
    "only the first",
)
_COMPOUND_SPLIT_SPECS = (
    (
        "data_loss",
        "indexing",
        ("data loss", "drop", "discard", "only the first", "first slot", "first element", "[0]", "m[0]"),
    ),
    (
        "wrong_output",
        "indexing",
        ("group_index", "group 0", "truthiness", "falsy", "empty group", "wrong output", "wrong value"),
    ),
    (
        "crash",
        "aggregation",
        ("join(", "str.join", "none element", "nonetype", "none in aggregat", "typeerror"),
    ),
    (
        "missing_return",
        "dispatch",
        ("missing else", "missing return", "implicit none", "no final return", "unhandled mode"),
    ),
    (
        "uncaught_exception",
        "exception_scope",
        ("uncaught", "not caught", "outside the try", "outside try", "exception handling"),
    ),
    (
        "unbounded_work",
        "resource_use",
        ("unbounded work", "resource exhaust", "expensive", "without limit", "without bound"),
    ),
)


def _evidence_blob(file_contents: Mapping[str, str] | None) -> str:
    if not file_contents:
        return ""
    return "\n".join(str(v) for v in file_contents.values() if v)


def _handler_likely_has_per_branch_returns(evidence: str) -> bool:
    """True when evidence shows at least one return per if/elif branch (generic heuristic)."""
    returns = len(_EVIDENCE_RETURN_RE.findall(evidence))
    branches = len(_EVIDENCE_ELIF_RE.findall(evidence)) + (
        1 if re.search(r"\bif\b", evidence, re.IGNORECASE) else 0
    )
    return returns >= max(1, branches)


def _ensure_subject_in_content(candidate: CandidateFinding) -> CandidateFinding:
    if extract_subject_class(candidate.content):
        return candidate
    subject = extract_subject_class(
        candidate.failure_mode,
        candidate.evidence_summary,
        candidate.recommendation or "",
    )
    if not subject:
        return candidate
    body = (candidate.content or "").strip()
    if body.lower().startswith(f"class {subject.lower()}"):
        return candidate
    return candidate.model_copy(update={"content": f"class {subject}: {body}"[:600]})


def _repair_branch_return_conflation(
    candidate: CandidateFinding,
    *,
    file_contents: Mapping[str, str] | None,
) -> CandidateFinding:
    """Rewrite missing-return-on-branch slips into missing terminal else when returns exist."""
    rec = candidate.recommendation or ""
    fm = candidate.failure_mode or ""
    blob = " ".join([fm, rec, candidate.evidence_summary, candidate.content])
    if is_security_or_unbounded_pattern_claim(blob):
        return candidate
    if not _CLAIM_MISSING_RETURN_RE.search(fm) and not _CLAIM_MISSING_RETURN_RE.search(rec):
        return candidate
    if not _ADD_RETURN_RE.search(rec):
        return candidate

    evidence = _evidence_blob(file_contents)
    if not evidence.strip() or not _handler_likely_has_per_branch_returns(evidence):
        return candidate

    summary = (
        "[SAFE] visible if/elif branches return; [DEFECT] no terminal else for unexpected discriminant."
    )
    return candidate.model_copy(
        update={
            "failure_mode": (
                "Missing terminal else: handler falls through with implicit None (or wrong type) "
                "when the discriminant is not handled, violating the declared return contract."
            )[:400],
            "recommendation": (
                "Add a terminal else (raise or return a contract-consistent default) after the "
                "last elif; do not duplicate returns on branches that already return."
            ),
            "evidence_summary": summary[:400],
            "content": (
                (candidate.content.split(":")[0] if ":" in candidate.content else candidate.content)
                + ": missing terminal else on discriminant dispatch"
            )[:600],
        }
    )


def _strengthen_hedged_structured_data_loss(candidate: CandidateFinding) -> CandidateFinding:
    """Replace hedge-only wording when evidence points at first-slot truncation on structured rows."""
    rec = (candidate.recommendation or "").lower()
    blob = " ".join(
        [
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            candidate.recommendation or "",
        ]
    ).lower()
    if is_security_or_unbounded_pattern_claim(blob):
        return candidate
    if not any(phrase in rec for phrase in _HEDGE_PHRASES):
        return candidate
    if not any(marker in blob for marker in _STRUCTURED_TRUNCATION_MARKERS):
        return candidate
    if "[0]" not in blob and "first" not in blob and "m[0]" not in blob and "matches[0]" not in blob:
        return candidate

    return candidate.model_copy(
        update={
            "claim_type": "defect",
            "failure_mode": (
                "Data loss: structured rows or tuples are normalized to a single index/slot "
                "without an explicit contract allowing truncation."
            )[:400],
            "recommendation": (
                "Retain all required slots per row, flatten safely, or narrow the contract and "
                "enforce first-slot-only behavior in code."
            ),
            "severity": "high",
            "reflection_specialties": ["logic"],
            "suspected_category": "logic",
            "behavioral_symptom": "data_loss",
            "root_operation": "indexing",
        }
    )


def _maybe_retag_findall_first_group_loss(candidate: CandidateFinding) -> CandidateFinding:
    """Structured row/tuple truncation tagged as perf-only → logic defect."""
    blob = " ".join(
        [
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
        ]
    ).lower()
    if candidate.claim_type != "performance_regression":
        return candidate
    if not any(marker in blob for marker in ("findall", "finditer", "[0]", "m[0]", "matches[0]", "tuple")):
        return candidate
    return candidate.model_copy(
        update={
            "claim_type": "defect",
            "reflection_specialties": ["logic"],
            "suspected_category": "logic",
        }
    )


def _split_compound_candidate(candidate: CandidateFinding) -> List[CandidateFinding]:
    """Clone candidates that describe multiple independent behavioral symptoms."""
    blob = " ".join(
        [
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            candidate.recommendation or "",
        ]
    ).lower()
    matches: List[tuple[str, str]] = []
    for symptom, root, markers in _COMPOUND_SPLIT_SPECS:
        if any(marker in blob for marker in markers):
            matches.append((symptom, root))
    deduped: List[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in matches:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    if len(deduped) <= 1:
        return [candidate_with_behavioral_metadata(candidate)]

    out: List[CandidateFinding] = []
    for index, (symptom, root) in enumerate(deduped, start=1):
        suffix = f":orthogonal-{index}"
        cid = candidate.candidate_id
        out.append(
            candidate.model_copy(
                update={
                    "candidate_id": cid if index == 1 else f"{cid}{suffix}",
                    "behavioral_symptom": symptom,
                    "root_operation": root,
                }
            )
        )
    return out


def _is_structured_extraction_task(task: ReviewTask) -> bool:
    if task.id in _STRUCTURED_EXTRACTION_TASK_IDS:
        return True
    return _STRUCTURED_TASK_TITLE_MARKER in f"{task.title} {task.description}".lower()


def _out_of_scope_for_structured_task(cand: CandidateFinding) -> bool:
    """Branch-exhaustiveness claims belong on diff-local tasks, not structured-extraction."""
    family = defect_family(
        cand.content,
        cand.failure_mode,
        cand.evidence_summary,
        cand.recommendation or "",
    )
    return family == "missing_branch_return"


def normalize_critiquer_candidates(
    task: ReviewTask,
    candidates: List[CandidateFinding],
    *,
    file_contents: Mapping[str, str] | None = None,
    git_diff: str = "",
) -> tuple[List[CandidateFinding], List[str], dict[str, list[str]]]:
    """Apply stable ids, line-anchor repair/drop, and collapse reflection specialties."""
    warnings: List[str] = []
    normalized: List[CandidateFinding] = []
    structured_task = _is_structured_extraction_task(task)
    for index, cand in enumerate(candidates, start=1):
        if structured_task and _out_of_scope_for_structured_task(cand):
            warnings.append(
                f"{task.id}:c{index}:structured_task_scope_drop:missing_branch_return"
            )
            continue
        cid = cand.candidate_id.strip() or f"{task.id}:c{index}"
        if not cid.startswith(task.id):
            cid = f"{task.id}:{cid}"
        with_ids = cand.model_copy(
            update={
                "candidate_id": cid,
                "patch_task_id": task.id,
            }
        )
        corrected, _ = correct_specialty_before_hardcap(with_ids)
        anchored_content = _ensure_subject_in_content(corrected)
        retagged = _maybe_retag_findall_first_group_loss(anchored_content)
        repaired = _repair_branch_return_conflation(retagged, file_contents=file_contents)
        strengthened = _strengthen_hedged_structured_data_loss(repaired)
        completed = candidate_with_behavioral_metadata(strengthened)
        for split in _split_compound_candidate(completed):
            normalized.append(with_single_reflection_specialty(split))

    normalized = ensure_unique_candidate_ids(normalized)

    anchored, anchor_warnings, dropped_ids = apply_line_anchor_policy(
        normalized,
        file_contents=file_contents,
        git_diff=git_diff,
    )
    warnings.extend(anchor_warnings)
    if dropped_ids:
        warnings.append(f"line_anchor_dropped:{','.join(dropped_ids)}")

    deduped, duplicate_map = dedupe_candidates_by_signature(anchored, git_diff=git_diff)
    if duplicate_map:
        warnings.append(
            f"semantic_dedupe_collapsed:{sum(len(v) for v in duplicate_map.values())}"
        )
    return deduped, warnings, duplicate_map
