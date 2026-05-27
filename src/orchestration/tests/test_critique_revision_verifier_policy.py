"""Tests for critique-revision verifier policy and ReDoS dedupe."""

from __future__ import annotations

from src.domain.schemas import CandidateFinding
from src.domain.state import GraphState
from src.orchestration.nodes.application.critique_revision import (
    _apply_verifier_policy_to_revisions,
    _dedupe_revision_candidate_ids,
)


def _cand(cid: str, *, claim_type: str = "security_risk", content: str = "ReDoS risk") -> CandidateFinding:
    return CandidateFinding(
        candidate_id=cid,
        patch_task_id="1",
        file_path="comfy_extras/nodes_string.py",
        line_start=1,
        line_end=2,
        content=content,
        claim_type=claim_type,  # type: ignore[arg-type]
        failure_mode="catastrophic backtracking on user regex",
        evidence_summary="uses re.search without timeout",
        recommendation="add timeout",
        reflection_specialties=["security"],
        suspected_category="security",
    )


def test_dedupe_redos_candidates_same_file() -> None:
    state: GraphState = {
        "candidate_findings": [
            _cand("1:task1_1"),
            _cand("1:task1_2", content="another ReDoS on RegexExtract"),
        ],
    }
    out = _dedupe_revision_candidate_ids(state, ["1:task1_1", "1:task1_2"])
    assert out == ["1:task1_1"]


def test_apply_verifier_policy_refuted_forces_reject() -> None:
    state: GraphState = {
        "candidate_findings": [
            CandidateFinding(
                candidate_id="c1",
                patch_task_id="1",
                file_path="m.py",
                line_start=1,
                line_end=2,
                content="raise",
                claim_type="defect",
                failure_mode="IndexError when index out of range",
                evidence_summary="crash",
                recommendation="fix bounds",
                reflection_specialties=["logic"],
                suspected_category="logic",
            )
        ],
        "metadata": {
            "verifier_hints": {
                "c1": {
                    "verdict": "refuted",
                    "verification_scope": "concrete_behavior",
                    "harness_error": False,
                    "product_verified": True,
                }
            }
        },
    }
    rows, warnings = _apply_verifier_policy_to_revisions(
        [{"candidate_id": "c1", "verdict": "accept", "updated_evidence_summary": "still bad"}],
        state,
    )
    assert rows[0]["verdict"] == "reject"
    assert any("critique_revision_verifier_refuted" in w for w in warnings)


def test_apply_verifier_policy_harness_annotates_summary() -> None:
    state: GraphState = {
        "metadata": {
            "verifier_hints": {
                "c1": {
                    "verdict": "verified",
                    "verification_scope": "concrete_behavior",
                    "harness_error": True,
                }
            }
        }
    }
    rows, _ = _apply_verifier_policy_to_revisions(
        [{"candidate_id": "c1", "verdict": "accept", "updated_evidence_summary": ""}],
        state,
    )
    assert "runtime unverified (harness)" in rows[0]["updated_evidence_summary"]


def test_apply_verifier_policy_refuted_wrong_output_does_not_force_reject() -> None:
    state: GraphState = {
        "candidate_findings": [
            CandidateFinding(
                candidate_id="c1",
                patch_task_id="1",
                file_path="m.py",
                line_start=1,
                line_end=2,
                content="join",
                claim_type="defect",
                failure_mode="Wrong output: loses capturing groups from findall tuples",
                evidence_summary="data loss",
                recommendation="fix indexing",
                reflection_specialties=["logic"],
                suspected_category="logic",
            )
        ],
        "metadata": {
            "verifier_hints": {
                "c1": {
                    "verdict": "refuted",
                    "verification_scope": "concrete_behavior",
                    "harness_error": False,
                }
            }
        },
    }
    rows, warnings = _apply_verifier_policy_to_revisions(
        [{"candidate_id": "c1", "verdict": "accept", "updated_evidence_summary": "still bad"}],
        state,
    )
    assert rows[0]["verdict"] == "accept"
    assert any("critique_revision_verifier_inconclusive_wrong_output" in w for w in warnings)
