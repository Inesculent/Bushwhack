"""Tests for coupled mandate-plan loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings
from src.domain.schemas import BehavioralSpec
from src.domain.state import GraphState
from src.infrastructure.behavioral_spec_store import BehavioralSpecStore
from src.orchestration.context.mandate_loop_context import (
    bootstrap_digest,
    build_bootstrap_digest,
    ledger_since_last_patch,
    patch_seq,
    should_skip_bootstrap_explorer,
)
from src.orchestration.routing.mandate_plan_coupling import (
    route_after_intent,
    route_after_mandate_patch,
    route_joint_critic,
)
from src.tools.mandate_exploration_tools import tool_dedupe_key


def test_tool_dedupe_key_stable() -> None:
    k1 = tool_dedupe_key("read_file", {"file_path": "a.py"})
    k2 = tool_dedupe_key("read_file", {"file_path": "a.py"})
    k3 = tool_dedupe_key("read_file", {"file_path": "b.py"})
    assert k1 == k2
    assert k1 != k3


def test_ledger_since_last_patch_uses_applied_count() -> None:
    state: GraphState = {  # type: ignore[assignment]
        "exploration_ledger": [
            {"kind": "mental_model_query", "dedupe_key": "x"},
            {"kind": "mandate_tool_observation", "tool": "read_file", "result_preview": "a"},
            {"kind": "mandate_tool_observation", "tool": "search_code", "result_preview": "b"},
        ],
        "metadata": {"mental_model": {"ledger_applied_count": 2, "patch_seq": 1}},
    }
    delta = ledger_since_last_patch(state)
    assert len(delta) == 1
    assert delta[0]["tool"] == "search_code"


def test_route_after_intent_bootstrap_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_MANDATE_EXPLORER_ENABLED", "true")
    from src.config import get_settings

    get_settings.cache_clear()
    try:
        state: GraphState = {"metadata": {}}  # type: ignore[assignment]
        assert route_after_intent(state) == "mandate_explorer"
    finally:
        monkeypatch.delenv("REVIEW_MANDATE_EXPLORER_ENABLED", raising=False)
        get_settings.cache_clear()


def test_route_after_intent_skips_bootstrap_when_digest_present() -> None:
    state: GraphState = {  # type: ignore[assignment]
        "behavioral_spec_ref": "file:abc",
        "metadata": {"mental_model": {"bootstrap_digest": "contracts: x"}},
    }
    assert route_after_intent(state) == "mandate_patch"


def test_route_after_mandate_patch_first_vs_revision() -> None:
    assert route_after_mandate_patch({"metadata": {}}) == "draft_planner"
    assert (
        route_after_mandate_patch(
            {"metadata": {"actor_critic_planner": {"draft_tasks": [{"id": "t1"}]}}}
        )
        == "plan_revision"
    )


def test_route_joint_critic_aligned_to_finalize() -> None:
    state: GraphState = {  # type: ignore[assignment]
        "metadata": {"actor_critic_planner": {"aligned": True}, "mental_model": {"coupled_loop": {}}},
    }
    assert route_joint_critic(state) == "mandate_finalize"


def test_route_joint_critic_targeted_when_requests() -> None:
    state: GraphState = {  # type: ignore[assignment]
        "metadata": {
            "actor_critic_planner": {
                "aligned": False,
                "exploration_requests": [{"file_path": "x.py", "question": "INPUT_TYPES?"}],
            },
            "mental_model": {"coupled_loop": {"cycles": 0, "explorer_invocations": {}}},
        },
    }
    assert route_joint_critic(state) == "mandate_explorer_targeted"


def test_reviewer_graph_compiles_coupled_path() -> None:
    from src.orchestration.reviewer_graph import build_graph

    g = build_graph()
    assert g is not None


def test_build_bootstrap_digest_bounded() -> None:
    spec = BehavioralSpec(
        intent_summary="Add feature",
        contract_boundaries="API stable",
        risk_hypotheses="edge cases in parsing",
    )
    digest = build_bootstrap_digest(spec, max_chars=100)
    assert len(digest) <= 103


def test_build_bootstrap_digest_includes_surfaces() -> None:
    spec = BehavioralSpec(
        intent_summary="Add feature",
        contract_boundaries="API stable",
        risk_hypotheses="edge cases",
    )
    digest = build_bootstrap_digest(
        spec,
        max_chars=500,
        surface_inventory=["Alpha", "Beta", "Gamma"],
    )
    assert "Surfaces:" in digest
    assert "Alpha" in digest
    assert "Gamma" in digest
