"""Annotate structural graphs with semantic node attributes (Phase 2)."""

from __future__ import annotations

from typing import Any, Literal, Optional

import networkx as nx

Provenance = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]


class SemanticGraphAnnotator:
    """Add semantic fields and optional INFERRED edges without mutating structural bedrock."""

    @staticmethod
    def annotate_node(
        graph: nx.DiGraph,
        node_id: str,
        *,
        purpose: str,
        rationale: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> None:
        if not graph.has_node(node_id):
            return
        data = graph.nodes[node_id]
        data["semantic_purpose"] = purpose
        if rationale is not None:
            data["semantic_rationale"] = rationale
        if confidence is not None:
            data["semantic_confidence"] = confidence

    @staticmethod
    def add_semantic_edge(
        graph: nx.DiGraph,
        source: str,
        target: str,
        *,
        edge_type: str,
        provenance: Provenance,
        confidence: float,
        run_id: str,
        detail: str = "",
    ) -> None:
        if provenance not in ("EXTRACTED", "INFERRED", "AMBIGUOUS"):
            raise ValueError(f"Invalid provenance: {provenance}")
        if not graph.has_node(source) or not graph.has_node(target):
            return
        graph.add_edge(
            source,
            target,
            edge_type=edge_type,
            provenance=provenance,
            confidence=confidence,
            run_id=run_id,
            semantic_detail=detail,
        )
