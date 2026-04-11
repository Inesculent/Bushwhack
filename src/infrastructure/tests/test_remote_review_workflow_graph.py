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


def test_main_includes_graph_image_path_when_flag_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _FakeResult:
        ast_summary = [{"file": "src/demo.py", "top_level_nodes": ["FunctionDef"]}]

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
            graph_output="plots/ast_graph.png",
            graph_title="AST Graph",
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
