"""BehavioralSpec schema."""

from src.domain.schemas import BehavioralEvidenceRef, BehavioralSpec, ReviewCheck, ReviewTask


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
