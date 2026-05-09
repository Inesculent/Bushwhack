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
