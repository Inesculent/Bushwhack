from src.domain.schemas import CandidateFinding
from src.orchestration.routing.claim_tiering import classify_claim_tier


def _candidate(**updates):
    data = {
        "candidate_id": "c1",
        "patch_task_id": "t1",
        "file_path": "x.py",
        "line_start": 1,
        "line_end": 1,
        "content": "Issue",
        "claim_type": "defect",
        "failure_mode": "",
        "evidence_summary": "",
        "recommendation": "Fix it.",
        "suspected_category": "logic",
        "reflection_specialties": ["logic"],
    }
    data.update(updates)
    return CandidateFinding(**data)


def test_direct_regression_for_removed_import_still_used() -> None:
    candidate = _candidate(
        content="The PR removed import time but the function still uses time.sleep.",
        failure_mode="NameError at runtime because removed import is still used.",
        evidence_summary="Diff removes import; source still references time.",
    )

    assert classify_claim_tier(candidate) == "direct_regression"


def test_generic_validation_is_speculative_guard() -> None:
    candidate = _candidate(
        content="Add validation for the new config value.",
        failure_mode="",
        evidence_summary="Validation would be safer.",
        recommendation="Consider validating invalid values.",
    )

    assert classify_claim_tier(candidate) == "speculative_guard"


def test_missing_test_with_specific_changed_behavior_is_coverage_gap() -> None:
    candidate = _candidate(
        claim_type="missing_test",
        content="Missing test for the changed retry default.",
        failure_mode="The changed default can break transient request retries.",
        evidence_summary="The PR changes retry behavior without coverage.",
        recommendation="Add a regression test for transient failures.",
    )

    assert classify_claim_tier(candidate) == "coverage_gap"


def test_kb_backed_contract_claim_is_contract_regression() -> None:
    candidate = _candidate(
        content="The new default violates the public API contract.",
        failure_mode="Contract mismatch for existing callers.",
        evidence_summary="The default changed from None to 0.",
    )

    assert classify_claim_tier(candidate, review_kb_context="- summary `api`: default is None") == "contract_regression"
