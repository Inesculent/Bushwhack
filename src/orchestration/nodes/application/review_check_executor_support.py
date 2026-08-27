"""Executor support helpers for review-check nodes."""

from __future__ import annotations

from typing import Any, Callable, Iterable, List, Mapping

from src.domain.schemas import CandidateFinding, ContractSourceRef, ReviewCheck, ReviewCheckResult, ReviewTask
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


def _expected_behavior_from_check(check: ReviewCheck, result: ReviewCheckResult | None = None) -> str:
    if result is not None and result.expected_behavior.strip():
        return result.expected_behavior.strip()[:500]
    if check.expected_behavior.strip():
        return check.expected_behavior.strip()[:500]
    return check.affected_invariant.strip()[:500]


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


def missing_evidence_for_unanswered_check(
    check: ReviewCheck,
    evidence_requirements_for_check: Callable[[ReviewCheck], List[str]],
) -> List[str]:
    """Retrieval hints for a check the executor never answered (omitted, failed, or truncated).

    Downgraded answers do not use this: their retrieval targets come only from
    what the executor itself named as missing.
    """
    requirements = evidence_requirements_for_check(check)
    if requirements:
        return requirements[:3]
    return list(check.required_evidence[:3])


def _retrieval_targets(result: ReviewCheckResult) -> List[str]:
    """What the executor said is missing, contract source first; never the check's own requirement text."""
    items = [result.missing_contract_source, *result.missing_evidence]
    return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def no_finding_has_strong_suppression(result: ReviewCheckResult, check: ReviewCheck) -> bool:
    if check.audit_only:
        return bool(result.evidence_refs)
    if result.decision != "no_finding" or not result.evidence_refs or not result.suppressing_evidence:
        return False
    check_path = check.file_path.strip().replace("\\", "/")
    refs = [ref.strip().replace("\\", "/") for ref in result.evidence_refs if str(ref).strip()]
    return any(check_path in ref or ref.startswith("focused_context:") for ref in refs)


def _answer_scope_is_neighboring(result: ReviewCheckResult) -> bool:
    scope = result.answer_scope.strip().lower()
    if not scope:
        return False
    exact_markers = ("exact", "assigned", "same contract", "direct")
    if any(marker in scope for marker in exact_markers):
        return False
    neighboring_markers = (
        "neighbor",
        "nearby",
        "adjacent",
        "different",
        "broader",
        "broad",
        "off-scope",
        "off scope",
        "wrong question",
        "operation-only",
        "operation only",
    )
    return any(marker in scope for marker in neighboring_markers)


def _answer_scope_is_exact(result: ReviewCheckResult) -> bool:
    scope = result.answer_scope.strip().lower()
    if not scope:
        return False
    exact_markers = ("exact", "assigned", "same contract", "direct")
    return any(marker in scope for marker in exact_markers)


def _suppression_basis_is_empty_or_generic(result: ReviewCheckResult) -> bool:
    basis = result.suppression_basis.strip().lower()
    if not basis:
        basis = " ".join(item.strip() for item in result.suppressing_evidence if str(item).strip()).lower()
    if not basis:
        return True
    stripped = basis.strip(".:- ")
    if stripped in {"none", "n/a", "na", "not applicable", "unknown"}:
        return True
    generic_markers = (
        "no issue found",
        "appears safe",
        "seems safe",
        "looks correct",
        "works as intended",
        "handled correctly",
        "no evidence",
        "insufficient evidence",
        "not applicable",
    )
    return any(marker in basis for marker in generic_markers)


def _contract_source_is_referenced(source: ContractSourceRef | None) -> bool:
    return source is not None and bool(source.ref.strip())


def _schema_source_answers_mode_check(result: ReviewCheckResult, check: ReviewCheck) -> bool:
    """A supported, schema-sourced suppression answers a declared-mode/fallback check exactly."""
    check_blob = " ".join(
        [
            check.behavioral_question,
            check.affected_invariant,
            check.expected_behavior,
            " ".join(check.suppress_criteria),
        ]
    ).lower()
    if not any(marker in check_blob for marker in ("mode", "enum", "option", "fallback")):
        return False
    if not any(marker in check_blob for marker in ("unexpected", "invalid", "schema enforcement", "declared")):
        return False
    source = result.contract_source
    return (
        result.contract_status == "supported"
        and source is not None
        and source.kind == "schema"
        and bool(source.ref.strip())
        and bool(result.evidence_refs)
    )


def _candidate_contract_reason(result: ReviewCheckResult) -> str:
    """Why a candidate is not contract-backed; empty when it is."""
    if result.contract_status != "contradicted":
        return f"contract_{result.contract_status}"
    if not _contract_source_is_referenced(result.contract_source):
        return "contract_source_unreferenced"
    return ""


def _no_finding_downgrade_reason(
    state: GraphState,
    result: ReviewCheckResult,
    check: ReviewCheck,
    *,
    schema_exact: bool,
) -> str:
    """Why a no_finding cannot stand; empty when implementation and contract evidence both support it."""
    if _answer_scope_is_neighboring(result):
        return "neighboring_answer_scope"
    degraded = _focused_context_degraded_for_check(state, check)
    if degraded:
        return degraded[0]
    if not check.audit_only and _suppression_basis_is_empty_or_generic(result):
        return "generic_suppression_basis"
    if (
        _check_requires_exact_transformation_suppression(check)
        and not schema_exact
        and (
            not _answer_scope_is_exact(result)
            or _suppression_basis_is_empty_or_generic(result)
            or not _suppression_basis_has_value_flow(result)
        )
    ):
        return "missing_exact_transformation_scope"
    if result.contract_status != "supported":
        return f"contract_{result.contract_status}"
    if not _contract_source_is_referenced(result.contract_source):
        return "contract_source_unreferenced"
    return ""


def _check_requires_exact_transformation_suppression(check: ReviewCheck) -> bool:
    if check.audit_only:
        return False
    families = {
        check.issue_family.strip().lower(),
        check.diff_signal_family.strip().lower(),
        check.lens.strip().lower() if isinstance(check.lens, str) else "",
    }
    if families & {
        "data_preservation_cardinality",
        "serialization_type_closure",
        "aggregation_cardinality",
        "index_bounds",
        "data_shape_consistency",
    }:
        return True
    blob = " ".join(
        [
            check.owned_contract_scope,
            check.behavioral_question,
            check.affected_invariant,
            check.expected_behavior,
        ]
    ).lower()
    return any(
        marker in blob
        for marker in (
            "producer",
            "projection",
            "selection",
            "aggregation",
            "serialization",
            "type-closure",
            "type closure",
            "join",
            "cardinality",
            "field",
            "element",
            "group",
            "nested",
            "structured",
            "return shape",
            "output shape",
            "payload",
        )
    )


def _suppression_basis_has_value_flow(result: ReviewCheckResult) -> bool:
    basis = " ".join(
        [
            result.suppression_basis,
            " ".join(result.suppressing_evidence),
            result.reportable_reason,
        ]
    ).lower()
    if not basis.strip():
        return False
    produced = any(
        marker in basis
        for marker in (
            "produced",
            "input shape",
            "source value",
            "original value",
            "before the operation",
            "pre-operation",
        )
    )
    selected = any(
        marker in basis
        for marker in (
            "selected",
            "transformed",
            "projected",
            "extracted",
            "normalized",
            "field",
            "element",
            "group",
        )
    )
    consumed = any(
        marker in basis
        for marker in (
            "returned",
            "joined",
            "serialized",
            "consumed",
            "output shape",
            "after the operation",
            "post-operation",
        )
    )
    intentional = "intentionally narrowed" in basis or "documented projection" in basis
    return intentional or (produced and selected and consumed)


def _focused_context_degraded_for_check(state: GraphState, check: ReviewCheck) -> list[str]:
    metadata = state.get("metadata", {}) or {}
    fc = metadata.get("focused_context", {}) if isinstance(metadata, Mapping) else {}
    diagnostics = fc.get("diagnostics", []) if isinstance(fc, Mapping) else []
    reasons: list[str] = []
    for row in diagnostics if isinstance(diagnostics, list) else []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("candidate_id") or "") != check.check_id:
            continue
        outcomes = {str(item) for item in row.get("outcomes", []) or []}
        reason = str(row.get("reason") or "")
        if reason:
            outcomes.add(reason)
        for outcome, reason in (
            ("no_hits", "focused_context_no_hits"),
            ("tool_unavailable", "focused_context_tool_unavailable"),
            ("path_mismatch", "focused_context_path_mismatch"),
        ):
            if outcome in outcomes:
                reasons.append(reason)
    return list(dict.fromkeys(reasons))


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

    def _downgrade(
        result: ReviewCheckResult,
        check: ReviewCheck,
        *,
        reason_warning: str,
    ) -> ReviewCheckResult:
        next_decision = "unsupported" if check_budget_remaining(state, check) else "budget_exhausted"
        next_warnings = [reason_warning]
        if next_decision == "budget_exhausted":
            next_warnings.append("review_check_budget_exhausted")
        return result.model_copy(
            update={
                "decision": next_decision,
                "candidate": None,
                "missing_evidence": _retrieval_targets(result),
                "warnings": list(result.warnings) + next_warnings,
            }
        )

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
            warnings.append(f"executor_candidate_missing_payload:{check.check_id}")
            result = result.model_copy(
                update={
                    "decision": "unsupported",
                    "candidate": None,
                    "missing_evidence": _retrieval_targets(result),
                    "warnings": list(result.warnings) + ["executor_candidate_missing_payload"],
                }
            )
            candidate = None
        if candidate is not None:
            candidate = candidate_with_check_behavioral_metadata(candidate, check)
            missing_contract_fields = _missing_contract_proof_field_names(candidate)
            if missing_contract_fields:
                warning = (
                    "executor_candidate_missing_contract_proof:"
                    f"{check.check_id}:{','.join(missing_contract_fields)}"
                )
                warnings.append(warning)
                result = result.model_copy(
                    update={
                        "decision": "unsupported",
                        "candidate": None,
                        "missing_evidence": list(
                            dict.fromkeys(
                                [
                                    *result.missing_evidence,
                                    *(f"candidate.{field}" for field in missing_contract_fields),
                                ]
                            )
                        ),
                        "warnings": list(result.warnings) + ["executor_candidate_missing_contract_proof"],
                    }
                )
                candidate = None
        if candidate is not None:
            candidate_digest = claim_digest_for_result(
                result.model_copy(update={"candidate": candidate}),
                check,
            )
            if candidate_digest and not candidate.claim_digest.strip():
                candidate = candidate.model_copy(update={"claim_digest": candidate_digest})
            if candidate_digest and not result.claim_digest.strip():
                result = result.model_copy(update={"claim_digest": candidate_digest})
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
        # A candidate must be backed by a referenced contract source that the implementation contradicts.
        if result.decision == "candidate" and result.candidate is not None:
            reason = _candidate_contract_reason(result)
            if reason:
                warnings.append(f"executor_candidate_contract_unbacked:{check.check_id}:{reason}")
                result = _downgrade(result, check, reason_warning=f"candidate_contract_unbacked:{reason}")
        # A no_finding must be backed by implementation evidence and a supported, referenced contract source.
        if result.decision == "no_finding":
            schema_exact = _schema_source_answers_mode_check(result, check)
            if schema_exact:
                suppression_basis = result.suppression_basis.strip() or " ".join(
                    item.strip() for item in result.suppressing_evidence if str(item).strip()
                )
                result = result.model_copy(
                    update={
                        "answer_scope": result.answer_scope.strip()
                        or f"exact: {check.owned_contract_scope or check.behavioral_question}",
                        "suppression_basis": suppression_basis[:500],
                    }
                )
            reason = _no_finding_downgrade_reason(state, result, check, schema_exact=schema_exact)
            if reason:
                warnings.append(f"executor_exact_question_mismatch:{check.check_id}:{reason}")
                result = _downgrade(result, check, reason_warning=f"exact_question_mismatch:{reason}")
        if result.decision == "no_finding" and not no_finding_has_strong_suppression(result, check):
            warnings.append(f"executor_weak_no_finding_downgraded:{check.check_id}")
            result = _downgrade(result, check, reason_warning="weak_no_finding_requires_more_evidence")
        if result.decision == "unsupported":
            targets = _retrieval_targets(result)
            if targets != list(result.missing_evidence):
                result = result.model_copy(update={"missing_evidence": targets})
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
                        missing_evidence=missing_evidence_for_unanswered_check(
                            check,
                            evidence_requirements_for_check,
                        ),
                        warnings=[missing_result_warning],
                    )
                )
    return normalized, warnings
