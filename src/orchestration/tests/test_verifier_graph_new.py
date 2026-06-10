"""Tests for the multi-node verifier subgraph."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from src.domain.state import GraphState
from src.domain.verifier_schemas import VerifierAttemptRecord, VerifierReport
from src.orchestration.nodes.verifier.result_judge import verifier_hint_flags_for_attempts
from src.orchestration.verifier_graph import (
    build_verifier_graph,
    verifier_preflight_node,
    verifier_generate_node,
    verifier_execute_node,
    verifier_judge_node,
    verifier_finalize_node,
    verifier_routing,
)


def _minimal_state(**kwargs) -> GraphState:
    base: GraphState = {
        "run_id": "r1",
        "repo_path": "/tmp/repo",
        "git_diff": "diff --git a/x b/x",
        "verifier_candidate": {
            "candidate_id": "c1",
            "file_path": "a.py",
            "line_start": 1,
            "line_end": 2,
            "failure_mode": "crash",
        },
        "metadata": {},
    }
    base.update(kwargs)
    return base


def test_verifier_preflight_node_enabled() -> None:
    state = _minimal_state()
    with patch("src.orchestration.verifier_graph.get_settings") as gs:
        m = MagicMock()
        m.verifier_enabled = True
        m.verifier_skip_if_no_sandbox = False
        gs.return_value = m
        with patch("src.orchestration.verifier_graph._infer_verifier_repo_root", return_value="/repo"):
            with patch("src.orchestration.routing.verifier_fanout.focused_context_text_for_candidate", return_value="ctx"):
                res = verifier_preflight_node(state)
    
    assert res["verifier_attempt_idx"] == 0
    assert res["verifier_repo_root"] == "/repo"
    assert res["verifier_focused_context_text"] == "ctx"
    assert "verifier_skipped_reason" not in res


def test_verifier_preflight_node_disabled() -> None:
    state = _minimal_state()
    with patch("src.orchestration.verifier_graph.get_settings") as gs:
        m = MagicMock()
        m.verifier_enabled = False
        gs.return_value = m
        with patch("src.orchestration.routing.verifier_fanout.focused_context_text_for_candidate", return_value="ctx"):
            res = verifier_preflight_node(state)
    
    assert res["verifier_skipped_reason"] == "verifier_disabled"


def test_verifier_generate_node_success() -> None:
    state = _minimal_state(
        verifier_focused_context_text="ctx",
        verifier_repo_root="/repo",
        verifier_attempt_idx=0,
    )
    with patch("src.orchestration.verifier_graph.generate_test_script", return_value=("print('ok')", 100)):
        res = verifier_generate_node(state)
    
    assert res["verifier_attempt_idx"] == 1
    assert res["verifier_current_test_code"] == "print('ok')"
    assert res["token_usage"] == 100


def test_verifier_execute_node() -> None:
    state = _minimal_state(
        verifier_attempt_idx=1,
        verifier_current_test_code="print('ok')",
    )
    record = VerifierAttemptRecord(attempt_number=1, test_code="print('ok')", exit_code=0)
    with patch("src.orchestration.verifier_graph.execute_test_script", return_value=record):
        res = verifier_execute_node(state)
    
    assert res["verifier_attempts"] == [record]


def test_verifier_judge_node_refuted() -> None:
    record = VerifierAttemptRecord(attempt_number=1, test_code="x", exit_code=0)
    state = _minimal_state(verifier_attempts=[record])
    res = verifier_judge_node(state)
    
    assert res["verifier_verdict"] == "refuted"


def test_verifier_judge_node_inconclusive_retry() -> None:
    record = VerifierAttemptRecord(attempt_number=1, test_code="x", exit_code=1, stdout="err", stderr="err")
    state = _minimal_state(verifier_attempts=[record])
    with patch("src.orchestration.verifier_graph.build_retry_feedback", return_value="fix it"):
        res = verifier_judge_node(state)
    
    assert res["verifier_verdict"] == "inconclusive"
    assert res["verifier_retry_feedback"] == "fix it"


def test_verifier_routing_continue() -> None:
    state = _minimal_state(
        verifier_verdict="inconclusive",
        verifier_attempt_idx=1,
    )
    with patch("src.orchestration.verifier_graph.get_settings") as gs:
        m = MagicMock()
        m.verifier_max_attempts = 2
        gs.return_value = m
        assert verifier_routing(state) == "generate"


def test_verifier_routing_stop_max_attempts() -> None:
    state = _minimal_state(
        verifier_verdict="inconclusive",
        verifier_attempt_idx=2,
    )
    with patch("src.orchestration.verifier_graph.get_settings") as gs:
        m = MagicMock()
        m.verifier_max_attempts = 2
        gs.return_value = m
        assert verifier_routing(state) == "finalize"


def test_verifier_finalize_product_verified_false_when_any_harness_attempt() -> None:
    harness = VerifierAttemptRecord(
        attempt_number=1,
        test_code="x",
        exit_code=1,
        stdout="STATUS: CRASHED | ExceptionType: ImportError: No module named 'pkg'",
        stderr="",
    )
    product = VerifierAttemptRecord(
        attempt_number=2,
        test_code="x",
        exit_code=1,
        stdout="STATUS: MISMATCH | expected=a actual=b",
        stderr="",
    )
    flags = verifier_hint_flags_for_attempts(
        verdict="verified",
        attempts=[harness, product],
        target_file_path="pkg/mod.py",
    )
    assert flags["harness_error"] is True
    assert flags["product_verified"] is False

    state = _minimal_state(
        verifier_verdict="verified",
        verifier_last_rationale="mismatch",
        verifier_scope="concrete_behavior",
        verifier_attempts=[harness, product],
    )
    with patch("src.orchestration.routing.verifier_fanout._lint_advisory_from_report", return_value=""):
        res = verifier_finalize_node(state)
    report = res["verifier_reports"][0]
    assert report.verdict == "inconclusive"
    hint = res["metadata"]["verifier_hints"]["c1"]
    assert hint["verdict"] == "inconclusive"
    assert hint["harness_error"] is True
    assert hint["product_verified"] is False


def test_verifier_finalize_node() -> None:
    state = _minimal_state(
        verifier_verdict="refuted",
        verifier_last_rationale="no crash",
        verifier_scope="concrete_behavior",
        verifier_attempts=[VerifierAttemptRecord(attempt_number=1)],
        token_usage=100,
    )
    with patch("src.orchestration.routing.verifier_fanout._lint_advisory_from_report", return_value=""):
        res = verifier_finalize_node(state)
    
    assert len(res["verifier_reports"]) == 1
    report = res["verifier_reports"][0]
    assert report.verdict == "refuted"
    assert "verifier" in res["metadata"]


def test_verifier_finalize_records_env_metadata() -> None:
    attempt = VerifierAttemptRecord(
        attempt_number=1,
        exit_code=2,
        stdout="STATUS: HARNESS_ERROR | setup failed",
        stderr="ModuleNotFoundError: No module named 'missing_dep'",
        env_metadata={
            "status": "usable",
            "fingerprint": "abc123",
            "python_path": "/exec/.verifier_venv_abc123/bin/python",
            "install_attempts": [{"target": "requirements.txt", "exit_code": 0}],
            "missing_modules": ["torch"],
            "target_files": ["pkg/mod.py"],
            "target_import_probes": [{"file_path": "pkg/mod.py", "module": "pkg.mod", "status": "failed"}],
            "dependency_install_policy": "targeted_only",
        },
    )
    state = _minimal_state(
        verifier_verdict="verified",
        verifier_last_rationale="mismatch",
        verifier_scope="concrete_behavior",
        verifier_attempts=[attempt],
    )
    with patch("src.orchestration.routing.verifier_fanout._lint_advisory_from_report", return_value=""):
        res = verifier_finalize_node(state)

    env = res["metadata"]["verifier_env"]["c1"]
    assert env["status"] == "usable"
    assert env["fingerprint"] == "abc123"
    assert env["python_path"].endswith("/bin/python")
    assert env["install_attempts"][0]["target"] == "requirements.txt"
    assert env["missing_modules"] == ["torch"]
    assert env["target_files"] == ["pkg/mod.py"]
    assert env["target_import_probes"][0]["module"] == "pkg.mod"
    assert env["dependency_install_policy"] == "targeted_only"
    report = res["verifier_reports"][0]
    assert report.metadata["verifier_env_repair_hints_used"] is True
    assert report.metadata["verifier_repeated_harness_error_count"] == 1
    assert report.metadata["verifier_unrepaired_missing_modules"] == ["missing_dep", "torch"]
    hint = res["metadata"]["verifier_hints"]["c1"]
    assert hint["verifier_env_repair_hints_used"] is True
    assert hint["verifier_unrepaired_missing_modules"] == ["missing_dep", "torch"]
    summary = res["metadata"]["verifier"]["failure_summary_by_candidate"]["c1"]
    assert summary["verifier_repeated_harness_error_count"] == 1
    assert summary["verifier_unrepaired_missing_modules"] == ["missing_dep", "torch"]


def test_verifier_graph_retries_after_harness_failure() -> None:
    state = _minimal_state()
    harness_record = VerifierAttemptRecord(
        attempt_number=1,
        test_code="broken",
        exit_code=2,
        stdout="STATUS: HARNESS_ERROR | SyntaxError: invalid syntax",
        stderr="ModuleNotFoundError: No module named 'missing_dep'",
        sandbox_mode="harness_preflight",
        env_metadata={
            "missing_modules": ["heavy_dep"],
            "target_import_probes": [{"module": "pkg.mod", "status": "failed"}],
        },
    )
    ok_record = VerifierAttemptRecord(
        attempt_number=2,
        test_code="print('ok')",
        exit_code=0,
        stdout="STATUS: SAFE\n",
        stderr="",
    )
    generate_calls: list = []

    def _fake_generate(**kwargs):
        generate_calls.append(dict(kwargs))
        return ("print('ok')", 10)

    with patch("src.orchestration.verifier_graph.get_settings") as gs:
        m = MagicMock()
        m.verifier_enabled = True
        m.verifier_skip_if_no_sandbox = False
        m.verifier_max_attempts = 3
        gs.return_value = m

        with patch("src.orchestration.verifier_graph._sandbox_ok", return_value=True), \
             patch("src.orchestration.verifier_graph._infer_verifier_repo_root", return_value="/repo"), \
             patch("src.orchestration.routing.verifier_fanout.focused_context_text_for_candidate", return_value="ctx"), \
             patch("src.orchestration.verifier_graph.generate_test_script", side_effect=_fake_generate), \
             patch("src.orchestration.verifier_graph.execute_test_script", side_effect=[harness_record, ok_record]), \
             patch("src.orchestration.routing.verifier_fanout._lint_advisory_from_report", return_value=""):
            graph = build_verifier_graph()
            final_state = graph.invoke(state)

    assert len(generate_calls) == 2
    assert generate_calls[1].get("retry_feedback", "").strip() not in ("", "(none)")
    assert "signature_mismatch" in generate_calls[1]["retry_feedback"] or "harness_error" in generate_calls[1]["retry_feedback"] or "syntax_error" in generate_calls[1]["retry_feedback"]
    assert "Missing modules seen: missing_dep, heavy_dep" in generate_calls[1]["retry_feedback"]
    assert "Failed target import probes: pkg.mod" in generate_calls[1]["retry_feedback"]
    assert final_state["verifier_verdict"] == "refuted"
    hint = final_state["metadata"]["verifier_hints"]["c1"]
    assert hint["harness_error"] is True


def test_compiled_verifier_graph_integration() -> None:
    # This tests the full graph flow with mocks
    state = _minimal_state()
    
    with patch("src.orchestration.verifier_graph.get_settings") as gs:
        m = MagicMock()
        m.verifier_enabled = True
        m.verifier_skip_if_no_sandbox = False
        m.verifier_max_attempts = 1
        gs.return_value = m
        
        with patch("src.orchestration.verifier_graph._sandbox_ok", return_value=True), \
             patch("src.orchestration.verifier_graph._infer_verifier_repo_root", return_value="/repo"), \
             patch("src.orchestration.routing.verifier_fanout.focused_context_text_for_candidate", return_value="ctx"), \
             patch("src.orchestration.verifier_graph.generate_test_script", return_value=("print('ok')", 50)), \
             patch("src.orchestration.verifier_graph.execute_test_script") as mock_exec, \
             patch("src.orchestration.routing.verifier_fanout._lint_advisory_from_report", return_value=""):
            
            record = VerifierAttemptRecord(attempt_number=1, test_code="print('ok')", exit_code=0)
            mock_exec.return_value = record
            
            graph = build_verifier_graph()
            final_state = graph.invoke(state)
            
            assert final_state["verifier_verdict"] == "refuted"
            assert len(final_state["verifier_reports"]) == 1
            assert final_state["token_usage"] == 50
            assert "verifier_preflight" in final_state["node_history"]
            assert "verifier_generate" in final_state["node_history"]
            assert "verifier_execute" in final_state["node_history"]
            assert "verifier_judge" in final_state["node_history"]
            assert "verifier_finalize" in final_state["node_history"]
