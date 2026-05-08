"""Deterministic graph diagnostics for Phase 2 snapshots."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import networkx as nx

from src.domain.schemas import KnowledgeGap, SnapshotDiagnostics, StructuralTopologyCommunity


def _symbol_label(graph: nx.DiGraph, node_id: str) -> str:
    if not graph.has_node(node_id):
        return node_id
    return str(graph.nodes[node_id].get("symbol_name") or graph.nodes[node_id].get("id") or node_id)


def _provenance_weight(graph: nx.DiGraph, u: str, v: str) -> int:
    data = graph.get_edge_data(u, v) or {}
    prov = str(data.get("provenance", "EXTRACTED") or "EXTRACTED").upper()
    if prov == "AMBIGUOUS":
        return 3
    if prov == "INFERRED":
        return 2
    return 1


def _eligible_symbol_nodes(graph: nx.DiGraph) -> List[str]:
    out: List[str] = []
    for node_id, attrs in graph.nodes(data=True):
        nt = attrs.get("node_type")
        if nt != "symbol":
            continue
        out.append(str(node_id))
    return out


def compute_god_nodes(
    graph: nx.DiGraph,
    partition: Mapping[str, int],
    *,
    top_n: int = 10,
) -> List[Dict[str, Any]]:
    """Top degree symbol nodes (excluding file/module hubs)."""
    scores: List[Tuple[int, str]] = []
    for node_id in _eligible_symbol_nodes(graph):
        deg = int(graph.degree(node_id))
        scores.append((deg, node_id))
    scores.sort(reverse=True)
    rows: List[Dict[str, Any]] = []
    for deg, node_id in scores[:top_n]:
        rows.append(
            {
                "node_id": node_id,
                "label": _symbol_label(graph, node_id),
                "degree": deg,
                "community_id": int(partition.get(node_id, -1)),
            }
        )
    return rows


def compute_bridge_nodes(
    graph: nx.DiGraph,
    partition: Mapping[str, int],
    *,
    top_n: int = 5,
) -> List[Dict[str, Any]]:
    """Approximate high-betweenness symbols that bridge multiple communities."""
    symbols = _eligible_symbol_nodes(graph)
    if not symbols:
        return []
    sub = graph.subgraph(symbols).copy()
    if sub.number_of_nodes() == 0:
        return []
    k = min(100, max(10, sub.number_of_nodes()))
    try:
        cent = nx.betweenness_centrality(sub, k=k, seed=42, normalized=True)
    except Exception:
        cent = nx.betweenness_centrality(sub, normalized=True)

    bridged: Dict[str, Set[int]] = {}
    for u, v in graph.edges():
        if u not in cent and v not in cent:
            continue
        cu, cv = partition.get(u), partition.get(v)
        if cu is None or cv is None or cu == cv:
            continue
        for node in (u, v):
            if node not in cent:
                continue
            if graph.nodes[node].get("node_type") != "symbol":
                continue
            bridged.setdefault(node, set()).update({int(cu), int(cv)})

    ranked = sorted(((cent.get(n, 0.0), n) for n in cent.keys()), reverse=True)
    rows: List[Dict[str, Any]] = []
    for score, node_id in ranked:
        if len(rows) >= top_n:
            break
        communities = sorted(bridged.get(node_id, ()))
        if len(communities) < 2:
            continue
        rows.append(
            {
                "node_id": node_id,
                "label": _symbol_label(graph, node_id),
                "betweenness": float(score),
                "communities_bridged": communities,
            }
        )
    return rows


def compute_cross_community_edges(
    graph: nx.DiGraph,
    partition: Mapping[str, int],
    *,
    top_n: int = 10,
) -> List[Dict[str, Any]]:
    """Cross-community structural edges scored by provenance weight, deduped per community pair."""
    best: Dict[Tuple[int, int], Tuple[int, str, str, Dict[str, Any]]] = {}
    for u, v in graph.edges():
        cu, cv = partition.get(u), partition.get(v)
        if cu is None or cv is None or cu == cv:
            continue
        pair = (min(int(cu), int(cv)), max(int(cu), int(cv)))
        w = _provenance_weight(graph, u, v)
        prev = best.get(pair)
        if prev is None or w > prev[0]:
            data = dict(graph.get_edge_data(u, v) or {})
            data["source"] = u
            data["target"] = v
            best[pair] = (w, u, v, data)
    ranked = sorted(best.values(), key=lambda item: item[0], reverse=True)
    rows: List[Dict[str, Any]] = []
    for w, u, v, data in ranked[:top_n]:
        row = dict(data)
        row["score"] = w
        row["source_community"] = int(partition[u])
        row["target_community"] = int(partition[v])
        rows.append(row)
    return rows


def detect_knowledge_gaps(
    graph: nx.DiGraph,
    partition: Mapping[str, int],
    communities: Iterable[StructuralTopologyCommunity],
    *,
    low_cohesion_threshold: float = 0.15,
    min_nodes_for_cohesion_gap: int = 5,
) -> List[KnowledgeGap]:
    gaps: List[KnowledgeGap] = []
    ambiguous_counts: Dict[int, int] = defaultdict(int)
    ambiguous_totals: Dict[int, int] = defaultdict(int)

    for u, v, attrs in graph.edges(data=True):
        prov = str(attrs.get("provenance", "EXTRACTED") or "EXTRACTED").upper()
        for node in (u, v):
            cid = partition.get(node)
            if cid is None:
                continue
            ambiguous_totals[int(cid)] += 1
            if prov == "AMBIGUOUS":
                ambiguous_counts[int(cid)] += 1

    for comm in communities:
        if comm.cohesion < low_cohesion_threshold and len(comm.node_ids) >= min_nodes_for_cohesion_gap:
            gaps.append(
                KnowledgeGap(
                    gap_type="low_cohesion",
                    description=(
                        f"Community {comm.community_id} has low cohesion "
                        f"({comm.cohesion:.3f}) with {len(comm.node_ids)} nodes."
                    ),
                    affected_node_ids=list(comm.node_ids)[:50],
                    community_id=comm.community_id,
                    severity="medium",
                )
            )
        total = ambiguous_totals.get(comm.community_id, 0)
        amb = ambiguous_counts.get(comm.community_id, 0)
        if total >= 8 and amb / max(total, 1) >= 0.35:
            gaps.append(
                KnowledgeGap(
                    gap_type="ambiguous_heavy",
                    description=(
                        f"Community {comm.community_id} has high AMBIGUOUS edge concentration "
                        f"({amb}/{total})."
                    ),
                    affected_node_ids=list(comm.node_ids)[:50],
                    community_id=comm.community_id,
                    severity="medium",
                )
            )

    isolated_candidates = [
        node_id
        for node_id in _eligible_symbol_nodes(graph)
        if int(graph.degree(node_id)) <= 1
    ]
    for node_id in isolated_candidates[:120]:
        cid = partition.get(node_id)
        gaps.append(
            KnowledgeGap(
                gap_type="isolated_symbol",
                description=f"Symbol {node_id} has very low structural degree.",
                affected_node_ids=[node_id],
                community_id=int(cid) if cid is not None else None,
                severity="low",
            )
        )

    return gaps


def build_snapshot_diagnostics(
    graph: nx.DiGraph,
    partition: Mapping[str, int],
    communities: List[StructuralTopologyCommunity],
    *,
    god_top_n: int,
    bridge_top_n: int,
    cross_edge_top_n: int,
    low_cohesion_threshold: float,
) -> SnapshotDiagnostics:
    god = compute_god_nodes(graph, partition, top_n=god_top_n)
    bridges = compute_bridge_nodes(graph, partition, top_n=bridge_top_n)
    cross_edges = compute_cross_community_edges(graph, partition, top_n=cross_edge_top_n)
    gaps = detect_knowledge_gaps(
        graph,
        partition,
        communities,
        low_cohesion_threshold=low_cohesion_threshold,
    )
    return SnapshotDiagnostics(
        god_nodes=god,
        bridge_nodes=bridges,
        cross_community_edges=cross_edges,
        knowledge_gaps=gaps,
    )
