"""Phase 2: collect_for_critique avoids generic repo search on logic tasks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.domain.schemas import ReviewTask
from src.domain.state import GraphState
from src.orchestration.context.review_context import LazyReviewContextProvider


def _state() -> GraphState:
    return {
        "run_id": "t",
        "repo_path": "/repo",
        "git_diff": "",
        "user_goals": "",
        "metadata": {},
    }


def test_collect_for_critique_logic_has_no_search_results() -> None:
    provider = LazyReviewContextProvider()
    task = ReviewTask(
        id="logic-1",
        title="Unicode",
        description="Check encoding",
        target_files=["pkg/foo.py"],
        specialty="logic",
    )
    with patch.object(provider, "_ensure_started"), patch.object(
        provider, "read_file_slice", return_value="def foo(): pass"
    ), patch.object(provider, "_searcher", MagicMock()) as searcher:
        provider._startup_warnings = []
        provider._host_repo_path = None
        provider._ast_parser = None
        provider._sandbox = None
        ctx = provider.collect_for_critique(_state(), task)
    assert ctx.search_results == {}
    searcher.search_text.assert_not_called()


def test_collect_for_critique_security_runs_search() -> None:
    provider = LazyReviewContextProvider()
    task = ReviewTask(
        id="sec-1",
        title="Auth",
        description="Check tokens",
        target_files=["auth.py"],
        specialty="security",
    )
    mock_searcher = MagicMock()
    mock_searcher.search_text.return_value = []
    with patch.object(provider, "_ensure_started"), patch.object(
        provider, "read_file_slice", return_value=""
    ):
        provider._startup_warnings = []
        provider._host_repo_path = None
        provider._ast_parser = None
        provider._sandbox = None
        provider._searcher = mock_searcher
        ctx = provider.collect_for_critique(_state(), task)
    assert len(ctx.search_results) >= 1
    mock_searcher.search_text.assert_called()
