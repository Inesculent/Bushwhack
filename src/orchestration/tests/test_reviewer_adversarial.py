"""Tests for adversarial reviewer graph, context caps, and cleanup."""

from __future__ import annotations

import pytest

from src.config import get_settings
from src.domain.schemas import (
    CandidateFinding,
    FocusedContextRequest,
    FocusedContextResult,
    ReflectionReport,
    ReviewFinding,
    ReviewTask,
)
from src.domain.state import merge_graph_metadata
from src.orchestration.context.review_context import BoundedReviewContextFulfiller
from src.orchestration.nodes.application.cleanup import make_adversarial_cleanup_node
from src.orchestration.nodes.application.critiquer import _auto_focus_requests
from src.orchestration.nodes.application.reflection import make_adversarial_reflection_node
from src.orchestration.reviewer_graph import build_graph


def test_merge_graph_metadata_deep_merges_parallel_critiquer_shapes() -> None:
    a = {"general_critiquer": {"by_task": {"t1": {"summary": "s1"}}}}
    b = {"general_critiquer": {"by_task": {"t2": {"summary": "s2"}}}}
    merged = merge_graph_metadata(a, b)
    assert merged["general_critiquer"]["by_task"]["t1"]["summary"] == "s1"
    assert merged["general_critiquer"]["by_task"]["t2"]["summary"] == "s2"


def test_reviewer_graph_compiles_adversarial_path():
    graph = build_graph()
    assert graph is not None


def test_reviewer_graph_compiles_legacy_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_REVIEWER_USE_LEGACY_SPECIALIST_WORKERS", "true")
    get_settings.cache_clear()
    try:
        graph = build_graph()
        assert graph is not None
    finally:
        monkeypatch.delenv("REVIEW_REVIEWER_USE_LEGACY_SPECIALIST_WORKERS", raising=False)
        get_settings.cache_clear()


def test_bounded_fulfiller_respects_file_cap() -> None:
    calls: dict[str, int] = {"reads": 0}

    class StubProvider:
        def _ensure_started(self, state: dict) -> None:
            return None

        def read_file_slice(self, file_path: str, *, max_chars: int = 20000) -> str:
            calls["reads"] += 1
            return f"body-{file_path}"

        def search_bounded(self, query: str, *, max_hits: int):
            return []

        def ast_entities_for_file(self, file_path: str):
            return [], []

    fulfiller = BoundedReviewContextFulfiller(StubProvider())  # type: ignore[arg-type]
    req = FocusedContextRequest(
        request_id="r1",
        candidate_id="c1",
        requested_by_specialty="security",
        file_paths=[f"f{i}.py" for i in range(20)],
        symbol_queries=[],
        text_queries=[],
    )
    state: dict = {"run_id": "t", "metadata": {}}
    result = fulfiller.fulfill(state, req)  # type: ignore[arg-type]
    assert len(result.file_snippets) <= 5
    assert calls["reads"] <= 5


def test_adversarial_cleanup_promotes_on_unanimous_accept() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="t1:c1",
        patch_task_id="t1",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="Issue",
        claim_type="defect",
        failure_mode="The changed branch returns the wrong value.",
        evidence_summary="The diff shows the branch returning False where True is expected.",
        suspected_category="logic",
        recommendation="Return the expected value for this branch.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty=spec,
            verdict="accept",
            rationale="ok",
        )
        for spec in ("security", "logic", "performance", "general")
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
    assert isinstance(out["findings"][0], ReviewFinding)


def test_adversarial_cleanup_drops_on_reject() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="t1:c1",
        patch_task_id="t1",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="Issue",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="security",
            verdict="reject",
            rationale="no",
        )
    ]
    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "metadata": {},
        }
    )
    assert out["findings"] == []


def test_adversarial_cleanup_ignores_off_domain_reject() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="review-security-1",
        patch_task_id="review-security",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="Regex can catastrophically backtrack on attacker input.",
        claim_type="security_risk",
        failure_mode="Attacker-controlled input can trigger catastrophic regex backtracking.",
        evidence_summary="The changed code applies an unbounded regex to user-provided input.",
        suspected_category="security",
        reflection_specialties=["security"],
        recommendation="Validate or bound the regex before executing it.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="security",
            verdict="accept",
            rationale="Attacker-controlled regex input can cause ReDoS.",
        ),
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="performance",
            verdict="reject",
            rationale="This is a security concern, not a performance concern.",
        ),
    ]

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "focused_context_results": {
                "ctx": FocusedContextResult(
                    request_id="ctx",
                    candidate_id=cand.candidate_id,
                    file_snippets={"src/x.py": "regex = user_input"},
                )
            },
            "metadata": {},
        }
    )

    assert len(out["findings"]) == 1
    assert out["metadata"]["adversarial_cleanup"]["ignored_off_domain_rejections"] == {
        cand.candidate_id: ["performance"]
    }


def test_adversarial_cleanup_drops_when_routed_expert_says_not_applicable() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="review-security-1",
        patch_task_id="review-security",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="Candidate was routed as security but is actually unrelated.",
        claim_type="defect",
        failure_mode="The issue was routed incorrectly.",
        evidence_summary="The candidate has no security evidence.",
        suspected_category="security",
        reflection_specialties=["security"],
        recommendation="Route this finding to the correct domain.",
    )
    report = ReflectionReport(
        candidate_id=cand.candidate_id,
        reflector_specialty="security",
        verdict="not_applicable",
        rationale="This is not a security issue.",
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [report],
            "metadata": {},
        }
    )

    assert out["findings"] == []
    assert out["metadata"]["adversarial_cleanup"]["misrouted_candidate_ids"][cand.candidate_id][0][
        "reflector_specialty"
    ] == "security"


def test_adversarial_cleanup_drops_positive_observation() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="review-performance-1",
        patch_task_id="review-performance",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="The new batch delete is faster.",
        claim_type="positive_observation",
        failure_mode="No failure mode.",
        evidence_summary="The diff uses one batch operation.",
        suspected_category="performance",
        reflection_specialties=["performance"],
        recommendation="No action needed.",
    )
    report = ReflectionReport(
        candidate_id=cand.candidate_id,
        reflector_specialty="performance",
        verdict="accept",
        rationale="This is more efficient.",
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [report],
            "metadata": {},
        }
    )

    assert out["findings"] == []
    assert out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id][
        "reason"
    ] == "non_promotable_claim_type"


def test_auto_focus_request_created_for_security_claim_needing_context() -> None:
    task = ReviewTask(
        id="review-security",
        title="Security",
        description="Review auth.",
        target_files=["src/x.py", "src/caller.py"],
        specialty="security",
    )
    cand = CandidateFinding(
        candidate_id="review-security:c1",
        patch_task_id="review-security",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="delete_by_ids may delete unauthorized records.",
        claim_type="security_risk",
        failure_mode="A caller can pass IDs they are not authorized to delete.",
        evidence_summary="",
        required_context=["Find callers and authorization checks."],
        suspected_category="security",
        reflection_specialties=["security"],
        recommendation="Verify ownership before deletion.",
    )

    requests = _auto_focus_requests(task, [cand])

    assert len(requests) == 1
    assert requests[0].candidate_id == cand.candidate_id
    assert "src/x.py" in requests[0].file_paths


def test_reflection_routes_candidates_only_to_declared_domains() -> None:
    node = make_adversarial_reflection_node(use_llm=False)
    security_candidate = CandidateFinding(
        candidate_id="review-security-1",
        patch_task_id="review-security",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="Security issue",
        suspected_category="security",
        reflection_specialties=["security"],
    )
    general_candidate = CandidateFinding(
        candidate_id="review-general-1",
        patch_task_id="review-general",
        file_path="src/y.py",
        line_start=1,
        line_end=2,
        content="Missing tests",
        suspected_category="general",
        reflection_specialties=["general"],
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [security_candidate, general_candidate],
            "metadata": {},
        }
    )

    assert out["metadata"]["adversarial_reflection"]["routed_candidate_counts"] == {
        "security": 1,
        "logic": 0,
        "performance": 0,
        "general": 1,
    }
    assert out["metadata"]["adversarial_reflection"]["total_routed_candidate_reviews"] == 2
