"""Tests for mental-model orchestration helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings
from src.domain.schemas import BehavioralSpec
from src.domain.state import GraphState
from src.infrastructure.behavioral_spec_store import BehavioralSpecStore
from src.orchestration.prompts.ledger_formatter import format_exploration_ledger_for_prompt
from src.orchestration.routing.send_payload import payload_for_send
from src.tools.mental_model_tools import query_mental_model


def test_behavioral_spec_store_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    store = BehavioralSpecStore(settings)
    spec = BehavioralSpec(
        intent_summary="Add login",
        behavioral_expectations="Users can sign in.",
        contract_boundaries="Session API stable.",
        historical_precedents="Prior PRs used JWT.",
        risk_hypotheses="Hypothesis: token expiry edge cases.",
        reviewer_guidance="Stay structural.",
    )
    ref, _abs = store.write("run-abc", spec)
    assert ref.startswith("file:")
    loaded = store.read(ref)
    assert loaded.intent_summary == "Add login"


def test_payload_for_send_is_shallow_copy() -> None:
    state: GraphState = {  # type: ignore[assignment]
        "run_id": "r1",
        "repo_path": "/repo",
        "git_diff": "diff",
        "exploration_ledger": [{"kind": "mental_model_query", "dedupe_key": "k1"}],
    }
    p = payload_for_send(state, current_task_id="t1")
    assert p["current_task_id"] == "t1"
    assert p["exploration_ledger"] == state["exploration_ledger"]
    p["extra"] = True
    assert "extra" not in state


def test_format_exploration_ledger_caps_and_prioritizes_task() -> None:
    ledger = [
        {"kind": "mental_model_query", "dedupe_key": "a", "query_preview": "q1", "answer_preview": "a1", "task_id": "t2"},
        {"kind": "mental_model_query", "dedupe_key": "b", "query_preview": "t1 files", "answer_preview": "a2", "task_id": "t1"},
    ]
    text, stats = format_exploration_ledger_for_prompt(
        ledger,
        task_id="t1",
        target_files=["src/x.py"],
        max_entries=1,
        max_chars=500,
    )
    assert "t1" in text or "files" in text
    assert stats.rendered <= 1


def test_query_mental_model_dedupe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    store = BehavioralSpecStore(settings)
    spec = BehavioralSpec(intent_summary="Hello")
    ref, _ = store.write("run-dedupe", spec)
    state: GraphState = {  # type: ignore[assignment]
        "run_id": "run-dedupe",
        "repo_path": str(tmp_path),
        "git_diff": "",
        "behavioral_spec_ref": ref,
        "exploration_ledger": [],
        "metadata": {},
    }
    monkeypatch.setattr("src.tools.mental_model_tools.get_settings", lambda: settings)
    r1 = query_mental_model(state=state, query="What is intent?", caller="t1")
    assert not r1["skipped"]
    state2 = {**state, "exploration_ledger": list(r1["exploration_ledger"])}
    r2 = query_mental_model(state=state2, query="What is intent?", caller="t1")
    assert r2["skipped"] and r2["skip_reason"] == "dedupe_cache_hit"


def test_reviewer_graph_compiles_legacy_planner_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("langgraph")
    try:
        from src.config import get_settings
        from src.orchestration.reviewer_graph import build_graph
    except ImportError as exc:
        pytest.skip(f"reviewer graph import unavailable ({exc})")

    monkeypatch.setenv("REVIEW_REVIEWER_LEGACY_PLANNER_MODE", "true")
    get_settings.cache_clear()
    try:
        g = build_graph()
        assert g is not None
    finally:
        monkeypatch.delenv("REVIEW_REVIEWER_LEGACY_PLANNER_MODE", raising=False)
        get_settings.cache_clear()


def test_snapshot_pin_skips_write_when_snapshot_source_loaded() -> None:
    import sys
    import types
    from unittest.mock import MagicMock

    if "redis" not in sys.modules:
        _redis_stub = types.ModuleType("redis")
        _redis_stub.Redis = MagicMock  # type: ignore[attr-defined]
        sys.modules["redis"] = _redis_stub

    from src.config import Settings
    from src.orchestration.nodes.exploration.snapshot_pin import make_snapshot_pin_node

    settings = Settings()
    writer = MagicMock()
    ptr = MagicMock()
    node = make_snapshot_pin_node(writer, ptr, settings=settings)
    out = node(
        {
            "run_id": "resume-run",
            "repo_path": "/repo",
            "git_diff": "",
            "snapshot_source": "loaded",
            "snapshot_id": "snap1",
            "snapshot_root": "/snap/root",
            "behavioral_spec_ref": "file:/tmp/spec.json",
            "metadata": {"exploration_snapshot": {"snapshot_id": "old"}},
        }
    )
    writer.write_snapshot.assert_not_called()
    ptr.write_pointer.assert_not_called()
    assert "snapshot_pin:loaded_passthrough" in out["node_history"]
    meta_snap = out["metadata"]["exploration_snapshot"]
    assert meta_snap["snapshot_id"] == "snap1"
    assert meta_snap["metadata"]["behavioral_spec_ref"] == "file:/tmp/spec.json"
