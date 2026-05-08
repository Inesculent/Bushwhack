"""Integration-style tests for Phase 2 wiring (no external LLM)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import Settings
from src.domain.schemas import (
    CommunityAgentOutput,
    CommunitySemanticSummary,
    CommunityWorkItem,
    StructuralTopologySummary,
)
from src.domain.state import GraphState
from src.infrastructure.snapshot_writer import SnapshotWriter
from src.orchestration.nodes.exploration.community_semantic_agent import make_community_semantic_agent_node
from src.orchestration.prompts.exploration_prompts import (
    render_community_semantic_prompt,
    render_explorer_prompt,
    render_semantic_merge_prompt,
    render_unverified_call_resolver_prompt,
)
from src.orchestration.nodes.exploration.semantic_dispatch import (
    make_semantic_dispatch_node,
    route_semantic_dispatch,
)
from src.orchestration.nodes.exploration.semantic_merge import make_semantic_merge_node
from src.orchestration.nodes.exploration.snapshot_pin import make_snapshot_pin_node
from src.orchestration.nodes.exploration.unverified_call_resolver import make_unverified_call_resolver_node
from src.infrastructure.snapshot_pointer_store import InMemorySnapshotPointerStore


def test_exploration_prompts_are_role_specific() -> None:
    work = {
        "community_id": 3,
        "file_paths": ["src/demo.py"],
        "symbol_context_lines": ["symbol: demo.run body: calls helper()"],
        "outbound_cross_community_targets": ["helper"],
        "target_communities_hint": [4],
    }
    community_prompt = render_community_semantic_prompt(
        repo_path="repo",
        item=CommunityWorkItem.model_validate(work),
    )
    explorer_prompt = render_explorer_prompt(
        repo_path="repo",
        user_goals="review only",
        git_diff="diff --git a/x b/x",
    )
    resolver_prompt = render_unverified_call_resolver_prompt(
        symbol_node_id="symbol:demo.run",
        body_text="def run(): pass",
    )
    merge_prompt = render_semantic_merge_prompt(
        [
            CommunitySemanticSummary(
                community_id=3,
                label="Demo Flow",
                purpose="Coordinates demo execution.",
                file_summaries=[],
                symbol_summaries=[],
                unverified_calls=[],
                cross_community_dependencies=[4],
                confidence=0.8,
            )
        ]
    )

    assert "changed entry points" in explorer_prompt
    assert "unverified_calls" in community_prompt
    assert "Do not infer hidden implementation details" in community_prompt
    assert "exactly one sentence" in resolver_prompt
    assert "community boundaries" in merge_prompt


def test_route_semantic_dispatch_empty_queue() -> None:
    state: GraphState = {  # type: ignore[typeddict-item]
        "run_id": "t1",
        "repo_path": "r",
        "git_diff": "",
        "semantic_community_work_queue": [],
    }
    assert route_semantic_dispatch(state) == "unverified_call_resolver"


def test_route_semantic_dispatch_batches_queue() -> None:
    state: GraphState = {  # type: ignore[typeddict-item]
        "run_id": "t1",
        "repo_path": "r",
        "git_diff": "",
        "semantic_community_work_queue": [
            {"community_id": i, "file_paths": [], "symbol_context_lines": []}
            for i in range(6)
        ],
        "semantic_dispatch_cursor": 0,
    }
    routed = route_semantic_dispatch(state)
    assert isinstance(routed, list)
    assert len(routed) == Settings().semantic_max_parallel_agents


def test_semantic_dispatch_advances_cursor_for_next_wave() -> None:
    node = make_semantic_dispatch_node(Settings(semantic_max_parallel_agents=3))
    state: GraphState = {  # type: ignore[typeddict-item]
        "run_id": "t1",
        "repo_path": "r",
        "git_diff": "",
        "metadata": {"semantic_phase2": {"dispatch": "ok"}},
        "semantic_community_work_queue": [
            {"community_id": i, "file_paths": [], "symbol_context_lines": []}
            for i in range(7)
        ],
        "semantic_dispatch_cursor": 3,
    }
    out = node(state)
    assert out["semantic_dispatch_cursor"] == 6


@pytest.mark.skipif(
    not (Path("plots") / "structural_graph.json").is_file(),
    reason="plots artifacts",
)
def test_semantic_dispatch_plans_when_reducer_supplies_empty_queue() -> None:
    graph_payload = json.loads((Path("plots") / "structural_graph.json").read_text(encoding="utf-8"))
    topo = StructuralTopologySummary.model_validate(
        json.loads((Path("plots") / "structural_topology.json").read_text(encoding="utf-8"))
    )
    node = make_semantic_dispatch_node(Settings(redis_enabled=False))
    state: GraphState = {  # type: ignore[typeddict-item]
        "run_id": "t1",
        "repo_path": "r",
        "git_diff": "",
        "structural_graph_node_link": graph_payload,
        "structural_topology": topo,
        "semantic_community_work_queue": [],
    }
    out = node(state)
    assert out["metadata"]["semantic_phase2"]["dispatch"] == "ok"
    assert out["metadata"]["semantic_phase2"]["pending_community_agents"] > 0
    assert out["semantic_community_work_queue"]


def test_community_agent_waits_on_active_local_server_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        redis_enabled=False,
        semantic_agent_max_retries=0,
        semantic_agent_retry_backoff_seconds=0,
        semantic_agent_timeout_patience_seconds=60,
    )
    calls = {"invoke": 0}

    class FakeLlm:
        def invoke(self, _prompt: str) -> CommunityAgentOutput:
            calls["invoke"] += 1
            if calls["invoke"] == 1:
                raise TimeoutError("Request timed out.")
            return CommunityAgentOutput(
                summary=CommunitySemanticSummary(
                    community_id=-1,
                    label="Real Summary",
                    purpose="Completed after a patient retry.",
                    file_summaries=[],
                    symbol_summaries=[],
                    unverified_calls=[],
                    cross_community_dependencies=[],
                    confidence=0.9,
                ),
                warnings=[],
            )

    monkeypatch.setattr(
        "src.orchestration.nodes.exploration.community_semantic_agent.Models.worker",
        lambda *_args, **_kwargs: FakeLlm(),
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.exploration.community_semantic_agent._local_llm_server_active",
        lambda _settings: (True, "status ok"),
    )

    agent = make_community_semantic_agent_node(settings=settings)
    out = agent(
        {
            "run_id": "t1",
            "repo_path": "r",
            "git_diff": "",
            "semantic_community_work_item": {
                "community_id": 7,
                "file_paths": ["src/demo.py"],
                "symbol_context_lines": ["stub"],
                "outbound_cross_community_targets": [],
                "target_communities_hint": [],
            },
        }
    )

    assert calls["invoke"] == 2
    assert out["metadata"]["semantic_phase2"]["community_7"]["attempts"] == 2
    assert out["metadata"]["semantic_phase2"]["community_7"]["warnings"] == ["llm_timeout_server_active"]
    assert out["community_summaries"][0].purpose == "Completed after a patient retry."


@pytest.mark.skipif(
    not (Path("plots") / "structural_graph.json").is_file(),
    reason="plots artifacts",
)
def test_phase2_stub_agent_merge_and_snapshot(tmp_path: Path) -> None:
    graph_payload = json.loads((Path("plots") / "structural_graph.json").read_text(encoding="utf-8"))
    topo = StructuralTopologySummary.model_validate(
        json.loads((Path("plots") / "structural_topology.json").read_text(encoding="utf-8"))
    )

    settings = Settings(
        snapshot_base_path=tmp_path,
        redis_enabled=False,
        semantic_enrichment_enabled=True,
        structural_topology_enabled=True,
    )

    agent = make_community_semantic_agent_node(settings=settings, use_llm=False)
    work = {
        "community_id": topo.communities[0].community_id,
        "file_paths": ["src/demo.py"],
        "symbol_context_lines": ["stub"],
        "outbound_cross_community_targets": [],
        "target_communities_hint": [],
    }
    run_id = "phase2-test:owner__repo__pr123"
    base_state: GraphState = {  # type: ignore[typeddict-item]
        "run_id": run_id,
        "repo_path": str(tmp_path),
        "git_diff": "",
        "structural_graph_node_link": graph_payload,
        "structural_topology": topo,
        "community_summaries": [],
        "semantic_community_work_item": work,
    }
    out_agent = agent(base_state)
    assert out_agent.get("community_summaries")

    state2: GraphState = {**base_state, **out_agent}  # type: ignore[typeddict-item]
    resolver = make_unverified_call_resolver_node(ast_parser=None, settings=settings, use_llm=False)
    out_res = resolver(state2)
    assert "resolved_unverified_calls" in out_res

    state3: GraphState = {**state2, **out_res}  # type: ignore[typeddict-item]
    merge = make_semantic_merge_node(settings=settings, use_llm=False)
    out_merge = merge(state3)
    assert out_merge.get("structural_graph_node_link")
    assert "global_summary" in out_merge

    state4: GraphState = {**state3, **out_merge}  # type: ignore[typeddict-item]
    writer = SnapshotWriter(settings)
    store = InMemorySnapshotPointerStore()
    pin = make_snapshot_pin_node(writer, store, settings=settings)
    out_pin = pin(state4)
    snapshot_root = out_pin.get("snapshot_root")
    assert snapshot_root
    assert ":" not in Path(str(snapshot_root)).name
    assert store.pointers.get(run_id)
    snapshot_json = Path(str(snapshot_root)) / "snapshot.json"
    assert snapshot_json.is_file()
    snapshot_payload = json.loads(snapshot_json.read_text(encoding="utf-8"))
    assert snapshot_payload["run_id"] == run_id
    assert snapshot_payload["run_dir_name"] == Path(str(snapshot_root)).name
