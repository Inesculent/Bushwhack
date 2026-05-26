"""Phase 2: mental model enricher always queries when behavioral_spec_ref is set."""

from __future__ import annotations

from unittest.mock import patch

from src.domain.schemas import ReviewTask
from src.domain.state import GraphState
from src.orchestration.nodes.application.critique_pipeline import (
    _mental_model_bullets,
    _should_skip_mental_model,
    make_mental_model_context_enricher_node,
)


def _state(**overrides: object) -> GraphState:
    base: GraphState = {
        "run_id": "t",
        "repo_path": "/repo",
        "git_diff": "",
        "user_goals": "",
        "behavioral_spec_ref": "spec-ref",
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "direct_context": "code",
                        "probe_flags": {"long_context": True, "risky_keywords": True},
                    }
                }
            }
        },
        "current_task_id": "t1",
        "task_registry": {
            "t1": ReviewTask(
                id="t1",
                title="Simple logic",
                description="Check branch",
                target_files=["a.py"],
                specialty="logic",
            ),
        },
    }
    base.update(overrides)  # type: ignore[typeddict-unknown-key]
    return base


def test_should_skip_without_spec_ref() -> None:
    state = _state()
    state["behavioral_spec_ref"] = None
    skip, reason = _should_skip_mental_model(state)
    assert skip is True
    assert reason == "no_behavioral_spec_ref"


def test_should_not_skip_when_spec_ref() -> None:
    skip, _ = _should_skip_mental_model(_state())
    assert skip is False


def test_mental_model_bullets_caps_lines() -> None:
    raw = "## risks\nReDoS possible\n\n## contracts\nInputs required\nextra"
    out = _mental_model_bullets(raw, max_bullets=3)
    assert out.count("\n") <= 2
    assert out.startswith("- ")


def test_enricher_queries_despite_long_context_flags() -> None:
    node = make_mental_model_context_enricher_node()
    with patch(
        "src.orchestration.nodes.application.critique_pipeline.query_mental_model",
        return_value={
            "answer": "## risks\nPossible registry gap",
            "exploration_ledger": [],
            "skipped": False,
            "skip_reason": "",
        },
    ) as qmm:
        out = node(_state())
    qmm.assert_called_once()
    slot = out["metadata"]["critique_pipeline"]["by_task"]["t1"]
    assert slot["mental_model_skipped"] is False
    assert "registry" in slot["mental_model_excerpt"].lower()


def test_enricher_skipped_without_spec_ref() -> None:
    node = make_mental_model_context_enricher_node()
    with patch(
        "src.orchestration.nodes.application.critique_pipeline.query_mental_model",
    ) as qmm:
        out = node(_state(behavioral_spec_ref=None))
    qmm.assert_not_called()
    slot = out["metadata"]["critique_pipeline"]["by_task"]["t1"]
    assert slot["mental_model_skipped"] is True
