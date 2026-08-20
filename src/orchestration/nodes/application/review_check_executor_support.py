"""Executor support helpers for review-check nodes."""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, List, Mapping

from src.domain.schemas import CandidateFinding, ReviewCheck, ReviewCheckResult, ReviewTask
from src.domain.state import GraphState
from src.orchestration.nodes.application.critiquer import _normalize_candidates
from src.orchestration.nodes.application.review_check_source_scope import check_requires_contract_justification
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


def _suppression_basis_is_operation_only(result: ReviewCheckResult) -> bool:
    basis = " ".join(
        [
            result.suppression_basis,
            " ".join(result.suppressing_evidence),
        ]
    ).strip().lower()
    if not basis:
        return False
    markers = (
        "only repeats",
        "merely repeats",
        "operation exists",
        "same operation",
        "risky operation",
        "operation-only",
        "operation only",
    )
    return any(marker in basis for marker in markers)


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


def _schema_enforcement_is_exact_mode_suppression(
    result: ReviewCheckResult,
    check: ReviewCheck,
) -> bool:
    """Recognize declared-enum enforcement as exact evidence for mode reachability."""
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
    suppression_blob = " ".join(
        [
            result.suppression_basis,
            " ".join(result.suppressing_evidence),
        ]
    ).lower()
    return bool(result.evidence_refs) and any(
        marker in suppression_blob
        for marker in (
            "declared mode enum",
            "declared enum",
            "schema enforces",
            "schema restricts",
            "only visible options",
            "allowed options",
        )
    )


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


def _normalize_contract_identity(text: str) -> str:
    chars = [char.lower() if char.isalnum() else " " for char in text]
    return " ".join("".join(chars).split())


def _suppression_omits_scope_variant(result: ReviewCheckResult, check: ReviewCheck) -> bool:
    if not _answer_scope_is_exact(result):
        return False
    scope = check.owned_contract_scope.strip()
    if not scope:
        return False
    basis = " ".join(
        [
            result.answer_scope,
            result.claim_digest,
            result.suppression_basis,
            " ".join(result.suppressing_evidence),
            result.reportable_reason,
        ]
    )
    normalized_basis = _normalize_contract_identity(basis)
    if not normalized_basis:
        return True
    normalized_scope = _normalize_contract_identity(scope)
    if normalized_scope and normalized_scope in normalized_basis:
        return False
    parts = [_normalize_contract_identity(part) for part in scope.split(":")]
    parts = [part for part in parts if len(part) >= 3]
    if parts and all(part in normalized_basis for part in parts):
        return False
    return True


def _suppression_has_contract_justification(result: ReviewCheckResult) -> bool:
    blob = " ".join(
        [
            result.evidence_for_contract,
            result.suppression_basis,
            " ".join(result.suppressing_evidence),
            result.answer_scope,
        ]
    ).lower()
    if not blob.strip():
        return False
    markers = (
        "old behavior",
        "prior behavior",
        "pr intent",
        "pull request intent",
        "documented",
        "documentation",
        "docstring",
        "test",
        "schema",
        "declared",
        "caller",
        "call site",
        "downstream",
        "consumer",
        "framework",
        "repository convention",
        "repo convention",
        "project convention",
        "public api",
        "api contract",
        "type declaration",
        "input_types",
        "input types",
        "return_types",
        "return types",
        "intentional narrowing",
        "representation invariant",
    )
    return any(marker in blob for marker in markers)


_VARIANT_MODE_PHRASE = re.compile(
    r"(?:"
    r"['\"]([^'\"]{2,48})['\"]\s+mode"
    r"|mode\s*(?:==|=|:)?\s*['\"]([^'\"]{2,48})['\"]"
    r"|\b((?:[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,3}))\s+mode\b"
    r")",
    re.IGNORECASE,
)
_SIBLING_VARIANT_MARKERS = (
    "other mode",
    "another mode",
    "different mode",
    "alternate mode",
    "alternative mode",
    "sibling mode",
    "separate mode",
)


def _extract_variant_mode_phrases(text: str) -> set[str]:
    phrases: set[str] = set()
    for match in _VARIANT_MODE_PHRASE.finditer(text or ""):
        raw = next((group for group in match.groups() if group), "")
        normalized = _normalize_contract_identity(raw)
        if len(normalized) >= 3:
            phrases.add(normalized)
    return phrases


def _variant_sets_overlap(left: set[str], right: set[str]) -> bool:
    for item in left:
        for other in right:
            if item == other or item in other or other in item:
                return True
    return False


def _suppression_displaces_owned_variant(result: ReviewCheckResult, check: ReviewCheck) -> bool:
    """True when suppression answers via a sibling variant instead of the owned one."""
    owned_blob = " ".join(
        [
            check.behavioral_question,
            check.owned_contract_scope,
            check.expected_behavior,
            check.affected_invariant,
            check.diff_signal,
        ]
    )
    suppress_blob = " ".join(
        [
            result.suppression_basis,
            " ".join(result.suppressing_evidence),
            result.reportable_reason,
            result.claim_digest,
        ]
    )
    owned = _extract_variant_mode_phrases(owned_blob)
    cited = _extract_variant_mode_phrases(suppress_blob)
    if not owned:
        return False
    suppress_lower = suppress_blob.lower()
    if any(marker in suppress_lower for marker in _SIBLING_VARIANT_MARKERS):
        if not _variant_sets_overlap(owned, cited):
            return True
    if not cited:
        return False
    cites_owned = _variant_sets_overlap(owned, cited)
    cites_extra = any(
        not any(item == other or item in other or other in item for other in owned)
        for item in cited
    )
    return cites_extra and not cites_owned


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
        schema_exact_suppression = (
            result.decision in {"no_finding", "suppressed"}
            and _schema_enforcement_is_exact_mode_suppression(result, check)
        )
        if schema_exact_suppression:
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
        exact_transformation_required = (
            _check_requires_exact_transformation_suppression(check)
            and not schema_exact_suppression
        )
        exact_transformation_mismatch = exact_transformation_required and (
            not _answer_scope_is_exact(result)
            or _suppression_basis_is_empty_or_generic(result)
            or not _suppression_basis_has_value_flow(result)
        )
        omitted_scope_variant = (
            _suppression_omits_scope_variant(result, check)
            if not schema_exact_suppression
            else False
        )
        displaced_owned_variant = (
            _suppression_displaces_owned_variant(result, check)
            if not schema_exact_suppression
            else False
        )
        missing_contract_justification = (
            check_requires_contract_justification(check)
            and result.decision in {"no_finding", "suppressed"}
            and not _suppression_has_contract_justification(result)
        )
        focused_degradation = _focused_context_degraded_for_check(state, check)
        if result.decision in {"no_finding", "suppressed"} and (
            _answer_scope_is_neighboring(result)
            or _suppression_basis_is_operation_only(result)
            or (not check.audit_only and _suppression_basis_is_empty_or_generic(result))
            or omitted_scope_variant
            or missing_contract_justification
            or displaced_owned_variant
            or bool(focused_degradation)
            or exact_transformation_mismatch
        ):
            reason = (
                "neighboring_answer_scope"
                if _answer_scope_is_neighboring(result)
                else (
                    focused_degradation[0]
                    if focused_degradation
                    else (
                        "operation_only_suppression"
                        if _suppression_basis_is_operation_only(result)
                        else (
                            "generic_suppression_basis"
                            if not check.audit_only and _suppression_basis_is_empty_or_generic(result)
                            else (
                                "missing_exact_transformation_scope"
                                if exact_transformation_mismatch and not _answer_scope_is_exact(result)
                                else (
                                    "cross_variant_displacement"
                                    if displaced_owned_variant
                                    else (
                                        "missing_owned_scope_variant"
                                        if omitted_scope_variant
                                        else (
                                            "missing_exact_transformation_scope"
                                            if exact_transformation_mismatch
                                            else "missing_contract_justification"
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
            warnings.append(f"executor_exact_question_mismatch:{check.check_id}:{reason}")
            next_decision = "unsupported" if check_budget_remaining(state, check) else "budget_exhausted"
            next_warnings = [f"exact_question_mismatch:{reason}"]
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
                        missing_evidence=missing_evidence_for_weak_no_finding(
                            check,
                            evidence_requirements_for_check,
                        ),
                        warnings=[missing_result_warning],
                    )
                )
    return normalized, warnings
