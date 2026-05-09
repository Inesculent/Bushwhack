"""Tests for verifier routing, judgment, and critique revision wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.domain.schemas import (
    CandidateFinding,
    FocusedContextResult,
    ReflectionReport,
    SearchResult,
)
from src.domain.state import GraphState
from src.domain.verifier_schemas import VerifierAttemptRecord, VerifierReport
from src.orchestration.nodes.application.critique_revision import _render_verifier_advisory_section
from src.orchestration.nodes.verifier.result_judge import infer_verification_scope, judge_attempt
from src.orchestration.routing.verifier_fanout import collect_verifier_send_payloads, focused_context_text_for_candidate


def _minimal_state(**kwargs: object) -> GraphState:
    base: GraphState = {
        "run_id": "r1",
        "repo_path": "/tmp/repo",
        "git_diff": "diff --git a/x b/x",
        "candidate_findings": [],
        "reflection_reports": [],
        "focused_context_results": {},
        "metadata": {},
    }
    base.update(kwargs)  # type: ignore[arg-type]
    return base


def test_collect_verifier_send_payloads_disabled() -> None:
    state = _minimal_state()
    with patch("src.orchestration.routing.verifier_fanout.get_settings") as gs:
        m = MagicMock()
        m.verifier_enabled = False
        gs.return_value = m
        assert collect_verifier_send_payloads(state) == []


def test_collect_verifier_send_payloads_eligible() -> None:
    cid = "cand-1"
    cand = CandidateFinding(
        candidate_id=cid,
        patch_task_id="t1",
        file_path="pkg/mod.py",
        line_start=1,
        line_end=2,
        content="bug",
        claim_type="defect",
        failure_mode="raises on empty input",
    )
    rep = ReflectionReport(
        candidate_id=cid,
        reflector_specialty="logic",
        verdict="needs_context",
        focused_request=None,
    )
    fc = FocusedContextResult(
        request_id="req1",
        candidate_id=cid,
        file_snippets={"pkg/mod.py": "x = 1"},
        search_hits={},
    )
    state = _minimal_state(
        candidate_findings=[cand],
        reflection_reports=[rep],
        focused_context_results={"req1": fc},
    )
    with patch("src.orchestration.routing.verifier_fanout.get_settings") as gs:
        m = MagicMock()
        m.verifier_enabled = True
        m.verifier_skip_if_no_docker = True
        m.verifier_run_on_defect = True
        m.verifier_run_on_security = False
        m.verifier_run_on_performance = False
        m.verifier_total_budget_per_pr = 10
        m.verifier_require_focused_evidence = True
        gs.return_value = m
        with patch("src.orchestration.routing.verifier_fanout._docker_available", return_value=True):
            sends = collect_verifier_send_payloads(state)
    assert len(sends) == 1
    assert sends[0].node == "verifier_subgraph"
    assert sends[0].arg["verifier_candidate"]["candidate_id"] == cid


def test_has_focused_evidence_accepts_full_file_payload() -> None:
    from src.orchestration.nodes.application.critique_revision import _has_focused_evidence

    cid = "c-full"
    fc = FocusedContextResult(
        request_id="r1",
        candidate_id=cid,
        file_contents_full={"nodes_string.py": "def f(): ..."},
        file_snippets={},
        search_hits={},
    )
    state = _minimal_state(focused_context_results={"r1": fc})
    assert _has_focused_evidence(state, [cid]) is True


def test_focused_context_text_for_candidate() -> None:
    cid = "c2"
    fc = FocusedContextResult(
        request_id="r1",
        candidate_id=cid,
        file_snippets={"a.py": "1"},
        search_hits={"q": [SearchResult(file_path="a.py", line_number=1, content="hit", context_lines=[])]},
    )
    state = _minimal_state(focused_context_results={"r1": fc})
    text = focused_context_text_for_candidate(state, cid)
    assert "a.py" in text


def test_judge_attempt_verified() -> None:
    rec = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=1,
        stdout="STATUS: CRASHED | ValueError: bad",
        stderr="",
    )
    v, _ = judge_attempt(rec)
    assert v == "verified"


def test_judge_attempt_refuted() -> None:
    rec = VerifierAttemptRecord(attempt_number=1, test_code="x", exit_code=0, stdout="", stderr="")
    v, _ = judge_attempt(rec)
    assert v == "refuted"


def test_infer_scope_abstract() -> None:
    d = {"failure_mode": "x", "content": "uses network calls"}
    assert infer_verification_scope(d) == "abstract_or_unverifiable"


def test_collect_verifier_send_payloads_no_focused_when_required() -> None:
    cid = "cand-1"
    cand = CandidateFinding(
        candidate_id=cid,
        patch_task_id="t1",
        file_path="pkg/mod.py",
        line_start=1,
        line_end=2,
        content="bug",
        claim_type="defect",
        failure_mode="x",
    )
    rep = ReflectionReport(
        candidate_id=cid,
        reflector_specialty="logic",
        verdict="needs_context",
        focused_request=None,
    )
    state = _minimal_state(
        candidate_findings=[cand],
        reflection_reports=[rep],
        focused_context_results={},
    )
    with patch("src.orchestration.routing.verifier_fanout.get_settings") as gs:
        m = MagicMock()
        m.verifier_enabled = True
        m.verifier_skip_if_no_docker = True
        m.verifier_run_on_defect = True
        m.verifier_run_on_security = False
        m.verifier_run_on_performance = False
        m.verifier_total_budget_per_pr = 10
        m.verifier_require_focused_evidence = True
        gs.return_value = m
        with patch("src.orchestration.routing.verifier_fanout._docker_available", return_value=True):
            assert collect_verifier_send_payloads(state) == []


def test_collect_verifier_send_payloads_without_focused_when_relaxed() -> None:
    cid = "cand-1"
    cand = CandidateFinding(
        candidate_id=cid,
        patch_task_id="t1",
        file_path="pkg/mod.py",
        line_start=1,
        line_end=2,
        content="bug",
        claim_type="defect",
        failure_mode="x",
    )
    rep = ReflectionReport(
        candidate_id=cid,
        reflector_specialty="logic",
        verdict="needs_context",
        focused_request=None,
    )
    state = _minimal_state(
        candidate_findings=[cand],
        reflection_reports=[rep],
        focused_context_results={},
    )
    with patch("src.orchestration.routing.verifier_fanout.get_settings") as gs:
        m = MagicMock()
        m.verifier_enabled = True
        m.verifier_skip_if_no_docker = True
        m.verifier_run_on_defect = True
        m.verifier_run_on_security = False
        m.verifier_run_on_performance = False
        m.verifier_total_budget_per_pr = 10
        m.verifier_require_focused_evidence = False
        gs.return_value = m
        with patch("src.orchestration.routing.verifier_fanout._docker_available", return_value=True):
            sends = collect_verifier_send_payloads(state)
    assert len(sends) == 1
    assert sends[0].node == "verifier_subgraph"


def test_route_focused_after_reflection_needs_context_without_focus_request() -> None:
    from src.orchestration.routing.adversarial_after_reflection import route_focused_after_reflection

    rep = ReflectionReport(
        candidate_id="p1",
        reflector_specialty="performance",
        verdict="needs_context",
        focused_request=None,
    )
    state = _minimal_state(reflection_reports=[rep])
    assert route_focused_after_reflection(state) == "post_reflection_evidence_pass"


def test_route_focused_after_reflection_with_embedded_focus_request() -> None:
    from src.domain.schemas import FocusedContextRequest
    from src.orchestration.routing.adversarial_after_reflection import route_focused_after_reflection

    req = FocusedContextRequest(
        request_id="r1",
        candidate_id="p1",
        requested_by_specialty="performance",
        file_paths=["a.py"],
    )
    rep = ReflectionReport(
        candidate_id="p1",
        reflector_specialty="performance",
        verdict="needs_context",
        focused_request=req,
    )
    state = _minimal_state(reflection_reports=[rep])
    assert route_focused_after_reflection(state) == "focused_context"


def test_route_focused_after_reflection_cleanup_when_no_needs_revision() -> None:
    from src.orchestration.routing.adversarial_after_reflection import route_focused_after_reflection

    rep = ReflectionReport(
        candidate_id="p1",
        reflector_specialty="performance",
        verdict="accept",
        focused_request=None,
    )
    state = _minimal_state(reflection_reports=[rep])
    assert route_focused_after_reflection(state) == "adversarial_cleanup"


def test_route_focused_after_reflection_needs_verification_before_focused_context() -> None:
    """Runtime verification path must win over embedded focused_context when both appear."""
    from src.domain.schemas import FocusedContextRequest
    from src.orchestration.routing.adversarial_after_reflection import route_focused_after_reflection

    req = FocusedContextRequest(
        request_id="r1",
        candidate_id="p1",
        requested_by_specialty="logic",
        file_paths=["a.py"],
    )
    reports = [
        ReflectionReport(
            candidate_id="p1",
            reflector_specialty="logic",
            verdict="needs_verification",
            focused_request=None,
            rationale="Need repro",
        ),
        ReflectionReport(
            candidate_id="p1",
            reflector_specialty="performance",
            verdict="needs_context",
            focused_request=req,
            rationale="Want static grep",
        ),
    ]
    state = _minimal_state(reflection_reports=reports)
    assert route_focused_after_reflection(state) == "post_reflection_evidence_pass"


def test_collect_verifier_send_payloads_needs_verification_bypasses_focused_requirement() -> None:
    """needs_verification + defect should still fan out when focused evidence is required but absent."""
    cid = "cand-nv"
    cand = CandidateFinding(
        candidate_id=cid,
        patch_task_id="t1",
        file_path="pkg/mod.py",
        line_start=1,
        line_end=2,
        content="bug",
        claim_type="defect",
        failure_mode="crashes on None input",
        evidence_summary="diff",
        recommendation="guard None",
    )
    rep = ReflectionReport(
        candidate_id=cid,
        reflector_specialty="logic",
        verdict="needs_verification",
        focused_request=None,
    )
    state = _minimal_state(
        candidate_findings=[cand],
        reflection_reports=[rep],
        focused_context_results={},
    )
    with patch("src.orchestration.routing.verifier_fanout.get_settings") as gs:
        m = MagicMock()
        m.verifier_enabled = True
        m.verifier_skip_if_no_docker = True
        m.verifier_run_on_defect = True
        m.verifier_run_on_security = False
        m.verifier_run_on_performance = False
        m.verifier_total_budget_per_pr = 10
        m.verifier_require_focused_evidence = True
        gs.return_value = m
        with patch("src.orchestration.routing.verifier_fanout._docker_available", return_value=True):
            sends = collect_verifier_send_payloads(state)
    assert len(sends) == 1
    assert sends[0].node == "verifier_subgraph"


def test_plan_critique_revision_shards_verifier_only_when_no_focused() -> None:
    from src.config import get_settings
    from src.orchestration.nodes.application.critique_revision import plan_critique_revision_shards

    cid = "c1"
    cand = CandidateFinding(
        candidate_id=cid,
        patch_task_id="t1",
        file_path="m.py",
        line_start=1,
        line_end=2,
        content="x",
        claim_type="defect",
        failure_mode="crash",
    )
    rep = ReflectionReport(
        candidate_id=cid,
        reflector_specialty="logic",
        verdict="needs_verification",
    )
    vr = VerifierReport(
        run_id="r1",
        candidate_id=cid,
        verdict="verified",
        final_rationale="ok",
        updated_evidence_summary="Runtime verifier: verified",
    )
    state = _minimal_state(
        candidate_findings=[cand],
        reflection_reports=[rep],
        verifier_reports=[vr],
    )
    settings = get_settings()
    shards = plan_critique_revision_shards(
        state,
        [cid],
        max_shard_chars=settings.reviewer_critique_revision_max_shard_chars,
        max_candidate_chars=settings.reviewer_critique_revision_max_candidate_chars,
    )
    assert len(shards) == 1
    assert shards[0].shard_id == f"{cid}:verifier_only"
    assert shards[0].focused_results == []


def test_revision_inputs_ready_accepts_verifier_without_focused() -> None:
    from src.orchestration.nodes.application.critique_revision import revision_inputs_ready

    cid = "c1"
    cand = CandidateFinding(
        candidate_id=cid,
        patch_task_id="t1",
        file_path="m.py",
        line_start=1,
        line_end=2,
        content="x",
        claim_type="defect",
        failure_mode="crash",
    )
    rep = ReflectionReport(
        candidate_id=cid,
        reflector_specialty="logic",
        verdict="needs_verification",
    )
    vr = VerifierReport(
        run_id="r1",
        candidate_id=cid,
        verdict="inconclusive",
        final_rationale="x",
        updated_evidence_summary="y",
    )
    state = _minimal_state(
        candidate_findings=[cand],
        reflection_reports=[rep],
        verifier_reports=[vr],
    )
    assert revision_inputs_ready(state, [cid]) is True


def test_start_verifier_sandbox_clones_when_repo_path_is_remote_url() -> None:
    from src.orchestration.nodes.verifier.sandbox_executor import _start_verifier_sandbox

    class StubSb:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def start(self, p: str) -> None:
            self.calls.append(("start", p))

        def start_from_remote_ref(self, repo_url: str, ref: str) -> None:
            self.calls.append(("remote", repo_url, ref))

    sb = StubSb()
    state = {
        "metadata": {
            "review_repo_url": "https://github.com/o/r",
            "review_pr_number": 99,
        }
    }
    _start_verifier_sandbox(sb, "https://github.com/other/repo", state)  # type: ignore[arg-type]
    assert sb.calls[0] == ("remote", "https://github.com/o/r", "pull/99/head")

    sb2 = StubSb()
    _start_verifier_sandbox(sb2, "https://github.com/o/r", None)  # type: ignore[arg-type]
    assert sb2.calls[0] == ("remote", "https://github.com/o/r", "HEAD")


def test_build_test_generator_prompt_str_format_succeeds() -> None:
    """Regression: markdown must use balanced {{}} placeholders for str.format."""
    from src.orchestration.nodes.verifier.test_generator import build_test_generator_prompt
    from src.orchestration.prompts.renderer import load_reviewer_prompt

    load_reviewer_prompt.cache_clear()
    text = build_test_generator_prompt(
        candidate={"file_path": "pkg/x.py", "line_start": 1, "line_end": 3, "failure_mode": "x"},
        focused_context_snippets="ctx",
        git_diff_excerpt="diff",
        retry_feedback="",
        mock_heavy_deps=True,
        timeout_seconds=60,
    )
    assert "pkg/x.py" in text
    assert "1" in text and "3" in text
