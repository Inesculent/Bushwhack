"""Tests for full-file focused context fulfillment."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.domain.schemas import FocusedContextRequest
from src.domain.state import GraphState
from src.orchestration.context.review_context import BoundedReviewContextFulfiller


def test_bounded_fulfiller_full_mode_uses_read_full_file() -> None:
    provider = MagicMock()
    provider.read_full_file.return_value = "ENTIRE_FILE\n"
    provider.ast_entities_for_file.return_value = ([], [])
    provider.search_bounded.return_value = []

    fulfiller = BoundedReviewContextFulfiller(provider)
    state: GraphState = {"run_id": "r1", "repo_path": "/tmp/repo", "metadata": {}}
    req = FocusedContextRequest(
        request_id="q1",
        candidate_id="c1",
        requested_by_specialty="logic",
        file_read_mode="full",
        file_paths=["nodes_string.py"],
    )
    result = fulfiller.fulfill(state, req)

    provider.read_full_file.assert_called_once()
    assert result.file_contents_full["nodes_string.py"] == "ENTIRE_FILE\n"
    provider.read_file_slice.assert_not_called()
