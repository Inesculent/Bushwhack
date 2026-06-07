"""Tests for structural graph call-edge summaries in review context."""

from __future__ import annotations

from src.orchestration.context.review_context import symbol_call_edges_for_file


def test_symbol_call_edges_for_file_maps_calls() -> None:
    state = {
        "structural_graph_node_link": {
            "nodes": [
                {"id": "file:mod.py", "node_type": "file", "file_path": "mod.py"},
                {
                    "id": "sym:Foo",
                    "node_type": "symbol",
                    "file_path": "mod.py",
                    "symbol_name": "Foo",
                },
                {
                    "id": "sym:bar",
                    "node_type": "symbol",
                    "file_path": "other.py",
                    "symbol_name": "bar",
                },
            ],
            "edges": [
                {"source": "sym:Foo", "target": "sym:bar", "edge_type": "calls"},
            ],
        }
    }
    edges = symbol_call_edges_for_file(state, "mod.py")
    assert edges.get("Foo") == ["bar"]
