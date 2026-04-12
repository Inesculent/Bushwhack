"""Community detection on structural graphs: Leiden (graspologic) or Louvain (NetworkX), splits, cohesion."""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

import networkx as nx

from src.domain.schemas import StructuralTopologyCommunity, StructuralTopologySummary

_ALLOWED_NODE_TYPES = frozenset({"file", "symbol"})


def structural_digraph_to_clustering_graph(di: nx.DiGraph) -> nx.Graph:
    """Project directed structural bedrock to an undirected simple graph for modularity methods.

    Only file and symbol nodes participate; module/external hubs are excluded. Each unordered
    pair of endpoints is linked at most once (directed parallel edges collapse to one link).
    """
    g = nx.Graph()
    for n in di.nodes():
        if di.nodes[n].get("node_type") in _ALLOWED_NODE_TYPES:
            g.add_node(n)
    seen: set[tuple[str, str]] = set()
    for u, v in di.edges():
        tu = di.nodes[u].get("node_type")
        tv = di.nodes[v].get("node_type")
        if tu not in _ALLOWED_NODE_TYPES or tv not in _ALLOWED_NODE_TYPES:
            continue
        a, b = (u, v) if u <= v else (v, u)
        if (a, b) in seen:
            continue
        seen.add((a, b))
        if g.has_node(a) and g.has_node(b):
            g.add_edge(a, b)
    return g


def _suppress_stdout() -> contextlib.AbstractContextManager[Any]:
    return contextlib.redirect_stdout(io.StringIO())


def _partition(G: nx.Graph, *, louvain_seed: int = 42) -> Tuple[Dict[str, int], str]:
    """Run community detection. Returns ({node_id: community_id}, algorithm_label)."""
    try:
        from graspologic.partition import leiden

        old_stderr = sys.stderr
        try:
            sys.stderr = io.StringIO()
            with _suppress_stdout():
                result = leiden(G)
        finally:
            sys.stderr = old_stderr
        out: Dict[str, int] = {str(k): int(v) for k, v in result.items()}
        return out, "graspologic_leiden"
    except ImportError:
        pass
    except Exception:
        pass

    kwargs: Dict[str, Any] = {"seed": louvain_seed, "threshold": 1e-4}
    if "max_level" in inspect.signature(nx.community.louvain_communities).parameters:
        kwargs["max_level"] = 10
    communities = nx.community.louvain_communities(G, **kwargs)
    partition: Dict[str, int] = {}
    for cid, nodes in enumerate(communities):
        for node in nodes:
            partition[str(node)] = cid
    return partition, "networkx_louvain"


def _split_community(G: nx.Graph, nodes: List[str], *, louvain_seed: int = 42) -> List[List[str]]:
    subgraph = G.subgraph(nodes)
    if subgraph.number_of_edges() == 0:
        return [[n] for n in sorted(nodes)]
    try:
        sub_partition, _ = _partition(subgraph, louvain_seed=louvain_seed)
        sub_communities: Dict[int, List[str]] = {}
        for node, cid in sub_partition.items():
            sub_communities.setdefault(cid, []).append(node)
        if len(sub_communities) <= 1:
            return [sorted(nodes)]
        return [sorted(v) for v in sub_communities.values()]
    except Exception:
        return [sorted(nodes)]


def cohesion_score(G: nx.Graph, community_nodes: List[str]) -> float:
    nodes = sorted(set(community_nodes))
    n = len(nodes)
    if n <= 1:
        return 1.0
    subgraph = G.subgraph(nodes)
    actual = subgraph.number_of_edges()
    possible = n * (n - 1) / 2
    if possible <= 0:
        return 0.0
    raw = actual / possible
    return round(min(1.0, raw), 2)


def score_all(G: nx.Graph, communities: Mapping[int, List[str]]) -> Dict[int, float]:
    return {cid: cohesion_score(G, nodes) for cid, nodes in communities.items()}


def _reindex_by_size(communities: List[List[str]]) -> Tuple[Dict[int, List[str]], Dict[str, int]]:
    ordered = sorted(communities, key=lambda nodes: (-len(nodes), sorted(nodes)))
    new_map: Dict[int, List[str]] = {}
    partition: Dict[str, int] = {}
    for i, nodes in enumerate(ordered):
        sn = sorted(nodes)
        new_map[i] = sn
        for n in sn:
            partition[n] = i
    return new_map, partition


def _counts_for_community(di: nx.DiGraph, nodes: List[str]) -> Tuple[int, int]:
    files = 0
    symbols = 0
    for n in nodes:
        nt = di.nodes[n].get("node_type")
        if nt == "file":
            files += 1
        elif nt == "symbol":
            symbols += 1
    return files, symbols


def _community_needs_split(
    nodes: List[str],
    undirected: nx.Graph,
    di: nx.DiGraph,
    max_size: int,
    max_files: int,
    max_symbols: int,
) -> bool:
    if len(nodes) > max_size:
        return True
    if max_files > 0 or max_symbols > 0:
        fc, sc = _counts_for_community(di, nodes)
        if max_files > 0 and fc > max_files:
            return True
        if max_symbols > 0 and sc > max_symbols:
            return True
    return False


def cluster_structural_graph(
    clustering: nx.Graph,
    structural_di: nx.DiGraph,
    *,
    max_fraction: float = 0.25,
    min_split_size: int = 10,
    max_files: int = 0,
    max_symbols: int = 0,
    louvain_seed: int = 42,
) -> Tuple[Dict[int, List[str]], Dict[str, int], str, int]:
    """Partition nodes into communities; split oversized groups. Returns communities, partition, algorithm, split_count."""
    splits_applied = 0
    algorithm = "none"

    if clustering.number_of_nodes() == 0:
        return {}, {}, algorithm, 0

    undirected = clustering
    if undirected.number_of_edges() == 0:
        comms = [[n] for n in sorted(undirected.nodes())]
        m, p = _reindex_by_size(comms)
        return m, p, "no_edges_singleton", 0

    isolates = [n for n in undirected.nodes() if undirected.degree(n) == 0]
    connected_nodes = [n for n in undirected.nodes() if undirected.degree(n) > 0]
    connected = undirected.subgraph(connected_nodes)

    raw_lists: List[List[str]] = []
    if connected.number_of_nodes() > 0:
        partition, algorithm = _partition(connected, louvain_seed=louvain_seed)
        buckets: Dict[int, List[str]] = {}
        for node, cid in partition.items():
            buckets.setdefault(cid, []).append(node)
        raw_lists.extend(buckets.values())
    else:
        algorithm = "isolates_only"

    raw_lists.extend([[n] for n in sorted(isolates)])

    max_size = max(min_split_size, int(undirected.number_of_nodes() * max_fraction))

    split_round: List[List[str]] = []
    for nodes in raw_lists:
        ns = sorted(nodes)
        if _community_needs_split(ns, undirected, structural_di, max_size, max_files, max_symbols):
            parts = _split_community(undirected, ns, louvain_seed=louvain_seed)
            split_round.extend(parts)
            if len(parts) > 1:
                splits_applied += len(parts) - 1
        else:
            split_round.append(ns)

    rounds = 0
    changed = True
    while changed and rounds < 10:
        changed = False
        rounds += 1
        next_lists: List[List[str]] = []
        for nodes in split_round:
            ns = sorted(nodes)
            if _community_needs_split(ns, undirected, structural_di, max_size, max_files, max_symbols):
                parts = _split_community(undirected, ns, louvain_seed=louvain_seed)
                next_lists.extend(parts)
                if len(parts) > 1:
                    splits_applied += len(parts) - 1
                    changed = True
            else:
                next_lists.append(ns)
        split_round = next_lists

    communities, partition = _reindex_by_size(split_round)
    if algorithm == "none" and communities:
        algorithm = "singleton_fallback"
    return communities, partition, algorithm, splits_applied


@dataclass(frozen=True)
class StructuralTopologyResult:
    clustering_graph: nx.Graph
    communities: Dict[int, List[str]]
    partition: Dict[str, int]
    cohesion_scores: Dict[int, float]
    algorithm: str
    splits_applied: int


def run_structural_topology(
    structural_di: nx.DiGraph,
    *,
    max_fraction: float = 0.25,
    min_split_size: int = 10,
    max_files: int = 0,
    max_symbols: int = 0,
    louvain_seed: int = 42,
) -> StructuralTopologyResult:
    clustering = structural_digraph_to_clustering_graph(structural_di)
    communities, partition, algorithm, splits_applied = cluster_structural_graph(
        clustering,
        structural_di,
        max_fraction=max_fraction,
        min_split_size=min_split_size,
        max_files=max_files,
        max_symbols=max_symbols,
        louvain_seed=louvain_seed,
    )
    scores = score_all(clustering, communities) if communities else {}
    return StructuralTopologyResult(
        clustering_graph=clustering,
        communities=communities,
        partition=partition,
        cohesion_scores=scores,
        algorithm=algorithm,
        splits_applied=splits_applied,
    )


def build_topology_summary(
    result: StructuralTopologyResult,
    structural_di: nx.DiGraph,
    config_snapshot: Dict[str, Any],
) -> StructuralTopologySummary:
    community_models: List[StructuralTopologyCommunity] = []
    for cid in sorted(result.communities.keys()):
        nodes = result.communities[cid]
        fc, sc = _counts_for_community(structural_di, nodes)
        community_models.append(
            StructuralTopologyCommunity(
                community_id=cid,
                node_ids=nodes,
                cohesion=result.cohesion_scores.get(cid, 0.0),
                file_count=fc,
                symbol_count=sc,
            )
        )
    return StructuralTopologySummary(
        algorithm=result.algorithm,
        community_count=len(result.communities),
        communities=community_models,
        node_to_community=dict(sorted(result.partition.items())),
        splits_applied=result.splits_applied,
        config=config_snapshot,
    )


def apply_community_attributes(structural_di: nx.DiGraph, partition: Mapping[str, int]) -> None:
    """Mutate structural DiGraph nodes with community_id (-1 if absent from partition)."""
    for n in structural_di.nodes():
        structural_di.nodes[n]["community_id"] = int(partition.get(n, -1))


def write_topology_summary_json(summary: StructuralTopologySummary, output_path: str) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary.model_dump(mode="json"), indent=2), encoding="utf-8")
    return str(path.resolve())


def draw_topology_graph(
    clustering_graph: nx.Graph,
    partition: Mapping[str, int],
    output_path: str,
    title: str = "Structural topology",
) -> str:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "matplotlib is required to render topology graph images. Install matplotlib to enable drawing."
        ) from exc

    g = clustering_graph
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig = plt.figure(figsize=(12, 8))
    if g.number_of_nodes() == 0:
        plt.text(0.5, 0.5, "No clustering graph nodes", ha="center", va="center")
        plt.axis("off")
    else:
        pos = nx.spring_layout(g, seed=42)
        colors = [partition.get(n, -1) for n in g.nodes()]
        nx.draw(
            g,
            pos=pos,
            node_color=colors,
            cmap=plt.cm.tab20,
            with_labels=False,
            node_size=80,
            width=0.4,
        )
    plt.title(title)
    fig.savefig(out, dpi=170)
    plt.close(fig)
    return str(out.resolve())
