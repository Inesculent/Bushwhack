"""Tests for sandbox AST and single-file excerpt wiring in review context."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.domain.schemas import CodeEntity, ReviewTask
from src.orchestration.context.review_context import LazyReviewContextProvider


def _task_single_file() -> ReviewTask:
    return ReviewTask(
        id="t3-logic-combo",
        title="COMBO logic",
        description="validate combo modes",
        target_files=["comfy_extras/nodes_string.py"],
        subtasks=[],
        specialty="logic",
        depth=2,
        assigned_model="logic",
    )


def test_collect_for_task_single_file_uses_full_read_and_sandbox_ast() -> None:
    provider = LazyReviewContextProvider()
    provider._searcher = MagicMock()
    provider._sandbox = MagicMock()
    provider._host_repo_path = None
    provider._startup_warnings = []

    entity = CodeEntity(
        name="CaseConverter",
        type="class",
        signature="class CaseConverter():",
        body="class CaseConverter():\n    pass",
        dependencies=[],
    )

    state = {
        "repo_path": "https://github.com/comfyanonymous/ComfyUI",
        "metadata": {"review_repo_url": "https://github.com/comfyanonymous/ComfyUI"},
        "structural_graph_node_link": {},
    }

    with (
        patch.object(provider, "read_full_file", return_value="FULL_FILE" * 100) as read_full,
        patch.object(provider, "read_file_slice", return_value="SLICE") as read_slice,
        patch(
            "src.orchestration.context.review_context.collect_sandbox_file_entities",
            return_value={"files": {"comfy_extras/nodes_string.py": [entity.model_dump()]}, "gaps": []},
        ),
        patch.object(provider._searcher, "search_text", return_value=[]),
    ):
        ctx = provider.collect_for_task(state=state, task=_task_single_file())

    read_full.assert_called_once()
    read_slice.assert_not_called()
    assert ctx.per_file_snippet_max_chars >= 5000
    assert "ast_capability:sandbox_enabled" in ctx.warnings
    assert ctx.entities_by_file["comfy_extras/nodes_string.py"][0].name == "CaseConverter"
