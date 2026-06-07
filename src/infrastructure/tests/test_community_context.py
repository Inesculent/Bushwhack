"""Tests for Phase 2 community context assembly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import Settings
from src.domain.schemas import StructuralTopologySummary
from src.infrastructure.community_context import plan_community_dispatch


def _plots_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "plots"


@pytest.mark.skipif(
    not (_plots_dir() / "structural_graph.json").is_file(),
    reason="plots/structural_graph.json not present",
)
def test_plan_community_dispatch_against_plots_artifacts() -> None:
    graph_path = _plots_dir() / "structural_graph.json"
    topo_path = _plots_dir() / "structural_topology.json"
    graph_payload = json.loads(graph_path.read_text(encoding="utf-8"))
    topo_payload = json.loads(topo_path.read_text(encoding="utf-8"))
    topology = StructuralTopologySummary.model_validate(topo_payload)

    settings = Settings(
        semantic_enrichment_enabled=True,
        semantic_max_tokens_per_community=8000,
        semantic_max_files_per_agent=50,
        semantic_max_symbols_per_agent=100,
        skip_trivial_communities=True,
    )
    trivial, work = plan_community_dispatch(topology, graph_payload, settings)
    assert isinstance(trivial, list)
    assert isinstance(work, list)
    assert trivial or work, "expected at least trivial or work communities"
    for item in work:
        assert item.community_id >= 0
        assert item.file_paths or item.symbol_context_lines


def test_plan_community_dispatch_prioritizes_without_dropping_counts() -> None:
    graph_payload = {
        "directed": True,
        "multigraph": False,
        "nodes": [
            {"id": "file:src/a.py", "node_type": "file", "file_path": "src/a.py"},
            {"id": "file:src/b.py", "node_type": "file", "file_path": "src/b.py"},
            {"id": "file:src/c.py", "node_type": "file", "file_path": "src/c.py"},
            {
                "id": "symbol:a:A",
                "node_type": "symbol",
                "file_path": "src/a.py",
                "symbol_name": "A",
                "signature": "def A():",
                "body": "def A():\n    pass",
            },
            {
                "id": "symbol:b:B",
                "node_type": "symbol",
                "file_path": "src/b.py",
                "symbol_name": "B",
                "signature": "def B():",
                "body": "def B():\n    pass",
            },
            {
                "id": "symbol:c:C",
                "node_type": "symbol",
                "file_path": "src/c.py",
                "symbol_name": "C",
                "signature": "def C():",
                "body": "def C():\n    pass",
            },
            {
                "id": "symbol:d:D",
                "node_type": "symbol",
                "file_path": "src/d.py",
                "symbol_name": "D",
                "signature": "def D():",
                "body": "def D():\n    pass",
            },
        ],
        "edges": [
            {"source": "file:src/a.py", "target": "symbol:a:A", "edge_type": "defines"},
            {"source": "file:src/b.py", "target": "symbol:b:B", "edge_type": "defines"},
            {"source": "file:src/c.py", "target": "symbol:c:C", "edge_type": "defines"},
            {"source": "symbol:b:B", "target": "symbol:d:D", "edge_type": "calls"},
        ],
    }
    topology = StructuralTopologySummary(
        algorithm="test",
        community_count=2,
        communities=[
            {
                "community_id": 0,
                "node_ids": [
                    "file:src/a.py",
                    "file:src/b.py",
                    "file:src/c.py",
                    "symbol:a:A",
                    "symbol:b:B",
                    "symbol:c:C",
                ],
                "file_count": 3,
                "symbol_count": 3,
            },
            {
                "community_id": 1,
                "node_ids": ["symbol:d:D"],
                "file_count": 0,
                "symbol_count": 1,
            },
        ],
        node_to_community={
            "file:src/a.py": 0,
            "file:src/b.py": 0,
            "file:src/c.py": 0,
            "symbol:a:A": 0,
            "symbol:b:B": 0,
            "symbol:c:C": 0,
            "symbol:d:D": 1,
        },
    )
    settings = Settings(
        redis_enabled=False,
        semantic_max_files_per_agent=2,
        semantic_max_symbols_per_agent=2,
    )

    _, work = plan_community_dispatch(
        topology,
        graph_payload,
        settings,
        changed_file_paths={"src/c.py"},
    )

    item = next(w for w in work if w.community_id == 0)
    assert item.total_files == 3
    assert item.total_symbols == 3
    assert item.total_unverified_targets == 1
    assert item.file_paths == ["src/c.py", "src/a.py"]
    assert item.symbol_context_lines[0].startswith("symbol:c:C")
    assert item.symbol_context_lines[1].startswith("symbol:b:B")
