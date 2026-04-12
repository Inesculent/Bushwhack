from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from src.infrastructure import remote_review_workflow as workflow


def test_draw_ast_summary_graph_writes_png_and_uses_nx_draw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("matplotlib")

    ast_summary = [
        {
            "file": "src/demo.py",
            "total_top_level_nodes": 2,
            "top_level_nodes": ["FunctionDef", "ClassDef"],
            "top_level_symbols": [
                {"name": "demo_fn", "type": "FunctionDef"},
                {"name": "DemoClass", "type": "ClassDef"},
            ],
        }
    ]

    draw_called = {"value": False}
    original_draw = workflow.nx.draw

    def _wrapped_draw(*args, **kwargs):
        draw_called["value"] = True
        return original_draw(*args, **kwargs)

    monkeypatch.setattr(workflow.nx, "draw", _wrapped_draw)

    output_path = tmp_path / "ast_graph.png"
    rendered = workflow.draw_ast_summary_graph(ast_summary=ast_summary, output_path=str(output_path))

    assert draw_called["value"] is True
    assert rendered == str(output_path)
    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_build_ast_summary_graph_creates_expected_nodes_and_edges() -> None:
    graph = workflow.build_ast_summary_graph(
        [
            {
                "file": "src/demo.py",
                "top_level_nodes": ["FunctionDef", "ClassDef"],
            },
            {
                "file": "src/err.py",
                "error": "SyntaxError: invalid syntax",
            },
        ]
    )

    file_nodes = [node for node, attrs in graph.nodes(data=True) if attrs.get("node_type") == "file"]
    symbol_nodes = [node for node, attrs in graph.nodes(data=True) if attrs.get("node_type") == "symbol"]
    error_nodes = [node for node, attrs in graph.nodes(data=True) if attrs.get("node_type") == "error"]

    assert len(file_nodes) == 2
    assert len(symbol_nodes) == 2
    assert len(error_nodes) == 1
    assert graph.number_of_edges() == 3

    for _, _, attrs in graph.edges(data=True):
        assert attrs["relation"] == attrs["edge_type"]
        assert attrs["provenance"] == "EXTRACTED"
        assert attrs["confidence"] == "EXTRACTED"
        assert attrs["source_ref"] == attrs["source_file"]
        assert attrs["source_location"].startswith("L")
        assert attrs["weight"] == 1.0


def test_build_ast_summary_graph_resolves_file_and_symbol_import_dependencies() -> None:
    graph = workflow.build_ast_summary_graph(
        [
            {
                "file": "module_a.py",
                "top_level_symbols": [{"name": "alpha_fn", "type": "FunctionDef"}],
                "imports": [],
            },
            {
                "file": "module_b.py",
                "top_level_symbols": [{"name": "beta_fn", "type": "FunctionDef"}],
                "imports": [
                    {"type": "from", "module": "module_a", "names": ["alpha_fn"], "level": 0, "line": 7}
                ],
            },
            {
                "file": "module_c.py",
                "top_level_symbols": [{"name": "gamma_fn", "type": "FunctionDef"}],
                "imports": [
                    {"type": "from", "module": "module_b", "names": ["beta_fn"], "level": 0, "line": 11}
                ],
            },
        ]
    )

    file_edges = [
        (src, dst, attrs)
        for src, dst, attrs in graph.edges(data=True)
        if attrs.get("edge_type") == "depends_on_file"
    ]
    symbol_edges = [
        (src, dst, attrs)
        for src, dst, attrs in graph.edges(data=True)
        if attrs.get("edge_type") == "imports_symbol"
    ]

    file_edge_pairs = {(src, dst) for src, dst, _ in file_edges}
    assert ("file:module_b.py", "file:module_a.py") in file_edge_pairs
    assert ("file:module_c.py", "file:module_b.py") in file_edge_pairs

    symbol_targets = {dst for _, dst, _ in symbol_edges}
    assert any(dst.endswith(":alpha_fn") for dst in symbol_targets)
    assert any(dst.endswith(":beta_fn") for dst in symbol_targets)

    assert any(attrs.get("source_location") == "L7" for _, _, attrs in file_edges)


def test_main_includes_graph_image_path_when_flag_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _FakeResult:
        ast_summary = [{"file": "src/demo.py", "top_level_nodes": ["FunctionDef"]}]
        manifest = None
        structural_entities: dict = {}

        @staticmethod
        def as_dict() -> dict:
            return {"repo_url": "demo", "head_commit": "abc"}

    monkeypatch.setattr(
        workflow,
        "_parse_args",
        lambda: SimpleNamespace(
            repo_url="https://example.com/repo.git",
            head_commit="abc",
            base_commit="",
            ast_scope="repository",
            max_ast_files=0,
            ast_dump_output="",
            ast_dump_max_chars=0,
            graph_output="plots/ast_graph.png",
            graph_title="AST Graph",
            structural_graph_json="",
            structural_topology_json="",
            topology_graph_output="",
            topology_graph_title="Structural topology",
            community_max_fraction=None,
            community_min_split_size=None,
            community_max_files=None,
            community_max_symbols=None,
        ),
    )
    monkeypatch.setattr(workflow, "run_remote_review_workflow", lambda **kwargs: _FakeResult())
    monkeypatch.setattr(
        workflow,
        "draw_ast_summary_graph",
        lambda ast_summary, output_path, title: output_path,
    )

    workflow.main()

    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert payload["graph_image_path"] == "plots/ast_graph.png"


def test_main_includes_ast_dump_path_when_flag_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _FakeResult:
        ast_summary = [{"file": "src/demo.py", "top_level_nodes": ["FunctionDef"]}]
        ast_dump = [{"file": "src/demo.py", "formatted_ast": "Module(...)"}]
        manifest = None
        structural_entities: dict = {}

        @staticmethod
        def as_dict() -> dict:
            return {
                "repo_url": "demo",
                "head_commit": "abc",
                "ast_dump": [{"file": "src/demo.py", "formatted_ast": "Module(...)"}],
            }

    monkeypatch.setattr(
        workflow,
        "_parse_args",
        lambda: SimpleNamespace(
            repo_url="https://example.com/repo.git",
            head_commit="abc",
            base_commit="",
            ast_scope="repository",
            max_ast_files=0,
            ast_dump_output="artifacts/ast_dump.json",
            ast_dump_max_chars=0,
            graph_output="",
            graph_title="AST Graph",
            structural_graph_json="",
            structural_topology_json="",
            topology_graph_output="",
            topology_graph_title="Structural topology",
            community_max_fraction=None,
            community_min_split_size=None,
            community_max_files=None,
            community_max_symbols=None,
        ),
    )
    monkeypatch.setattr(workflow, "run_remote_review_workflow", lambda **kwargs: _FakeResult())
    monkeypatch.setattr(
        workflow,
        "write_ast_dump_file",
        lambda ast_dump, output_path: output_path,
    )

    workflow.main()

    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert payload["ast_dump_path"] == "artifacts/ast_dump.json"
    assert payload["ast_dump_file_count"] == 1
    assert "ast_dump" not in payload
