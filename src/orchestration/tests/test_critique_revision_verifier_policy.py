"""Tests for critique-revision verifier policy and generic dedupe."""

from __future__ import annotations

from src.domain.schemas import CandidateFinding
from src.domain.state import GraphState
from src.domain.verifier_schemas import VerifierAttemptRecord, VerifierReport
from src.orchestration.nodes.application.critique_revision import (
    _apply_verifier_policy_to_revisions,
    _dedupe_revision_candidate_ids,
    _dedupe_revision_candidate_ids_with_duplicates,
    _render_verifier_advisory_section,
)


def _cand(
    cid: str,
    *,
    claim_type: str = "defect",
    content: str = "Handler drops one row",
    failure_mode: str = "Data loss in changed aggregation",
    evidence_summary: str = "The changed loop skips one row.",
    recommendation: str = "Preserve every row.",
    behavioral_symptom: str = "data_loss",
    root_operation: str = "aggregation",
    reflection_specialties: list[str] | None = None,
    suspected_category: str = "logic",
) -> CandidateFinding:
    return CandidateFinding(
        candidate_id=cid,
        patch_task_id="1",
        file_path="comfy_extras/nodes_string.py",
        line_start=1,
        line_end=2,
        content=content,
        claim_type=claim_type,  # type: ignore[arg-type]
        failure_mode=failure_mode,
        evidence_summary=evidence_summary,
        recommendation=recommendation,
        reflection_specialties=reflection_specialties or ["logic"],
        suspected_category=suspected_category,
        behavioral_symptom=behavioral_symptom,
        root_operation=root_operation,
    )


def test_dedupe_revision_candidates_by_generic_claim_signature() -> None:
    state: GraphState = {
        "candidate_findings": [
            _cand("1:task1_1"),
            _cand("1:task1_2"),
        ],
    }
    out = _dedupe_revision_candidate_ids(state, ["1:task1_1", "1:task1_2"])
    assert out == ["1:task1_1"]
    _, duplicates = _dedupe_revision_candidate_ids_with_duplicates(
        state,
        ["1:task1_1", "1:task1_2"],
    )
    assert duplicates == {"1:task1_1": ["1:task1_2"]}


def test_dedupe_revision_preserves_distinct_same_file_claims() -> None:
    state: GraphState = {
        "candidate_findings": [
            _cand("data-loss"),
            _cand(
                "missing-return",
                content="Handler can fall through without returning.",
                failure_mode="Implicit None from changed dispatch path.",
                evidence_summary="No fallback branch returns a value.",
                recommendation="Return a valid value on every path.",
                behavioral_symptom="missing_return",
                root_operation="dispatch",
            ),
        ],
    }
    out = _dedupe_revision_candidate_ids(state, ["data-loss", "missing-return"])
    assert out == ["data-loss", "missing-return"]


def test_dedupe_revision_preserves_distinct_security_same_file_claims() -> None:
    state: GraphState = {
        "candidate_findings": [
            _cand(
                "path-risk",
                claim_type="security_risk",
                content="Handler joins an untrusted relative path.",
                failure_mode="Path traversal can escape the allowed directory.",
                evidence_summary="The changed path join accepts '../'.",
                recommendation="Normalize and validate the resolved path.",
                behavioral_symptom="contract_mismatch",
                root_operation="resource_use",
                reflection_specialties=["security"],
                suspected_category="security",
            ),
            _cand(
                "token-risk",
                claim_type="security_risk",
                content="Handler logs a sensitive token on failure.",
                failure_mode="Sensitive value exposure in diagnostics.",
                evidence_summary="The changed error branch formats the token into logs.",
                recommendation="Redact the token before logging.",
                behavioral_symptom="contract_mismatch",
                root_operation="serialization",
                reflection_specialties=["security"],
                suspected_category="security",
            ),
        ],
    }
    out = _dedupe_revision_candidate_ids(state, ["path-risk", "token-risk"])
    assert out == ["path-risk", "token-risk"]


def test_apply_verifier_policy_refuted_stays_advisory() -> None:
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
    assert rows[0]["verdict"] == "accept"
    assert "Runtime verifier reported refuted" in rows[0]["updated_evidence_summary"]
    assert any("critique_revision_verifier_refuted_advisory" in w for w in warnings)


def test_apply_verifier_policy_verified_stays_advisory() -> None:
    state: GraphState = {
        "candidate_findings": [
            CandidateFinding(
                candidate_id="c1",
                patch_task_id="1",
                file_path="m.py",
                line_start=1,
                line_end=2,
                content="branch is incomplete",
                claim_type="defect",
                failure_mode="SyntaxError in changed source",
                evidence_summary="source-only proof",
                recommendation="complete the branch",
                reflection_specialties=["logic"],
                suspected_category="logic",
            )
        ],
        "metadata": {
            "verifier_hints": {
                "c1": {
                    "verdict": "verified",
                    "verification_scope": "concrete_behavior",
                    "harness_error": False,
                    "product_verified": True,
                    "confidence": "clean_product_signal",
                    "updated_evidence_summary": "Runtime verifier: verified syntax error",
                }
            }
        },
    }
    rows, warnings = _apply_verifier_policy_to_revisions(
        [{"candidate_id": "c1", "verdict": "reject", "updated_evidence_summary": "needs code"}],
        state,
    )
    assert rows[0]["verdict"] == "reject"
    assert "verified syntax error" in rows[0]["updated_evidence_summary"]
    assert any("critique_revision_verifier_verified_advisory:c1" in w for w in warnings)


def test_apply_verifier_policy_harness_is_ignored() -> None:
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
    assert rows[0]["verdict"] == "accept"
    assert rows[0]["updated_evidence_summary"] == ""


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
    assert rows[0]["updated_evidence_summary"].startswith("still bad")
    assert "advisory evidence" in rows[0]["updated_evidence_summary"]
    assert any("critique_revision_verifier_refuted_advisory" in w for w in warnings)


def test_render_verifier_advisory_section_only_includes_clean_product_signal() -> None:
    clean = VerifierReport(
        run_id="r1",
        candidate_id="clean",
        verdict="verified",
        verification_scope="concrete_behavior",
        attempts=[
            VerifierAttemptRecord(
                attempt_number=1,
                stdout="STATUS: MISMATCH | expected=a actual=b",
                exit_code=1,
            )
        ],
    )
    harness = VerifierReport(
        run_id="r1",
        candidate_id="harness",
        verdict="verified",
        verification_scope="concrete_behavior",
        attempts=[
            VerifierAttemptRecord(
                attempt_number=1,
                stdout="STATUS: HARNESS_ERROR | ImportError",
                exit_code=2,
            )
        ],
    )
    state: GraphState = {
        "verifier_reports": [clean, harness],
        "metadata": {
            "verifier_hints": {
                "clean": {"confidence": "clean_product_signal"},
                "harness": {"confidence": "harness_only"},
            }
        },
    }

    rendered = _render_verifier_advisory_section(state, ["clean", "harness"])

    assert "clean" in rendered
    assert "MISMATCH" in rendered
    assert "harness" not in rendered
    assert "HARNESS_ERROR" not in rendered
