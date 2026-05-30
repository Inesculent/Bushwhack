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
from src.orchestration.nodes.verifier.result_judge import (
    attempt_was_harness_error,
    build_retry_feedback,
    classify_attempt_failure,
    infer_verification_scope,
    judge_attempt,
)
from src.orchestration.nodes.verifier.sandbox_executor import (
    _verifier_sandbox_image,
    validate_test_code,
)
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
        m.verifier_skip_if_no_sandbox = True
        m.verifier_run_on_defect = True
        m.verifier_run_on_security = False
        m.verifier_run_on_performance = False
        m.verifier_total_budget_per_pr = 10
        m.verifier_require_focused_evidence = True
        gs.return_value = m
        with patch("src.orchestration.routing.verifier_fanout.sandbox_runtime_available", return_value=True):
            sends = collect_verifier_send_payloads(state)
    assert len(sends) == 1
    assert sends[0].node == "verifier_subgraph"
    assert sends[0].arg["verifier_candidate"]["candidate_id"] == cid
    assert sends[0].arg["token_usage"] == 0


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


def test_judge_attempt_verified_product_crash_in_target_file() -> None:
    rec = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=1,
        stdout="STATUS: CRASHED | ValueError: bad",
        stderr='  File "/repo/pkg/mod.py", line 10, in execute\n    raise ValueError("bad")',
    )
    v, _ = judge_attempt(rec, target_file_path="pkg/mod.py")
    assert v == "verified"


def test_judge_attempt_crashed_without_target_traceback_is_inconclusive() -> None:
    rec = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=1,
        stdout="STATUS: CRASHED | TypeError: missing 4 required positional arguments",
        stderr="",
    )
    v, rationale = judge_attempt(rec, target_file_path="pkg/mod.py")
    assert v == "inconclusive"
    assert "harness" in rationale.lower() or "signature" in rationale.lower()


def test_judge_attempt_io_boolean_attribute_error_is_harness() -> None:
    rec = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=1,
        stdout="",
        stderr=(
            '  File "/repo/comfy_extras/nodes_string.py", line 145, in StringContains\n'
            "    RETURN_TYPES = (IO.BOOLEAN,)\n"
            "AttributeError: 'types.SimpleNamespace' object has no attribute 'BOOLEAN'"
        ),
    )
    v, _ = judge_attempt(rec, target_file_path="comfy_extras/nodes_string.py")
    assert v == "inconclusive"


def test_judge_attempt_mismatch_verified() -> None:
    rec = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=1,
        stdout="STATUS: MISMATCH | expected=foo actual=bar",
        stderr="",
    )
    v, _ = judge_attempt(rec)
    assert v == "verified"


def test_judge_attempt_refuted() -> None:
    rec = VerifierAttemptRecord(attempt_number=1, test_code="x", exit_code=0, stdout="", stderr="")
    v, _ = judge_attempt(rec)
    assert v == "refuted"


def test_judge_attempt_refuted_when_status_safe() -> None:
    rec = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=0,
        stdout="STATUS: SAFE\n",
        stderr="",
    )
    v, _ = judge_attempt(rec)
    assert v == "refuted"


def test_judge_attempt_harness_import_crash_is_inconclusive() -> None:
    rec = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=1,
        stdout="STATUS: CRASHED | ExceptionType: ImportError: No module named 'comfy_extras'\n",
        stderr="",
    )
    v, _ = judge_attempt(rec)
    assert v == "inconclusive"
    assert attempt_was_harness_error(rec)


def test_judge_attempt_pil_mock_submodule_crash_is_harness() -> None:
    rec = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=1,
        stdout=(
            "STATUS: CRASHED | ExceptionType: ModuleNotFoundError: "
            "No module named 'PIL.PngImagePlugin'; 'PIL' is not a package\n"
        ),
        stderr="",
        sandbox_mode="exec_workspace",
    )
    assert attempt_was_harness_error(rec)
    v, _ = judge_attempt(rec)
    assert v == "inconclusive"


def test_judge_attempt_harness_error_exit_code() -> None:
    rec = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=2,
        stdout="STATUS: HARNESS_ERROR | SyntaxError: invalid syntax",
        stderr="",
        sandbox_mode="harness_preflight",
    )
    v, _ = judge_attempt(rec)
    assert v == "inconclusive"


def test_validate_test_code_rejects_syntax_error() -> None:
    assert validate_test_code("def broken(\n") is not None


def test_start_verifier_sandbox_uses_exec_workspace_when_enabled(tmp_path) -> None:
    from unittest.mock import MagicMock

    from src.orchestration.nodes.verifier.sandbox_executor import _start_verifier_sandbox

    repo = tmp_path / "repo"
    repo.mkdir()

    settings = MagicMock()
    settings.verifier_clone_remote_in_container = False
    settings.verifier_require_repo_in_container = False
    settings.verifier_use_execution_workspace = True

    class StubSb:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def start(self, path: str) -> None:
            self.calls.append(f"start:{path}")

        def create_execution_workspace(self, workspace_name: str | None = None) -> str:
            self.calls.append(f"exec:{workspace_name}")
            return "/verify_exec"

    sb = StubSb()
    mode = _start_verifier_sandbox(sb, str(repo), {}, settings=settings)  # type: ignore[arg-type]
    assert mode == "exec_workspace"
    assert sb.calls[0].startswith("start:")
    assert sb.calls[1] == "exec:verify_exec"


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
        m.verifier_skip_if_no_sandbox = True
        m.verifier_run_on_defect = True
        m.verifier_run_on_security = False
        m.verifier_run_on_performance = False
        m.verifier_total_budget_per_pr = 10
        m.verifier_require_focused_evidence = True
        gs.return_value = m
        with patch("src.orchestration.routing.verifier_fanout.sandbox_runtime_available", return_value=True):
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
        m.verifier_skip_if_no_sandbox = True
        m.verifier_run_on_defect = True
        m.verifier_run_on_security = False
        m.verifier_run_on_performance = False
        m.verifier_total_budget_per_pr = 10
        m.verifier_require_focused_evidence = False
        gs.return_value = m
        with patch("src.orchestration.routing.verifier_fanout.sandbox_runtime_available", return_value=True):
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
        m.verifier_skip_if_no_sandbox = True
        m.verifier_run_on_defect = True
        m.verifier_run_on_security = False
        m.verifier_run_on_performance = False
        m.verifier_total_budget_per_pr = 10
        m.verifier_require_focused_evidence = True
        gs.return_value = m
        with patch("src.orchestration.routing.verifier_fanout.sandbox_runtime_available", return_value=True):
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


def test_start_verifier_sandbox_snippet_workspace_when_clone_disabled() -> None:
    from unittest.mock import MagicMock

    from src.orchestration.nodes.verifier.sandbox_executor import _start_verifier_sandbox

    settings = MagicMock()
    settings.verifier_clone_remote_in_container = False
    settings.verifier_require_repo_in_container = False

    class StubSb:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def start_snippet_workspace(self) -> str:
            self.calls.append("snippet")
            return "cid"

    sb = StubSb()
    _start_verifier_sandbox(sb, "https://github.com/other/repo", {}, settings=settings)  # type: ignore[arg-type]
    assert sb.calls == ["snippet"]


def test_verifier_sandbox_image_uses_clone_stack_for_remote_pr() -> None:
    from unittest.mock import MagicMock

    settings = MagicMock()
    settings.verifier_clone_remote_in_container = True
    settings.verifier_image = "verifier-test-env:latest"
    settings.verifier_clone_image = "agent-fs-sandbox"

    state = {"metadata": {"review_repo_url": "https://github.com/o/r", "review_pr_number": 1}}
    img = _verifier_sandbox_image(settings, "https://github.com/o/r", state)
    assert img == "agent-fs-sandbox"


def test_verifier_sandbox_image_uses_verifier_image_for_local_mount(tmp_path) -> None:
    from unittest.mock import MagicMock

    settings = MagicMock()
    settings.verifier_image = "verifier-test-env:latest"
    settings.verifier_clone_image = "agent-fs-sandbox"
    settings.verifier_clone_remote_in_container = True

    img = _verifier_sandbox_image(settings, str(tmp_path), {})
    assert img == "verifier-test-env:latest"


def test_start_verifier_sandbox_raises_when_repo_required_but_no_url() -> None:
    from unittest.mock import MagicMock

    import pytest

    from src.orchestration.nodes.verifier.sandbox_executor import _start_verifier_sandbox

    settings = MagicMock()
    settings.verifier_clone_remote_in_container = True
    settings.verifier_require_repo_in_container = True

    class StubSb:
        def start_snippet_workspace(self) -> str:
            raise AssertionError("should not use snippet")

    with pytest.raises(FileNotFoundError):
        _start_verifier_sandbox(StubSb(), "", {}, settings=settings)  # type: ignore[arg-type]


def test_start_verifier_sandbox_clones_when_clone_flag_enabled() -> None:
    from unittest.mock import MagicMock

    from src.orchestration.nodes.verifier.sandbox_executor import _start_verifier_sandbox

    settings = MagicMock()
    settings.verifier_clone_remote_in_container = True
    settings.verifier_require_repo_in_container = True
    settings.verifier_use_execution_workspace = False

    class StubSb:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def start_from_remote_ref(self, repo_url: str, ref: str) -> None:
            self.calls.append(("remote", repo_url, ref))

    sb = StubSb()
    state = {
        "metadata": {
            "review_repo_url": "https://github.com/o/r",
            "review_pr_number": 99,
        }
    }
    _start_verifier_sandbox(sb, "https://github.com/other/repo", state, settings=settings)  # type: ignore[arg-type]
    assert sb.calls[0] == ("remote", "https://github.com/o/r", "pull/99/head")

    sb2 = StubSb()
    _start_verifier_sandbox(sb2, "https://github.com/o/r", None, settings=settings)  # type: ignore[arg-type]
    assert sb2.calls[0] == ("remote", "https://github.com/o/r", "HEAD")


def test_review_sandbox_default_image_is_agent_fs_stack() -> None:
    """Review context uses RepoSandbox() default image (git+rg stack per docker_mcp/fs-mcp); verifier uses verifier_image."""
    from src.infrastructure.sandbox import RepoSandbox

    from src.config import Settings
    from src.infrastructure.sandbox import build_repo_sandbox

    with patch("src.infrastructure.sandbox_docker.docker.from_env"):
        sandbox = build_repo_sandbox(Settings(sandbox_backend="docker"))
        assert sandbox.image_name == "agent-fs-sandbox"


def test_build_test_generator_prompt_escapes_python_braces() -> None:
    """Regression: markdown code blocks must escape {{}} for str.format (e.g. f\"PIL.{sub}\")."""
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
        repo_root="/repo",
    )
    assert "pkg/x.py" in text
    assert 'f"PIL.{sub}"' in text
    assert "PngImagePlugin" in text
    assert "comfy.comfy_types" not in text
    assert "inspect.signature" in text


def test_build_retry_feedback_includes_error_class_and_prior_summary() -> None:
    prior = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=2,
        stdout="STATUS: HARNESS_ERROR | SyntaxError: invalid",
        stderr="",
    )
    last = VerifierAttemptRecord(
        attempt_number=2,
        test_code="x",
        exit_code=1,
        stdout="STATUS: CRASHED | TypeError: missing 2 required positional arguments",
        stderr="",
    )
    fb = build_retry_feedback(
        last,
        prior_attempts=[prior],
        target_file_path="pkg/mod.py",
    )
    assert "signature_mismatch" in fb
    assert "attempt=1" in fb
    assert "pkg/mod.py" in fb


def test_classify_attempt_failure_signature_mismatch() -> None:
    rec = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=1,
        stdout="STATUS: CRASHED | TypeError: foo() missing 1 required positional argument",
        stderr="",
    )
    assert classify_attempt_failure(rec, target_file_path="pkg/mod.py") == "signature_mismatch"


def test_classify_harness_crashed_not_product_crash() -> None:
    rec = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=1,
        stdout="STATUS: CRASHED | TypeError: missing 4 required positional arguments",
        stderr="",
    )
    assert classify_attempt_failure(rec, target_file_path="pkg/mod.py") == "signature_mismatch"
    assert classify_attempt_failure(rec, target_file_path="pkg/mod.py") != "product_crash"


def test_classify_wrong_status_protocol() -> None:
    rec = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=1,
        stdout="Traceback...\n",
        stderr="",
    )
    assert classify_attempt_failure(rec) == "wrong_status_protocol"


def test_build_retry_feedback_harness_error_action_hint() -> None:
    rec = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=2,
        stdout="STATUS: HARNESS_ERROR | SyntaxError: invalid",
        stderr="",
        sandbox_mode="harness_preflight",
    )
    fb = build_retry_feedback(rec, target_file_path="pkg/mod.py")
    assert "HARNESS_ERROR" in fb
    assert "syntax_error" in fb or "harness_error" in fb


def test_invoke_verifier_runner_metadata_harness_flags() -> None:
    from src.orchestration.nodes.verifier.verifier_runner import invoke_verifier_for_candidate

    harness = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=2,
        stdout="STATUS: HARNESS_ERROR | SyntaxError: invalid",
        stderr="",
        sandbox_mode="harness_preflight",
    )
    with patch("src.orchestration.nodes.verifier.verifier_runner._sandbox_ok", return_value=True), \
         patch("src.orchestration.nodes.verifier.verifier_runner.generate_test_script", return_value=("code", 0)), \
         patch("src.orchestration.nodes.verifier.verifier_runner.execute_test_script", return_value=harness), \
         patch("src.orchestration.nodes.verifier.verifier_runner.get_settings") as gs:
        m = MagicMock()
        m.verifier_enabled = True
        m.verifier_skip_if_no_sandbox = True
        m.verifier_max_attempts = 1
        gs.return_value = m
        report = invoke_verifier_for_candidate(
            run_id="r1",
            repo_path="/tmp/repo",
            candidate={
                "candidate_id": "c1",
                "file_path": "pkg/mod.py",
                "line_start": 1,
                "line_end": 2,
                "failure_mode": "crash",
            },
            focused_context_snippets="",
            git_diff_excerpt="",
            use_llm=False,
        )
    assert report.metadata.get("harness_error") is True
    assert report.metadata.get("product_verified") is False


def test_verifier_advisory_omits_test_code() -> None:
    huge_code = "x = 1\n" * 5000
    vr = VerifierReport(
        run_id="r1",
        candidate_id="c1",
        verdict="inconclusive",
        attempts=[
            VerifierAttemptRecord(
                attempt_number=1,
                test_code=huge_code,
                exit_code=2,
                stdout="STATUS: HARNESS_ERROR",
                stderr="",
            )
        ],
    )
    state = _minimal_state(verifier_reports=[vr])
    blob = _render_verifier_advisory_section(state, ["c1"])
    assert "test_code" not in blob
    assert len(blob) < len(huge_code)
