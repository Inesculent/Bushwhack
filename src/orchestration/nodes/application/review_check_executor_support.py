"""Executor support helpers for review-check nodes."""

from __future__ import annotations

from typing import Any, Callable, Iterable, List, Mapping

from src.domain.schemas import CandidateFinding, ReviewCheck, ReviewCheckResult, ReviewTask
from src.domain.state import GraphState
from src.orchestration.nodes.application.critiquer import _normalize_candidates
from src.orchestration.nodes.verifier.source_only import source_only_verify_candidate

_INSUFFICIENT_EVIDENCE_MARKERS = (
    "insufficient evidence",
    "not enough evidence",
    "cannot verify",
    "cannot confirm",
    "could not verify",
    "not visible",
    "missing evidence",
    "no evidence",
    "provided evidence does not",
    "evidence is insufficient",
)

_OUTER_ONLY_SUPPRESSION_MARKERS = (
    "return type",
    "returns a string",
    "returns string",
    "outer container",
    "container type",
    "consistent return",
    "all branches return",
    "schema",
    "enum",
    "declared option",
)

_DIMENSION_MARKERS = {
    "aggregation": (
        "aggregat",
        "serializ",
        "join",
        "combine",
        "collect",
        "structured",
        "data shape",
        "tuple",
        "field",
        "slot",
        "element",
        "entry",
        "row",
    ),
    "indexing": (
        "index",
        "indices",
        "bounds",
        "slot",
        "field",
        "key",
        "value",
        "element",
        "entry",
        "row",
        "offset",
    ),
    "dispatch": (
        "branch",
        "dispatch",
        "fallback",
        "fallthrough",
        "exhaust",
        "default",
        "else",
        "mode",
        "case",
        "return path",
    ),
}

_AGGREGATION_PRESERVATION_MARKERS = (
    "all",
    "each",
    "every",
    "preserve",
    "preserved",
    "loss",
    "lost",
    "drop",
    "dropped",
    "omit",
    "omitted",
    "truncate",
    "truncated",
    "remaining",
    "count",
    "order",
    "field",
    "fields",
    "slot",
    "slots",
    "row",
    "rows",
    "entry",
    "entries",
)


def _has_any(text: str, markers: Iterable[str]) -> bool:
    return any(marker in text for marker in markers)


def file_contents_from_slot(slot: Mapping[str, Any]) -> Mapping[str, str] | None:
    te = slot.get("task_evidence") if isinstance(slot.get("task_evidence"), dict) else {}
    if isinstance(te, dict) and isinstance(te.get("file_contents"), dict):
        return te["file_contents"]
    return None


def _behavioral_defaults_for_check(check: ReviewCheck) -> tuple[str, str]:
    blob = " ".join([str(check.lens), check.affected_invariant, check.behavioral_question]).lower()
    if check.lens == "error_propagation":
        return "uncaught_exception", "exception_scope"
    if check.lens == "resource_lifecycle":
        return "unbounded_work", "resource_use"
    if check.lens == "data_shape_consistency":
        if any(term in blob for term in ("index", "slot", "field", "bound")):
            return "data_loss", "indexing"
        if any(term in blob for term in ("aggregat", "join", "serial")):
            return "data_loss", "aggregation"
        return "data_loss", "serialization"
    if check.lens in {"api_compatibility", "input_validation", "permission_boundary", "test_oracle_strength"}:
        return "contract_mismatch", "contract"
    if check.lens == "state_transition":
        return "wrong_output", "dispatch"
    if check.lens == "concurrency_ordering":
        return "wrong_output", "resource_use"
    return "other", "other"


def candidate_with_check_behavioral_metadata(
    candidate: CandidateFinding,
    check: ReviewCheck,
) -> CandidateFinding:
    """Fill missing generic behavior identity from the originating check."""
    symptom, root = _behavioral_defaults_for_check(check)
    updates: dict[str, str] = {}
    if not candidate.behavioral_symptom:
        updates["behavioral_symptom"] = symptom
    if not candidate.root_operation:
        updates["root_operation"] = root
    if not updates:
        return candidate
    return candidate.model_copy(update=updates)


def file_evidence_is_complete(slot: Mapping[str, Any], file_path: str) -> bool:
    te = slot.get("task_evidence") if isinstance(slot.get("task_evidence"), dict) else {}
    if not isinstance(te, dict):
        return False
    files = te.get("file_contents") if isinstance(te.get("file_contents"), dict) else {}
    if not isinstance(files, dict):
        return False
    normalized = file_path.strip().replace("\\", "/")
    if not normalized or not str(files.get(normalized) or "").strip():
        return False
    complete = te.get("files_complete") if isinstance(te.get("files_complete"), dict) else {}
    return bool(complete.get(normalized) or complete.get(file_path))


def missing_evidence_for_weak_no_finding(
    check: ReviewCheck,
    evidence_requirements_for_check: Callable[[ReviewCheck], List[str]],
) -> List[str]:
    requirements = evidence_requirements_for_check(check)
    if requirements:
        return requirements[:3]
    return list(check.required_evidence[:3])


def no_finding_has_strong_suppression(result: ReviewCheckResult, check: ReviewCheck) -> bool:
    if not result.evidence_refs or not result.suppressing_evidence:
        return False
    blob = " ".join([result.reportable_reason, *result.suppressing_evidence]).lower()
    if any(marker in blob for marker in _INSUFFICIENT_EVIDENCE_MARKERS):
        return False
    check_blob = " ".join(
        [
            check.behavioral_question,
            check.affected_invariant,
            " ".join(check.required_evidence),
            " ".join(check.report_criteria),
            " ".join(check.suppress_criteria),
        ]
    ).lower()
    if no_finding_needs_semantic_suppression_audit(result, check):
        return True
    check_path = check.file_path.strip().replace("\\", "/")
    refs = [ref.strip().replace("\\", "/") for ref in result.evidence_refs if str(ref).strip()]
    return any(check_path in ref or ref.startswith("focused_context:") for ref in refs)


def no_finding_needs_semantic_suppression_audit(
    result: ReviewCheckResult,
    check: ReviewCheck,
) -> bool:
    if result.decision != "no_finding" or not result.evidence_refs or not result.suppressing_evidence:
        return False
    blob = " ".join([result.reportable_reason, *result.suppressing_evidence]).lower()
    if any(marker in blob for marker in _INSUFFICIENT_EVIDENCE_MARKERS):
        return False
    check_blob = " ".join(
        [
            check.behavioral_question,
            check.affected_invariant,
            " ".join(check.required_evidence),
            " ".join(check.report_criteria),
            " ".join(check.suppress_criteria),
        ]
    ).lower()
    categories = {
        category
        for category, markers in _DIMENSION_MARKERS.items()
        if _has_any(check_blob, markers)
    }
    if categories and _has_any(blob, _OUTER_ONLY_SUPPRESSION_MARKERS):
        if "aggregation" in categories and not _has_any(blob, _AGGREGATION_PRESERVATION_MARKERS):
            return True
        if "indexing" in categories and not _has_any(blob, _DIMENSION_MARKERS["indexing"]):
            return True
        if "dispatch" in categories and not _has_any(blob, _DIMENSION_MARKERS["dispatch"]):
            return True
    return False


def source_only_backstop_candidate(
    *,
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    check: ReviewCheck,
    check_is_source_local: bool,
) -> tuple[CandidateFinding | None, str]:
    if not check_is_source_local:
        return None, ""
    if not file_evidence_is_complete(slot, check.file_path):
        return None, ""
    check_blob = " ".join(
        [
            check.changed_code_anchor,
            check.behavioral_question,
            check.affected_invariant,
            " ".join(check.required_evidence),
            " ".join(check.report_criteria),
        ]
    )
    candidate = {
        "candidate_id": f"{check.check_id}:source-only",
        "patch_task_id": task.id,
        "file_path": check.file_path,
        "line_start": check.line_start,
        "line_end": check.line_end,
        "content": check.behavioral_question or check.affected_invariant,
        "failure_mode": check.affected_invariant,
        "evidence_summary": check_blob,
        "recommendation": "Fix the source-local behavior proven by static source evidence.",
    }
    verdict, rationale, attempt = source_only_verify_candidate(state, candidate)
    if verdict != "verified" or attempt is None:
        return None, ""
    failure_class = str(attempt.failure_class or "")
    if failure_class == "missing_return":
        symptom = "missing_return"
        operation = "dispatch"
    elif failure_class == "syntax_error":
        symptom = "crash"
        operation = "contract"
    elif "join" in rationale.lower():
        symptom = "crash"
        operation = "aggregation"
    elif "tuple element 0" in rationale.lower() or "re.search" in rationale.lower():
        symptom = "data_loss"
        operation = "indexing"
    else:
        symptom = "contract_mismatch"
        operation = "contract"
    category = task.specialty if task.specialty in {"security", "logic", "performance", "general"} else "logic"
    specialty = category if category in {"security", "logic", "performance", "general"} else "logic"
    finding = CandidateFinding(
        candidate_id=str(candidate["candidate_id"]),
        patch_task_id=task.id,
        file_path=check.file_path,
        line_start=check.line_start,
        line_end=check.line_end,
        content=(check.behavioral_question or check.affected_invariant or rationale)[:600],
        claim_type="defect",
        failure_mode=rationale[:400],
        evidence_summary=f"Source-only verifier proved: {rationale}"[:400],
        required_context=[],
        confidence=0.9,
        suspected_category=category,  # type: ignore[arg-type]
        reflection_specialties=[specialty],  # type: ignore[list-item]
        feedback_type="defect_detection",
        severity="medium",
        recommendation="Fix the source-local behavior proven by static source evidence.",
        behavioral_symptom=symptom,  # type: ignore[arg-type]
        root_operation=operation,  # type: ignore[arg-type]
    )
    return finding, rationale


def normalize_executor_results(
    *,
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    checks: List[ReviewCheck],
    results: Iterable[ReviewCheckResult],
    git_diff: str,
    check_budget_remaining: Callable[[GraphState, ReviewCheck], int],
    evidence_requirements_for_check: Callable[[ReviewCheck], List[str]],
    compiled_check_is_source_local: Callable[[ReviewCheck], bool],
    include_missing_results: bool = True,
    missing_result_warning: str = "executor_missing_result",
) -> tuple[List[ReviewCheckResult], List[str]]:
    warnings: List[str] = []
    by_check = {check.check_id: check for check in checks}
    normalized: List[ReviewCheckResult] = []
    for raw in results:
        if raw.check_id not in by_check:
            warnings.append(f"executor_result_unknown_check:{raw.check_id}")
            continue
        check = by_check[raw.check_id]
        result = raw.model_copy(update={"patch_task_id": task.id})
        candidate = result.candidate
        if candidate is not None:
            candidate = candidate_with_check_behavioral_metadata(candidate, check)
            cid = candidate.candidate_id.strip() or f"{check.check_id}:candidate"
            patched = candidate.model_copy(
                update={
                    "candidate_id": cid,
                    "patch_task_id": task.id,
                    "file_path": candidate.file_path or check.file_path,
                    "line_start": candidate.line_start or check.line_start,
                    "line_end": max(candidate.line_end or check.line_end, candidate.line_start or check.line_start),
                }
            )
            normed = _normalize_candidates(
                task,
                [patched],
                pipeline_slot={"task_evidence": {"file_contents": file_contents_from_slot(slot) or {}}},
                git_diff=git_diff,
            )
            if normed:
                result = result.model_copy(update={"candidate": normed[0], "decision": "candidate"})
            else:
                warnings.append(f"executor_candidate_dropped_by_normalizer:{cid}")
                result = result.model_copy(update={"candidate": None, "decision": "unsupported"})
        if result.decision == "no_finding":
            source_candidate, source_rationale = source_only_backstop_candidate(
                state=state,
                task=task,
                slot=slot,
                check=check,
                check_is_source_local=compiled_check_is_source_local(check),
            )
            if source_candidate is not None:
                warnings.append(f"executor_source_only_no_finding_overridden:{check.check_id}")
                result = result.model_copy(
                    update={
                        "decision": "candidate",
                        "candidate": source_candidate,
                        "reportable_reason": source_rationale[:500],
                        "suppressing_evidence": [],
                        "warnings": list(result.warnings) + ["source_only_no_finding_overridden"],
                    }
                )
        if result.decision == "no_finding" and not no_finding_has_strong_suppression(result, check):
            warnings.append(f"executor_weak_no_finding_downgraded:{check.check_id}")
            next_decision = "unsupported" if check_budget_remaining(state, check) else "budget_exhausted"
            next_warnings = ["weak_no_finding_requires_more_evidence"]
            if next_decision == "budget_exhausted":
                next_warnings.append("review_check_budget_exhausted")
            result = result.model_copy(
                update={
                    "decision": next_decision,
                    "missing_evidence": missing_evidence_for_weak_no_finding(
                        check,
                        evidence_requirements_for_check,
                    ),
                    "warnings": list(result.warnings) + next_warnings,
                }
            )
        normalized.append(result)
        if (
            result.decision == "unsupported"
            and result.missing_evidence
            and not check_budget_remaining(state, check)
        ):
            result = result.model_copy(
                update={
                    "decision": "budget_exhausted",
                    "warnings": list(result.warnings) + ["review_check_budget_exhausted"],
                }
            )
            normalized[-1] = result
    present = {item.check_id for item in normalized}
    if include_missing_results:
        for check in checks:
            if check.check_id not in present:
                normalized.append(
                    ReviewCheckResult(
                        check_id=check.check_id,
                        patch_task_id=task.id,
                        decision="unsupported",
                        warnings=[missing_result_warning],
                    )
                )
    return normalized, warnings
