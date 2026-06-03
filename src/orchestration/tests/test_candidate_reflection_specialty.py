"""Tests for single-specialty hardcap routing."""

from __future__ import annotations

import pytest

from src.domain.schemas import CandidateFinding, ReviewTask
from src.orchestration.routing.normalize_critiquer_candidates import normalize_critiquer_candidates
from src.orchestration.routing.candidate_reflection_specialty import (
    correct_specialty_before_hardcap,
    normalize_reflection_specialty_hardcap,
    with_single_reflection_specialty,
)


def _base_candidate(**kwargs) -> CandidateFinding:
    defaults: dict = {
        "candidate_id": "t1:c1",
        "patch_task_id": "t1",
        "file_path": "src/x.py",
        "line_start": 1,
        "line_end": 2,
        "content": "x",
    }
    defaults.update(kwargs)
    return CandidateFinding(**defaults)  # type: ignore[arg-type]


def test_hardcap_picks_security_over_performance() -> None:
    c = _base_candidate(
        reflection_specialties=["performance", "security"],
        claim_type="defect",
    )
    assert normalize_reflection_specialty_hardcap(c) == "security"


def test_hardcap_picks_logic_over_general() -> None:
    c = _base_candidate(
        reflection_specialties=["general", "logic"],
        claim_type="defect",
    )
    assert normalize_reflection_specialty_hardcap(c) == "logic"


def test_infer_from_claim_type_security_risk() -> None:
    c = _base_candidate(
        reflection_specialties=[],
        claim_type="security_risk",
        suspected_category="other",
    )
    assert normalize_reflection_specialty_hardcap(c) == "security"


def test_text_hints_do_not_override_declared_claim_shape() -> None:
    c = _base_candidate(
        reflection_specialties=[],
        claim_type="defect",
        suspected_category="other",
        content="",
        failure_mode="ReDoS from attacker-controlled regex",
        evidence_summary="",
    )
    assert normalize_reflection_specialty_hardcap(c) == "logic"


def test_normalize_candidates_collapses_to_single_entry() -> None:
    task = ReviewTask(id="t1", title="t", description="d", target_files=["src/x.py"])
    raw = _base_candidate(
        candidate_id="c1",
        patch_task_id="legacy",
        reflection_specialties=["logic", "general"],
        claim_type="defect",
    )
    out, _, _ = normalize_critiquer_candidates(task, [raw])
    assert len(out) == 1
    assert out[0].reflection_specialties == ["logic"]
    assert out[0].candidate_id.startswith("t1")


@pytest.mark.parametrize(
    ("specialties", "expected"),
    [
        (["security", "performance", "logic"], "security"),
        (["performance", "general"], "performance"),
    ],
)
def test_hardcap_priority_order(specialties: list[str], expected: str) -> None:
    c = _base_candidate(reflection_specialties=specialties, claim_type="defect")
    assert normalize_reflection_specialty_hardcap(c) == expected


def test_correct_specialty_security_risk_over_performance_tag() -> None:
    c = _base_candidate(
        reflection_specialties=["performance"],
        claim_type="security_risk",
        suspected_category="performance",
    )
    corrected, reason = correct_specialty_before_hardcap(c)
    assert corrected.reflection_specialties == ["security"]
    assert reason == "specialty_corrected:security_risk"


def test_with_single_reflection_specialty_returns_one_element_list() -> None:
    c = _base_candidate(reflection_specialties=["general", "security"])
    coerced = with_single_reflection_specialty(c)
    assert coerced.reflection_specialties == ["security"]
