"""Tests for reflection report consolidation and rationale consistency."""

from __future__ import annotations

from src.domain.schemas import CandidateFinding, ReflectionReport
from src.orchestration.nodes.application.cleanup import make_adversarial_cleanup_node
from src.orchestration.nodes.application.reflection import _enforce_rationale_consistency
from src.orchestration.routing.reflection_consolidation import (
    consolidate_reflection_reports,
    pick_preferred_report,
)


def test_consolidate_prefers_accept_over_reject() -> None:
    accept = ReflectionReport(
        candidate_id="c1",
        reflector_specialty="logic",
        verdict="accept",
        rationale="Missing else returns None.",
    )
    reject = ReflectionReport(
        candidate_id="c1",
        reflector_specialty="logic",
        verdict="reject",
        rationale="No bug.",
    )
    out = consolidate_reflection_reports([reject, accept])
    assert len(out) == 1
    assert out[0].verdict == "accept"


def test_pick_preferred_report_needs_verification_over_reject() -> None:
    reject = ReflectionReport(
        candidate_id="c1",
        reflector_specialty="logic",
        verdict="reject",
        rationale="",
    )
    nv = ReflectionReport(
        candidate_id="c1",
        reflector_specialty="logic",
        verdict="needs_verification",
        rationale="",
    )
    assert pick_preferred_report(reject, nv).verdict == "needs_verification"


def test_rationale_missing_return_does_not_auto_reject_accept() -> None:
    report = ReflectionReport(
        candidate_id="c1",
        reflector_specialty="logic",
        verdict="accept",
        rationale="Missing return causes implicit None downstream.",
    )
    updated, warn = _enforce_rationale_consistency(report)
    assert warn is None
    assert updated.verdict == "accept"


def test_cleanup_promotes_when_duplicate_logic_accept_beats_reject() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="logic-dup-1",
        patch_task_id="t1",
        file_path="src/x.py",
        line_start=10,
        line_end=15,
        content="if mode == 'a': return True",
        claim_type="defect",
        failure_mode="Implicit None when mode unrecognized.",
        evidence_summary="No else branch.",
        evidence_for_contract="The mode dispatch is expected to return a boolean for each supported path.",
        counterexample="Calling with an unrecognized mode falls through without returning.",
        rejection_check="No caller guarantee or intentional narrowing is shown.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        recommendation="Add else returning False.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="logic",
            verdict="reject",
            rationale="False positive.",
        ),
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="logic",
            verdict="accept",
            rationale="Missing else is a defect.",
        ),
    ]
    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "metadata": {},
        }
    )
    assert len(out["findings"]) == 1
