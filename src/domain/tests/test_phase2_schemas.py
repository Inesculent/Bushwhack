"""Unit tests for Phase 2 semantic domain schemas."""

from __future__ import annotations

import pytest

from src.domain.schemas import (
    CommunityAgentOutput,
    CommunitySemanticSummary,
    CommunityWorkItem,
    ExplorationSnapshot,
    FileSemanticSummary,
    GlobalSemanticSynthesisOutput,
    KnowledgeGap,
    ResolverSymbolSummaryOutput,
    SnapshotDiagnostics,
    SymbolSemanticSummary,
    UnverifiedCallTarget,
)


def test_community_semantic_summary_roundtrip() -> None:
    sym = SymbolSemanticSummary(
        symbol_node_id="symbol:abc:foo",
        purpose="Does foo",
        confidence=0.9,
    )
    file_s = FileSemanticSummary(
        file_node_id="file:src/x.py",
        purpose="Holds foo",
        key_symbols=["symbol:abc:foo"],
        confidence=0.85,
    )
    u = UnverifiedCallTarget(
        source_symbol_id="symbol:abc:bar",
        target_name="baz",
        source_community_id=1,
        context_hint="bar() calls baz()",
    )
    c = CommunitySemanticSummary(
        community_id=1,
        label="Test Layer",
        purpose="Testing.",
        file_summaries=[file_s],
        symbol_summaries=[sym],
        unverified_calls=[u],
        cross_community_dependencies=[2],
        confidence=0.8,
    )
    raw = c.model_dump(mode="json")
    assert CommunitySemanticSummary.model_validate(raw).community_id == 1


def test_community_agent_output_nested() -> None:
    inner = CommunitySemanticSummary(
        community_id=0,
        label="Init",
        purpose="Init only",
        confidence=0.5,
    )
    out = CommunityAgentOutput(summary=inner, warnings=["x"])
    assert out.summary.label == "Init"


def test_exploration_snapshot() -> None:
    snap = ExplorationSnapshot(
        snapshot_id="h1",
        run_id="r1",
        snapshot_root="/tmp/r1",
        status="exploration_complete",
        community_count=3,
        total_nodes=10,
        total_edges=20,
        unresolved_call_count=0,
        extraction_gap_count=1,
        metadata={"tokens": 42},
    )
    assert snap.unresolved_call_count == 0


def test_snapshot_diagnostics() -> None:
    d = SnapshotDiagnostics(
        god_nodes=[{"node_id": "s1", "label": "Foo", "degree": 5, "community_id": 0}],
        bridge_nodes=[],
        cross_community_edges=[],
        knowledge_gaps=[
            KnowledgeGap(
                gap_type="isolated_symbol",
                description="Lonely",
                affected_node_ids=["symbol:x:y"],
                severity="low",
            )
        ],
    )
    assert len(d.knowledge_gaps) == 1


def test_community_work_item() -> None:
    w = CommunityWorkItem(
        community_id=2,
        file_paths=["a.py"],
        symbol_context_lines=["id: x"],
        outbound_cross_community_targets=["OtherFn"],
        target_communities_hint=[3],
    )
    assert w.community_id == 2


def test_global_semantic_synthesis_output() -> None:
    g = GlobalSemanticSynthesisOutput(global_summary="Repo does things.")
    assert "things" in g.global_summary


def test_resolver_symbol_summary_output() -> None:
    r = ResolverSymbolSummaryOutput(symbol_node_id="symbol:z:Z", one_line_summary="Z helper.")
    assert r.symbol_node_id.endswith("Z")
