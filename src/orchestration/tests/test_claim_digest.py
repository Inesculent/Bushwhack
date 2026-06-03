from src.domain.schemas import CandidateFinding, ReviewCheck, ReviewCheckResult
from src.orchestration.routing.claim_digest import (
    claim_digest_for_candidate,
    claim_digest_for_result,
    owned_contract_scope_for_check,
)


def _candidate(**kwargs) -> CandidateFinding:
    base = {
        "candidate_id": "c1",
        "patch_task_id": "t",
        "file_path": "src/tool.py",
        "line_start": 10,
        "line_end": 20,
        "content": "Parser.run drops values in 'Batch' mode.",
        "claim_type": "defect",
        "failure_mode": "data loss",
        "evidence_summary": "Only the first element is kept.",
        "recommendation": "Preserve every element.",
        "reflection_specialties": ["logic"],
        "suspected_category": "logic",
        "behavioral_symptom": "data_loss",
        "root_operation": "aggregation",
        "evidence_for_contract": "The mode name promises batch output.",
        "counterexample": "Batch with two elements emits one.",
        "rejection_check": "No intentional narrowing is documented.",
    }
    base.update(kwargs)
    return CandidateFinding(**base)  # type: ignore[arg-type]


def test_claim_digest_is_stable_for_same_contract_with_different_wording() -> None:
    left = claim_digest_for_candidate(_candidate(content="Parser.run drops tuple fields in 'Batch' mode."))
    right = claim_digest_for_candidate(
        _candidate(
            candidate_id="c2",
            content="Parser.run omits remaining fields in 'Batch' mode.",
            evidence_summary="The changed path keeps one field.",
        )
    )

    assert left == right


def test_claim_digest_distinguishes_nearby_contracts() -> None:
    batch = claim_digest_for_candidate(_candidate(content="Parser.run drops tuple fields in 'Batch' mode."))
    empty = claim_digest_for_candidate(
        _candidate(
            candidate_id="c2",
            content="Parser.run falls through in 'Empty' mode.",
            failure_mode="missing return",
            evidence_summary="No fallback branch returns.",
            behavioral_symptom="missing_return",
            root_operation="dispatch",
            counterexample="Empty input reaches the terminal branch.",
        )
    )

    assert batch != empty
    assert "variant=batch" in batch
    assert "variant=empty" in empty


def test_check_scope_and_result_digest_are_synthesized() -> None:
    check = ReviewCheck(
        check_id="chk",
        patch_task_id="t",
        file_path="src/tool.py",
        changed_code_anchor="Parser.run",
        behavioral_question="Does 'Batch' mode preserve every tuple field?",
        affected_invariant="Batch mode preserves tuple cardinality.",
        report_criteria=["Reports data loss when only one tuple field is kept."],
    )
    result = ReviewCheckResult(
        check_id="chk",
        patch_task_id="t",
        decision="candidate",
        reportable_reason="Only one tuple field is kept.",
        evidence_for_contract="Batch mode promises cardinality.",
        counterexample="Two tuple fields become one.",
        rejection_check="No caller guarantee narrows it.",
    )

    assert "parser_run" in owned_contract_scope_for_check(check)
    assert claim_digest_for_result(result, check).startswith("src/tool.py::parser_run")
