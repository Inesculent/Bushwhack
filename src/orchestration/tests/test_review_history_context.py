from __future__ import annotations

from src.config import Settings
from src.domain.schemas import (
    GitHubFileReviewHistory,
    GitHubReviewHistoryComment,
    RepoMetadata,
)
from src.domain.state import GraphState
from src.orchestration.context.mandate_loop_context import format_delta_ledger_for_patch
from src.orchestration.nodes.review_history_context import make_review_history_context_node


class _Provider:
    def get_repo_metadata(self, owner: str, repo: str) -> RepoMetadata:
        return RepoMetadata(owner=owner, repo=repo, default_branch="main")

    def get_file_review_history(self, owner: str, repo: str, ref: str, paths, **kwargs):
        return [
            GitHubFileReviewHistory(
                file_path="src/widget.py",
                comments=[
                    GitHubReviewHistoryComment(
                        file_path="src/widget.py",
                        pr_number=12,
                        pr_title="Preserve widget fallbacks",
                        commit_sha="abc123",
                        author="alice",
                        body="Keep missing widget names non-fatal; callers depend on the fallback path.",
                        comment_path="src/widget.py",
                        line=42,
                    )
                ],
            )
        ]


class _MissingHistoryToolProvider(_Provider):
    def get_file_review_history(self, owner: str, repo: str, ref: str, paths, **kwargs):
        return [
            GitHubFileReviewHistory(
                file_path="src/widget.py",
                warnings=["commits_fetch_failed:missing_mcp_tool:get_commits_for_path"],
            )
        ]


def _state() -> GraphState:
    return {  # type: ignore[assignment]
        "run_id": "r1",
        "repo_path": "https://github.com/owner/repo",
        "git_diff": (
            "diff --git a/src/widget.py b/src/widget.py\n"
            "--- a/src/widget.py\n"
            "+++ b/src/widget.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
        "metadata": {
            "pr_repo": "owner/repo",
            "review_pr_number": 99,
            "docs_prebrief": {"ref": "main"},
        },
        "exploration_ledger": [],
    }


def test_review_history_context_adds_mandate_observation() -> None:
    node = make_review_history_context_node(
        github_provider=_Provider(),  # type: ignore[arg-type]
        settings=Settings(github_mcp_review_history_max_total_chars=1000),
    )

    out = node(_state())

    assert out["node_history"] == ["review_history_context"]
    assert out["metadata"]["review_history_context"]["status"] == "ok"
    assert out["metadata"]["review_history_context"]["comment_count"] == 1
    ledger = out["exploration_ledger"]
    assert ledger[0]["kind"] == "mandate_tool_observation"
    assert ledger[0]["tool"] == "review_history_context"
    assert "non-fatal" in ledger[0]["result_preview"]


def test_review_history_context_records_skip_reason_without_provider() -> None:
    node = make_review_history_context_node(
        github_provider=None,
        settings=Settings(),
    )

    out = node(_state())

    assert out["node_history"] == ["review_history_context:skipped"]
    assert out["metadata"]["review_history_context"]["skip_reason"] == "no_github_provider"


def test_review_history_context_marks_missing_mcp_tool_degraded() -> None:
    node = make_review_history_context_node(
        github_provider=_MissingHistoryToolProvider(),  # type: ignore[arg-type]
        settings=Settings(github_mcp_review_history_max_total_chars=1000),
    )

    out = node(_state())

    slot = out["metadata"]["review_history_context"]
    assert slot["status"] == "degraded"
    assert slot["mcp_degraded"] is True
    assert out["node_history"] == ["review_history_context"]


def test_review_history_context_reaches_mandate_patch_delta_prompt() -> None:
    node = make_review_history_context_node(
        github_provider=_Provider(),  # type: ignore[arg-type]
        settings=Settings(github_mcp_review_history_max_total_chars=1000),
    )
    state = _state()
    out = node(state)
    merged: GraphState = {
        **state,
        "metadata": out["metadata"],
        "exploration_ledger": out["exploration_ledger"],
    }  # type: ignore[assignment]

    delta = format_delta_ledger_for_patch(merged, max_entries=4, max_chars=2000)

    assert "review_history_context" in delta
    assert "institutional memory" in delta
    assert "non-fatal" in delta
