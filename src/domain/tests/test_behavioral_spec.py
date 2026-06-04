"""BehavioralSpec schema."""

from src.domain.schemas import (
    BehavioralEvidenceRef,
    BehavioralSpec,
    CandidateFinding,
    ReviewCheck,
    ReviewCheckResult,
    ReviewFinding,
    ReviewTask,
)


def test_behavioral_spec_defaults() -> None:
    s = BehavioralSpec(intent_summary="x")
    assert s.confidence == 0.5
    assert s.evidence_refs == []
    assert s.surfaces == []
    assert s.surface_invariants == []


def test_behavioral_evidence_ref() -> None:
    r = BehavioralEvidenceRef(kind="file", ref="src/a.py", note="touched")
    assert r.kind == "file"


def test_old_artifacts_load_without_surface_fields() -> None:
    spec = BehavioralSpec.model_validate({"intent_summary": "old"})
    task = ReviewTask.model_validate(
        {"id": "t1", "title": "T", "description": "D", "target_files": ["src/a.py"]}
    )
    check = ReviewCheck.model_validate(
        {
            "check_id": "c1",
            "patch_task_id": "t1",
            "file_path": "src/a.py",
            "changed_code_anchor": "handle",
            "behavioral_question": "Does handle preserve the expected behavior?",
            "affected_invariant": "contract",
            "required_evidence": ["code"],
            "suppress_criteria": ["ok"],
            "report_criteria": ["bad"],
            "allowed_retrieval": ["task_evidence"],
        }
    )

    assert spec.surfaces == []
    assert spec.surface_invariants == []
    assert task.surface_ids == []
    assert check.surface_ids == []
    assert check.owned_contract_scope == ""
    assert check.issue_family == ""
    assert check.diff_signal_family == ""
    assert check.diff_signal == ""
    assert check.audit_only is False


def test_old_artifacts_load_without_contract_proof_fields() -> None:
    result = ReviewCheckResult.model_validate(
        {
            "check_id": "c1",
            "patch_task_id": "t1",
            "decision": "candidate",
        }
    )
    candidate = CandidateFinding.model_validate(
        {
            "candidate_id": "cand1",
            "patch_task_id": "t1",
            "file_path": "src/a.py",
            "line_start": 1,
            "line_end": 1,
            "content": "Issue",
        }
    )
    finding = ReviewFinding.model_validate(
        {
            "id": "f1",
            "file_path": "src/a.py",
            "line_start": 1,
            "line_end": 1,
            "content": "Issue",
        }
    )

    assert result.evidence_for_contract == ""
    assert result.expected_behavior == ""
    assert result.counterexample == ""
    assert result.rejection_check == ""
    assert result.claim_digest == ""
    assert candidate.evidence_for_contract == ""
    assert candidate.expected_behavior == ""
    assert candidate.counterexample == ""
    assert candidate.rejection_check == ""
    assert candidate.claim_digest == ""
    assert finding.evidence_for_contract == ""
    assert finding.expected_behavior == ""
    assert finding.counterexample == ""
    assert finding.rejection_check == ""
    assert finding.claim_digest == ""


def test_expected_behavior_round_trips_across_review_artifacts() -> None:
    check = ReviewCheck.model_validate(
        {
            "check_id": "c1",
            "patch_task_id": "t1",
            "file_path": "src/a.py",
            "changed_code_anchor": "handle",
            "behavioral_question": "Does handle preserve the result?",
            "affected_invariant": "result contract",
            "expected_behavior": "handle returns the declared result on every reachable path.",
            "required_evidence": ["code"],
            "suppress_criteria": ["all paths return"],
            "report_criteria": ["a path returns None"],
            "allowed_retrieval": ["task_evidence"],
        }
    )
    result = ReviewCheckResult(
        check_id="c1",
        patch_task_id="t1",
        decision="candidate",
        expected_behavior=check.expected_behavior,
    )
    candidate = CandidateFinding(
        candidate_id="cand1",
        patch_task_id="t1",
        file_path="src/a.py",
        line_start=1,
        line_end=1,
        content="Issue",
        expected_behavior=check.expected_behavior,
    )
    finding = ReviewFinding(
        id="f1",
        file_path="src/a.py",
        line_start=1,
        line_end=1,
        content="Issue",
        expected_behavior=check.expected_behavior,
    )

    assert ReviewCheck.model_validate(check.model_dump()).expected_behavior == check.expected_behavior
    assert ReviewCheckResult.model_validate(result.model_dump()).expected_behavior == check.expected_behavior
    assert CandidateFinding.model_validate(candidate.model_dump()).expected_behavior == check.expected_behavior
    assert ReviewFinding.model_validate(finding.model_dump()).expected_behavior == check.expected_behavior
