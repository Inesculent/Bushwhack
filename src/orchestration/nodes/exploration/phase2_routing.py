"""Shared routing helpers for Phase 2 semantic enrichment."""

from __future__ import annotations

from src.config import Settings, get_settings
from src.domain.schemas import StructuralTopologySummary
from src.domain.state import GraphState


def semantic_phase2_should_run(state: GraphState, settings: Settings | None = None) -> bool:
    """Whether to insert the Phase 2 subgraph between structural extraction and planning."""
    resolved = settings or get_settings()
    if not resolved.semantic_enrichment_enabled:
        return False
    if state.get("snapshot_root"):
        return False
    topo = state.get("structural_topology")
    if topo is None:
        return False
    try:
        summary = (
            topo if isinstance(topo, StructuralTopologySummary) else StructuralTopologySummary.model_validate(topo)
        )
    except Exception:
        return False
    if not summary.communities:
        return False
    if not state.get("structural_graph_node_link"):
        return False
    return True
