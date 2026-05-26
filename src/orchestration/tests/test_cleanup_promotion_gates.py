"""Tests for adversarial cleanup promotion gates (Phase 2 follow-ups)."""

from __future__ import annotations

from src.domain.schemas import CandidateFinding, ReflectionReport
from src.orchestration.nodes.application.cleanup import make_adversarial_cleanup_node


def _security_cand(**kwargs) -> CandidateFinding:
    base = dict(
        candidate_id="sec-1",
        patch_task_id="task-2",
        file_path="pkg/h.py",
        line_start=10,
        line_end=20,
        content="class RegexMatch",
        claim_type="security_risk",
        failure_mode="ReDoS from unbounded regex backtracking",
        evidence_summary="pattern passed to re.search without timeout",
        recommendation="add timeout",
        reflection_specialties=["security"],
        suspected_category="security",
        severity="high",
    )
    base.update(kwargs)
    return CandidateFinding(**base)  # type: ignore[arg-type]


def test_cleanup_drops_security_risk_on_harness_error_without_focused_context() -> None:
    node = make_adversarial_cleanup_node()
    cand = _security_cand()
    out = node(
        {
            "run_id": "t",
            "git_diff": "diff --git a/pkg/h.py b/pkg/h.py\n+++ b/pkg/h.py\n+pass\n",
            "candidate_findings": [cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="security",
                    verdict="accept",
                    rationale="ReDoS plausible from user-controlled pattern.",
                ),
            ],
            "metadata": {
                "verifier_hints": {
                    cand.candidate_id: {
                        "verdict": "inconclusive",
                        "harness_error": True,
                        "product_verified": False,
                    }
                }
            },
        }
    )
    assert out["findings"] == []
    lifecycle = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"]
    assert lifecycle[cand.candidate_id]["reason"] == "security_unverified_harness_error"
