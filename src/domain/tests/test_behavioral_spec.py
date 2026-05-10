"""BehavioralSpec schema."""

from src.domain.schemas import BehavioralEvidenceRef, BehavioralSpec


def test_behavioral_spec_defaults() -> None:
    s = BehavioralSpec(intent_summary="x")
    assert s.confidence == 0.5
    assert s.evidence_refs == []


def test_behavioral_evidence_ref() -> None:
    r = BehavioralEvidenceRef(kind="file", ref="src/a.py", note="touched")
    assert r.kind == "file"
