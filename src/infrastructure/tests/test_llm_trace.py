from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.config import Settings
from src.domain.schemas import (
    CandidateFinding,
    CritiquerOutput,
    ReflectionBatchOutput,
    ReflectionReport,
    ReviewTask,
)
from src.infrastructure.llm.trace import trace_llm_call
from src.orchestration.nodes.application.critiquer import make_general_critiquer_node
from src.orchestration.nodes.application.planner import ReviewPlanOutput, run_planner_generation
from src.orchestration.nodes.application.reflection import _reflect_specialty_batches
from src.orchestration.nodes.verifier.test_generator import generate_test_script
from src.domain.verifier_schemas import VerifierTestGeneratorOutput
from src.reviewer_agent.harness.aacr import _write_raw


class _Raw:
    usage_metadata = {
        "input_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 7,
    }
    response_metadata = None
    content = "raw assistant response that should be summarized"


class _FakeLLM:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _trace_state() -> dict[str, Any]:
    return {
        "run_id": "trace-test",
        "repo_path": "/repo",
        "git_diff": "diff --git a/a.py b/a.py",
        "metadata": {"review_trace_enabled": True},
        "global_insights": [],
        "node_history": [],
        "token_usage": 0,
    }


def test_trace_llm_call_disabled_returns_no_records() -> None:
    traced = trace_llm_call(
        _FakeLLM({"raw": _Raw()}),
        "prompt text",
        state={"metadata": {"review_trace_enabled": False}},
        node_name="node",
    )
    assert traced.tokens == 7
    assert traced.trace_records == []


def test_trace_llm_call_records_bounded_token_details(caplog) -> None:
    caplog.set_level(logging.INFO, logger="research_pipeline.reviewer_trace")
    traced = trace_llm_call(
        _FakeLLM({"raw": _Raw()}),
        "x" * 1000,
        state=_trace_state(),
        node_name="node",
        model_key="model-a",
        schema_name="Schema",
        input_summary={"large": "y" * 1000},
    )
    assert traced.tokens == 7
    assert [r["event"] for r in traced.trace_records] == ["llm_request", "llm_response"]
    response = traced.trace_records[-1]
    assert response["token_usage"]["prompt_tokens"] == 3
    assert response["token_usage"]["completion_tokens"] == 4
    assert response["live_total_tokens"] >= 7
    assert len(traced.trace_records[0]["prompt"]["preview"]) <= 240
    assert "TRACE llm_response" in caplog.text


def test_planner_generation_returns_llm_trace(monkeypatch) -> None:
    output = ReviewPlanOutput(
        summary="plan",
        tasks=[
            ReviewTask(
                id="t1",
                title="Logic",
                description="Check logic",
                target_files=["a.py"],
                specialty="logic",
            )
        ],
    )
    fake = _FakeLLM({"parsed": output, "raw": _Raw()})
    monkeypatch.setattr(
        "src.orchestration.nodes.application.planner.Models.planner",
        lambda *_args, **_kwargs: fake,
    )
    tasks, _summary, _warnings, tokens, llm_trace = run_planner_generation(_trace_state())
    assert tasks
    assert tokens == 7
    assert any(record["event"] == "llm_response" for record in llm_trace)


def test_critiquer_retry_records_error_and_retry_trace(monkeypatch) -> None:
    task = ReviewTask(
        id="t1",
        title="Logic",
        description="Check logic",
        target_files=["a.py"],
        specialty="logic",
    )
    state = {
        **_trace_state(),
        "current_task_id": "t1",
        "task_registry": {"t1": task},
        "metadata": {
            "review_trace_enabled": True,
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "direct_context": "code",
                        "context_packet": {"sections": []},
                        "task_evidence": {},
                    }
                }
            },
        },
    }

    class LengthFinishReasonError(Exception):
        pass

    calls = {"count": 0}

    class FakeCritiquer:
        def invoke(self, _prompt: str) -> Any:
            calls["count"] += 1
            if calls["count"] == 1:
                raise LengthFinishReasonError("length limit")
            return {"parsed": CritiquerOutput(summary="ok"), "raw": _Raw()}

    monkeypatch.setattr(
        "src.orchestration.nodes.application.critiquer.Models.worker",
        lambda *_args, **_kwargs: FakeCritiquer(),
    )
    out = make_general_critiquer_node(context_provider=object(), use_pipeline_cache=True)(state)
    events = [record["event"] for record in out["llm_trace"]]
    assert "llm_error" in events
    assert "llm_response" in events
    assert out["token_usage"] == 7


def test_reflection_batch_returns_llm_trace(monkeypatch) -> None:
    candidate = CandidateFinding(
        candidate_id="c1",
        patch_task_id="t1",
        file_path="a.py",
        line_start=1,
        line_end=1,
        content="Issue",
    )
    output = ReflectionBatchOutput(
        reports=[
            ReflectionReport(
                candidate_id="c1",
                reflector_specialty="logic",
                verdict="accept",
                rationale="evidence",
            )
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.reflection.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    _reports, _requests, _warnings, tokens, llm_trace = _reflect_specialty_batches(
        state=_trace_state(),
        specialty="logic",
        specialty_candidates=[candidate],
        selected_model="model-a",
        resolved_settings=Settings(),
        mental_model_ledger_snippet="",
        use_llm=True,
    )
    assert tokens == 7
    assert any(record["node"] == "adversarial_reflection" for record in llm_trace)


def test_verifier_generation_returns_llm_trace(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.orchestration.nodes.verifier.test_generator.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM(
            {
                "parsed": VerifierTestGeneratorOutput(test_code="print('ok')"),
                "raw": _Raw(),
            }
        ),
    )
    code, tokens, llm_trace = generate_test_script(
        candidate={"candidate_id": "c1", "file_path": "a.py", "line_start": 1, "line_end": 1},
        state=_trace_state(),
    )
    assert code == "print('ok')"
    assert tokens == 7
    assert llm_trace[-1]["node"] == "verifier_generate"


def test_write_raw_includes_llm_trace(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    path = _write_raw(raw_dir, "slug", {"metadata": {}, "llm_trace": [{"event": "llm_response"}]})
    assert '"llm_trace"' in path.read_text(encoding="utf-8")
