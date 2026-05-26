"""Tests for misroute rationale parsing and cleanup salvage."""

from __future__ import annotations

from src.domain.schemas import CandidateFinding, ReflectionReport
from src.orchestration.nodes.application.cleanup import make_adversarial_cleanup_node
from src.orchestration.routing.misroute_recovery import parse_misroute_redirect_category
from src.orchestration.routing.normalize_critiquer_candidates import normalize_critiquer_candidates
from src.domain.schemas import ReviewTask


def test_parse_misroute_redirect_security() -> None:
    assert parse_misroute_redirect_category("This belongs in security, not performance.") == "security"


def test_parse_misroute_redirect_none() -> None:
    assert parse_misroute_redirect_category("This is not a security issue.") is None


def test_normalize_forces_security_risk_specialty() -> None:
    task = ReviewTask(id="t1", title="t", description="d", target_files=["src/x.py"])
    raw = CandidateFinding(
        candidate_id="c1",
        patch_task_id="t1",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="user regex",
        claim_type="security_risk",
        failure_mode="ReDoS",
        evidence_summary="attacker-controlled pattern",
        recommendation="limit",
        reflection_specialties=["performance"],
        suspected_category="performance",
    )
    out, _, _ = normalize_critiquer_candidates(task, [raw])
    assert out[0].reflection_specialties == ["security"]


def test_normalize_retags_findall_m0_as_logic_defect() -> None:
    task = ReviewTask(id="t1", title="t", description="d", target_files=["src/x.py"])
    raw = CandidateFinding(
        candidate_id="c1",
        patch_task_id="t1",
        file_path="nodes_string.py",
        line_start=10,
        line_end=12,
        content="re.findall + m[0]",
        claim_type="performance_regression",
        failure_mode="uses findall and m[0]",
        evidence_summary="drops extra groups",
        recommendation="use finditer",
        reflection_specialties=["performance"],
        suspected_category="performance",
    )
    out, _, _ = normalize_critiquer_candidates(task, [raw])
    assert out[0].claim_type == "defect"
    assert out[0].reflection_specialties == ["logic"]


_STRING_COMPARE_BODY = """
class StringCompare():
    def execute(self, string_a, string_b, mode, case_sensitive, **kwargs):
        if mode == 'Equal':
            return string_a == string_b,
        elif mode == 'Starts With':
            return string_a.startswith(string_b),
        elif mode == 'Ends With':
            return string_a.endswith(string_b),
"""


def test_normalize_drops_missing_else_on_structured_extraction_task() -> None:
    task = ReviewTask(
        id="review-logic-structured-extraction",
        title="Structured extraction and aggregation",
        description="Audit tuple slots and join paths. Do not review any other class in the target file.",
        target_files=["comfy_extras/nodes_string.py"],
    )
    raw = CandidateFinding(
        candidate_id="c1",
        patch_task_id="review-logic-structured-extraction",
        file_path="comfy_extras/nodes_string.py",
        line_start=159,
        line_end=189,
        content="class StringCompare():",
        claim_type="defect",
        failure_mode="missing else after elif chain",
        evidence_summary="no terminal else on mode dispatch",
        recommendation="add terminal else",
        reflection_specialties=["logic"],
        suspected_category="logic",
    )
    out, warnings, _ = normalize_critiquer_candidates(task, [raw])
    assert out == []
    assert any("structured_task_scope_drop" in w for w in warnings)


def test_normalize_repairs_endswith_return_slip_to_missing_else() -> None:
    task = ReviewTask(
        id="general-diff-local-1-1",
        title="StringCompare",
        description="branch exhaustiveness",
        target_files=["comfy_extras/nodes_string.py"],
    )
    raw = CandidateFinding(
        candidate_id="c1",
        patch_task_id="general-diff-local-1-1",
        file_path="comfy_extras/nodes_string.py",
        line_start=2,
        line_end=10,
        content="class StringCompare():",
        claim_type="defect",
        failure_mode="Missing return statement in 'Ends With' branch",
        evidence_summary="Ends With branch has no return",
        recommendation='Add the missing return statement: `return a.endswith(b),` after the elif.',
        reflection_specialties=["logic"],
        suspected_category="logic",
    )
    out, _, _ = normalize_critiquer_candidates(
        task,
        [raw],
        file_contents={"comfy_extras/nodes_string.py": _STRING_COMPARE_BODY},
    )
    assert "terminal else" in out[0].failure_mode.lower()
    assert "duplicate returns" in (out[0].recommendation or "").lower()


def test_normalize_does_not_strengthen_redos_with_hedge_wording() -> None:
    task = ReviewTask(id="general_1", title="quality", description="d", target_files=["pkg/h.py"])
    raw = CandidateFinding(
        candidate_id="c1",
        patch_task_id="general_1",
        file_path="pkg/h.py",
        line_start=10,
        line_end=40,
        content="def execute(self, pattern, **kwargs):",
        claim_type="security_risk",
        failure_mode="ReDoS: catastrophic backtracking on user-controlled pattern",
        evidence_summary="group_index and matches[0] mentioned in passing",
        recommendation="Consider adding pattern length limits and timeout mechanism.",
        reflection_specialties=["security"],
        suspected_category="security",
        severity="high",
    )
    out, _, _ = normalize_critiquer_candidates(task, [raw])
    assert "data loss" not in out[0].failure_mode.lower()
    assert "retain all required slots" not in (out[0].recommendation or "").lower()


def test_normalize_strengthens_hedged_findall_tuple_candidate() -> None:
    task = ReviewTask(
        id="logic-structured-extraction-005",
        title="structured extraction",
        description="RegexExtract findall tuples",
        target_files=["comfy_extras/nodes_string.py"],
    )
    raw = CandidateFinding(
        candidate_id="c1",
        patch_task_id="logic-structured-extraction-005",
        file_path="comfy_extras/nodes_string.py",
        line_start=155,
        line_end=157,
        content="elif mode == 'All Matches':",
        claim_type="defect",
        failure_mode="matches[0] tuple handling",
        evidence_summary="findall may return tuples; code uses matches[0]",
        recommendation=(
            "The current logic appears correct. Consider adding explicit handling "
            "when matches[0] is a tuple."
        ),
        reflection_specialties=["logic"],
        suspected_category="logic",
        severity="medium",
    )
    out, _, _ = normalize_critiquer_candidates(task, [raw])
    assert out[0].severity == "high"
    assert "data loss" in out[0].failure_mode.lower()
    assert "appears correct" not in (out[0].recommendation or "").lower()


def test_cleanup_misroute_recovered_when_redirect_parsed() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="perf-redos-1",
        patch_task_id="perf",
        file_path="nodes_string.py",
        line_start=1,
        line_end=5,
        content="ReDoS on user-controlled regex",
        claim_type="security_risk",
        failure_mode="catastrophic backtracking",
        evidence_summary="re.search without timeout on user pattern",
        recommendation="add timeout",
        reflection_specialties=["performance"],
        suspected_category="performance",
    )
    report = ReflectionReport(
        candidate_id=cand.candidate_id,
        reflector_specialty="performance",
        verdict="not_applicable",
        rationale="This belongs in security, not performance.",
    )
    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [report],
            "metadata": {},
        }
    )
    assert len(out["findings"]) == 1
    life = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]
    assert life["reason"] == "misroute_recovered_from_not_applicable"
    assert life["final_category"] == "security"


def test_cleanup_revision_accept_overrides_required_context() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="sec-needs-ctx",
        patch_task_id="t",
        file_path="src/api.py",
        line_start=1,
        line_end=10,
        content="Authorization not visible in handler.",
        claim_type="security_risk",
        failure_mode="Potential IDOR.",
        evidence_summary="No auth check in diff.",
        suspected_category="security",
        reflection_specialties=["security"],
        recommendation="Verify ownership.",
        required_context=["Find middleware auth."],
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="security",
            verdict="needs_verification",
            rationale="Needs runtime proof.",
        )
    ]
    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "focused_context_results": {},
            "metadata": {
                "critique_revision": {
                    "revisions": [
                        {
                            "candidate_id": cand.candidate_id,
                            "verdict": "accept",
                            "updated_evidence_summary": "Static diff shows missing check.",
                        }
                    ]
                },
                "verifier_hints": {
                    cand.candidate_id: {"harness_error": True, "verdict": "inconclusive"},
                },
            },
        }
    )
    assert len(out["findings"]) == 1
    life = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]
    assert life["context_requirement_overridden"] == "critique_revision_accept"
