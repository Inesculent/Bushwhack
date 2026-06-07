"""Mandate explorer subgraph loop control (finish flag + step budget)."""

from src.domain.state import GraphState
from src.orchestration.mandate_explorer_graph import (
    _explorer_finished,
    _explorer_route,
    _explorer_step_idx,
    _step_budget,
)


def _state(**kwargs) -> GraphState:
    base: GraphState = {
        "run_id": "test",
        "repo_path": "/repo",
        "git_diff": "",
        "metadata": {"mental_model": {"explorer_mode": "bootstrap"}},
    }
    base.update(kwargs)
    return base


def test_explorer_route_exits_on_finished_flag():
    state = _state(mandate_explorer_finished=True, mandate_explorer_step_idx=1)
    assert _explorer_route(state) == "done"


def test_explorer_route_exits_on_metadata_finished():
    state = _state(
        metadata={
            "mental_model": {
                "explorer_mode": "bootstrap",
                "explorer_run": {"finished": True, "step_idx": 3},
            }
        }
    )
    assert _explorer_finished(state)
    assert _explorer_route(state) == "done"


def test_explorer_route_exits_on_step_budget():
    state = _state(mandate_explorer_step_idx=8, mandate_explorer_mode="bootstrap")
    assert _step_budget(state) == 8
    assert _explorer_route(state) == "done"


def test_explorer_route_continues_under_budget():
    state = _state(mandate_explorer_step_idx=2, mandate_explorer_finished=False)
    assert _explorer_route(state) == "step"


def test_explorer_step_idx_reads_metadata_fallback():
    state = _state(
        metadata={
            "mental_model": {
                "explorer_run": {"step_idx": 5},
            }
        }
    )
    assert _explorer_step_idx(state) == 5
