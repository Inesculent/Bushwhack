"""Tests that focused context does not spam GitHub MCP for default doc paths."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.domain.schemas import FocusedContextRequest, RepoDocsBundle
from src.orchestration.context.review_context import BoundedReviewContextFulfiller


def test_github_fallback_skips_doc_paths_when_setting_disabled() -> None:
    class StubProvider:
        def _ensure_started(self, state: dict) -> None:
            return None

        def read_file_slice(self, file_path: str, *, max_chars: int = 20000) -> str:
            return "class Foo: pass"

        def read_full_file(self, file_path: str, *, max_chars: int) -> str:
            return "class Foo: pass"

        def search_bounded(self, query: str, *, max_hits: int, file_paths=None):
            return []

        def ast_entities_for_file(self, file_path: str, **kwargs):
            return [], []

    github = MagicMock()
    github.get_repo_docs.return_value = RepoDocsBundle(
        repo="o/r",
        ref="main",
        documents=[],
        warnings=[],
    )

    fulfiller = BoundedReviewContextFulfiller(StubProvider(), github_provider=github)  # type: ignore[arg-type]
    req = FocusedContextRequest(
        request_id="r1",
        candidate_id="c1",
        requested_by_specialty="logic",
        file_paths=["comfy_extras/nodes_string.py"],
        symbol_queries=["StringCompare"],
        text_queries=["mode"],
    )
    state = {
        "run_id": "t",
        "repo_path": "https://github.com/o/r",
        "metadata": {"pr_repo": "o/r", "review_repo_url": "https://github.com/o/r"},
    }
    result = fulfiller.fulfill(state, req)  # type: ignore[arg-type]

    github.get_repo_docs.assert_not_called()
    assert "sandbox_search_no_hits:github_doc_fallback_disabled" in result.warnings
