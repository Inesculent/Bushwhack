"""Executor support helpers for review-check nodes."""

from __future__ import annotations

from typing import Any, Callable, Iterable, List, Mapping

from src.domain.schemas import CandidateFinding, ReviewCheck, ReviewCheckResult, ReviewTask
from src.domain.state import GraphState
from src.orchestration.nodes.application.critiquer import _normalize_candidates
from src.orchestration.routing.claim_digest import claim_digest_for_result


def file_contents_from_slot(slot: Mapping[str, Any]) -> Mapping[str, str] | None:
    te = slot.get("task_evidence") if isinstance(slot.get("task_evidence"), dict) else {}
    if isinstance(te, dict) and isinstance(te.get("file_contents"), dict):
        return te["file_contents"]
    return None


def _behavioral_defaults_for_check(check: ReviewCheck) -> tuple[str, str]:
    if check.lens == "error_propagation":
        return "uncaught_exception", "exception_scope"
    if check.lens == "resource_lifecycle":
        return "unbounded_work", "resource_use"
    if check.lens == "data_shape_consistency":
        return "data_loss", "contract"
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


def _contract_evidence_from_check(check: ReviewCheck, result: ReviewCheckResult | None = None) -> str:
    if result is not None and result.evidence_for_contract.strip():
        return result.evidence_for_contract.strip()[:500]
    parts = [check.affected_invariant.strip()]
    parts.extend(str(item).strip() for item in check.required_evidence if str(item).strip())
    return "; ".join(part for part in parts if part)[:500]


def _expected_behavior_from_check(check: ReviewCheck, result: ReviewCheckResult | None = None) -> str:
    if result is not None and result.expected_behavior.strip():
        return result.expected_behavior.strip()[:500]
    if check.expected_behavior.strip():
        return check.expected_behavior.strip()[:500]
    return check.affected_invariant.strip()[:500]


def _counterexample_from_result(check: ReviewCheck, result: ReviewCheckResult) -> str:
    if result.counterexample.strip():
        return result.counterexample.strip()[:500]
    reason = result.reportable_reason.strip()
    if reason:
        return reason[:500]
    return (check.report_criteria[0] if check.report_criteria else check.behavioral_question)[:500]


def _rejection_check_from_result(check: ReviewCheck, result: ReviewCheckResult) -> str:
    if result.rejection_check.strip():
        return result.rejection_check.strip()[:500]
    suppress = "; ".join(str(item).strip() for item in check.suppress_criteria if str(item).strip())
    if suppress:
        return f"No suppressing evidence found for: {suppress}"[:500]
    return "Concrete changed-code evidence supports the claim; no intentional narrowing or caller guarantee suppresses it."


def candidate_with_check_contract_proof(
    candidate: CandidateFinding,
    check: ReviewCheck,
    result: ReviewCheckResult,
) -> CandidateFinding:
    updates: dict[str, str] = {}
    if not candidate.expected_behavior.strip():
        expected = _expected_behavior_from_check(check, result)
        if expected:
            updates["expected_behavior"] = expected
    if not candidate.evidence_for_contract.strip():
        updates["evidence_for_contract"] = _contract_evidence_from_check(check, result)
    if not candidate.counterexample.strip():
        updates["counterexample"] = _counterexample_from_result(check, result)
    if not candidate.rejection_check.strip():
        updates["rejection_check"] = _rejection_check_from_result(check, result)
    if not updates:
        return candidate
    return candidate.model_copy(update=updates)


def _missing_contract_proof_field_names(candidate: CandidateFinding) -> list[str]:
    missing: list[str] = []
    if not candidate.expected_behavior.strip():
        missing.append("expected_behavior")
    if not candidate.evidence_for_contract.strip():
        missing.append("evidence_for_contract")
    if not candidate.counterexample.strip():
        missing.append("counterexample")
    if not candidate.rejection_check.strip():
        missing.append("rejection_check")
    return missing


def _candidate_payload_is_concrete(result: ReviewCheckResult) -> bool:
    if result.suppressing_evidence:
        return False
    if not result.reportable_reason.strip() or not result.evidence_refs:
        return False
    return True


def _synthesize_candidate_from_result(
    *,
    task: ReviewTask,
    check: ReviewCheck,
    result: ReviewCheckResult,
) -> CandidateFinding | None:
    if not _candidate_payload_is_concrete(result):
        return None
    expected_behavior = _expected_behavior_from_check(check, result)
    if not expected_behavior:
        return None
    symptom, operation = _behavioral_defaults_for_check(check)
    specialty = task.specialty if task.specialty in {"security", "performance", "logic", "general"} else "general"
    category = specialty if specialty in {"security", "performance", "logic", "general"} else "other"
    reason = result.reportable_reason.strip()
    invariant = check.affected_invariant.strip() or check.behavioral_question.strip()
    recommendation = (
        f"Update the changed path so it preserves: {invariant}."
        if invariant
        else "Update the changed path so it satisfies the check's report criteria."
    )
    return CandidateFinding(
        candidate_id=f"{check.check_id}:candidate",
        patch_task_id=task.id,
        file_path=check.file_path,
        line_start=check.line_start,
        line_end=check.line_end,
        content=reason[:600],
        claim_type="defect",
        failure_mode=(invariant or reason)[:400],
        evidence_summary=reason[:400],
        confidence=0.65,
        suspected_category=category,  # type: ignore[arg-type]
        reflection_specialties=[specialty],  # type: ignore[list-item]
        feedback_type="defect_detection",
        severity="medium",
        recommendation=recommendation[:400],
        expected_behavior=expected_behavior,
        behavioral_symptom=symptom,  # type: ignore[arg-type]
        root_operation=operation,  # type: ignore[arg-type]
        claim_digest=claim_digest_for_result(result, check),
        evidence_for_contract=_contract_evidence_from_check(check, result),
        counterexample=_counterexample_from_result(check, result),
        rejection_check=_rejection_check_from_result(check, result),
    )


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
    if check.audit_only:
        return bool(result.evidence_refs)
    if result.decision != "no_finding" or not result.evidence_refs or not result.suppressing_evidence:
        return False
    check_path = check.file_path.strip().replace("\\", "/")
    refs = [ref.strip().replace("\\", "/") for ref in result.evidence_refs if str(ref).strip()]
    return any(check_path in ref or ref.startswith("focused_context:") for ref in refs)


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
        if not result.expected_behavior.strip():
            expected_behavior = _expected_behavior_from_check(check, result)
            if expected_behavior:
                result = result.model_copy(update={"expected_behavior": expected_behavior})
        digest = claim_digest_for_result(result, check)
        if digest and not result.claim_digest.strip():
            result = result.model_copy(update={"claim_digest": digest})
        candidate = result.candidate
        if result.decision == "candidate" and candidate is None:
            candidate = _synthesize_candidate_from_result(
                task=task,
                check=check,
                result=result,
            )
            if candidate is not None:
                warnings.append(f"executor_candidate_payload_synthesized:{check.check_id}")
                result = result.model_copy(
                    update={
                        "candidate": candidate,
                        "warnings": list(result.warnings) + ["executor_candidate_payload_synthesized"],
                    }
                )
            else:
                warnings.append(f"executor_candidate_missing_payload:{check.check_id}")
                result = result.model_copy(
                    update={
                        "decision": "unsupported",
                        "candidate": None,
                        "missing_evidence": result.missing_evidence
                        or missing_evidence_for_weak_no_finding(
                            check,
                            evidence_requirements_for_check,
                        ),
                        "warnings": list(result.warnings) + ["executor_candidate_missing_payload"],
                    }
                )
                candidate = None
        if candidate is not None:
            candidate = candidate_with_check_behavioral_metadata(candidate, check)
            missing_contract_fields = _missing_contract_proof_field_names(candidate)
            candidate = candidate_with_check_contract_proof(candidate, check, result)
            candidate_digest = claim_digest_for_result(
                result.model_copy(update={"candidate": candidate}),
                check,
            )
            if candidate_digest and not candidate.claim_digest.strip():
                candidate = candidate.model_copy(update={"claim_digest": candidate_digest})
            if candidate_digest and not result.claim_digest.strip():
                result = result.model_copy(update={"claim_digest": candidate_digest})
            filled_contract_fields = [
                field
                for field in missing_contract_fields
                if field not in _missing_contract_proof_field_names(candidate)
            ]
            if filled_contract_fields:
                warnings.append(
                    "executor_contract_proof_backfilled:"
                    f"{check.check_id}:{','.join(filled_contract_fields)}"
                )
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
