"""Tests for structural graph projection and community detection."""

from __future__ import annotations

from unittest import mock

import networkx as nx
import pytest

from src.infrastructure.structural_topology import (
    cluster_structural_graph,
    cohesion_score,
    run_structural_topology,
    structural_digraph_to_clustering_graph,
)


def _make_di_two_files_two_symbols() -> nx.DiGraph:
    """file:a <-> symbol:a1, file:b <-> symbol:b1 plus cross edge symbol to symbol."""
    g = nx.DiGraph()
    g.add_node("file:a.py", node_type="file", file_path="a.py")
    g.add_node("file:b.py", node_type="file", file_path="b.py")
    g.add_node("sym:a1", node_type="symbol", file_path="a.py")
    g.add_node("sym:b1", node_type="symbol", file_path="b.py")
    g.add_node("mod:x", node_type="module", module_name="x")
    g.add_edge("file:a.py", "sym:a1", edge_type="defines")
    g.add_edge("file:b.py", "sym:b1", edge_type="defines")
    g.add_edge("sym:a1", "sym:b1", edge_type="calls")
    g.add_edge("sym:a1", "mod:x", edge_type="imports")
    return g


def test_structural_digraph_to_clustering_graph_excludes_module() -> None:
    di = _make_di_two_files_two_symbols()
    ug = structural_digraph_to_clustering_graph(di)
    assert set(ug.nodes()) == {"file:a.py", "file:b.py", "sym:a1", "sym:b1"}
    assert ug.number_of_edges() == 3  # file-symbol x2 + symbol-symbol


def test_run_structural_topology_empty() -> None:
    g = nx.DiGraph()
    r = run_structural_topology(g)
    assert r.communities == {}
    assert r.algorithm == "none"


def test_run_structural_topology_no_edges_singletons() -> None:
    g = nx.DiGraph()
    g.add_node("file:x.py", node_type="file")
    r = run_structural_topology(g)
    assert r.algorithm == "no_edges_singleton"
    assert len(r.communities) == 1


def test_cohesion_score_complete_pair() -> None:
    g = nx.Graph()
    g.add_edge("a", "b")
    assert cohesion_score(g, ["a", "b"]) == 1.0


def test_networkx_louvain_when_graspologic_leiden_fails() -> None:
    pytest.importorskip("graspologic")
    di = _make_di_two_files_two_symbols()
    with mock.patch("graspologic.partition.leiden", side_effect=RuntimeError("force louvain")):
        r = run_structural_topology(di, louvain_seed=42)
    assert r.algorithm == "networkx_louvain"
    assert len(r.communities) >= 1


def test_cluster_split_oversized() -> None:
    """Force tiny max_fraction so a single community must split."""
    di = nx.DiGraph()
    for i in range(8):
        nid = f"f{i}"
        di.add_node(nid, node_type="file")
    ug = nx.complete_graph([f"f{i}" for i in range(8)])
    for u, v in ug.edges():
        ug.edges[u, v]["w"] = 1

    communities, _, algo, splits = cluster_structural_graph(
        ug,
        di,
        max_fraction=0.01,
        min_split_size=2,
    )
    assert splits >= 0
    assert len(communities) >= 1
    assert algo in ("graspologic_leiden", "networkx_louvain", "singleton_fallback")
