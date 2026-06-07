"""Tests for focused-context file path scoping."""

from __future__ import annotations

from src.domain.schemas import FocusedContextRequest
from src.domain.state import GraphState
from src.orchestration.context.focus_request_scope import (
    allowed_review_paths,
    clamp_focused_context_request,
)


def test_allowed_review_paths_from_diff() -> None:
    state: GraphState = {
        "git_diff": "diff --git a/comfy_extras/nodes_string.py b/comfy_extras/nodes_string.py\n+++ b/comfy_extras/nodes_string.py\n",
    }
    allowed = allowed_review_paths(state)
    assert "comfy_extras/nodes_string.py" in allowed


def test_clamp_drops_out_of_scope_paths() -> None:
    allowed = frozenset({"comfy_extras/nodes_string.py", "nodes.py"})
    req = FocusedContextRequest(
        request_id="req-1",
        candidate_id="c1",
        requested_by_specialty="security",
        file_paths=["main.py", "comfy_extras/nodes_string.py", "execution.py"],
        text_queries=["timeout"],
    )
    out = clamp_focused_context_request(req, allowed, fallback_path="comfy_extras/nodes_string.py")
    assert out.file_paths == ["comfy_extras/nodes_string.py"]
