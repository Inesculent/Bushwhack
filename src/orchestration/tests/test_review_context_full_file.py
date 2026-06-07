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


def test_bounded_fulfiller_uses_review_check_line_window_without_candidate() -> None:
    provider = MagicMock()
    provider.read_file_window.return_value = "class StringCompare:\n    def execute(self):\n        return False\n"
    provider.read_file_slice.return_value = "prefix only"
    provider.ast_entities_for_file.return_value = ([], [])
    provider.search_bounded.return_value = []

    fulfiller = BoundedReviewContextFulfiller(provider)
    state: GraphState = {
        "run_id": "r1",
        "repo_path": "/tmp/repo",
        "metadata": {
            "review_checks": {
                "by_task": {
                    "logic": {
                        "compiled_checks": [
                            {
                                "check_id": "logic:compare",
                                "file_path": "nodes_string.py",
                                "line_start": 159,
                                "line_end": 189,
                            }
                        ]
                    }
                }
            }
        },
        "candidate_findings": [],
    }
    req = FocusedContextRequest(
        request_id="check:logic:compare:1",
        candidate_id="logic:compare",
        requested_by_specialty="logic",
        file_paths=["nodes_string.py"],
    )

    result = fulfiller.fulfill(state, req)

    provider.read_file_window.assert_called_once_with(
        "nodes_string.py",
        line_start=159,
        line_end=189,
        max_chars=16000,
    )
    assert "StringCompare" in result.file_snippets["nodes_string.py"]
    provider.read_file_slice.assert_not_called()
