from __future__ import annotations

from src.config import Settings
from src.infrastructure.cache.memory_cache import InMemoryCache
from src.infrastructure.mcp.client import MCPToolError
from src.infrastructure.mcp.github_context import GitHubMCPContextProvider


class _DummyMCPClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, args: dict) -> dict:
        self.calls.append((name, args))
        if name == "get_commits_for_path":
            return {
                "commits": [
                    {"sha": "aaa111"},
                    {"sha": "bbb222"},
                    {"sha": "ccc333"},
                ]
            }
        if name == "get_pull_requests_for_commit":
            sha = args["commit_sha"]
            if sha == "aaa111":
                return {
                    "pull_requests": [
                        {
                            "number": 7,
                            "title": "current pr",
                            "html_url": "https://example/pr/7",
                            "state": "closed",
                            "merged_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                }
            if sha == "bbb222":
                return {
                    "pull_requests": [
                        {
                            "number": 8,
                            "title": "tighten widget contract",
                            "html_url": "https://example/pr/8",
                            "state": "closed",
                            "merged_at": "2026-01-02T00:00:00Z",
                        }
                    ]
                }
            return {
                "pull_requests": [
                    {
                        "number": 8,
                        "title": "tighten widget contract",
                        "html_url": "https://example/pr/8",
                        "state": "closed",
                        "merged_at": "2026-01-02T00:00:00Z",
                    }
                ]
            }
        if name == "get_pull_request_review_comments":
            return {
                "comments": [
                    {
                        "author": "alice",
                        "body": "Please preserve the fallback branch for empty widget names.",
                        "path": "src/widget.py",
                        "line": 12,
                        "created_at": "2026-01-02T00:00:00Z",
                    },
                    {
                        "author": "bob",
                        "body": "Unrelated file note.",
                        "path": "src/other.py",
                        "line": 3,
                    },
                ]
            }
        if name == "get_issue_comments":
            return {
                "comments": [
                    {
                        "author": "carol",
                        "body": "This module usually treats missing names as non-fatal.",
                        "created_at": "2026-01-02T00:00:00Z",
                    }
                ]
            }
        raise AssertionError(f"unexpected tool: {name}")


def test_file_review_history_dedupes_current_pr_and_filters_paths() -> None:
    client = _DummyMCPClient()
    provider = GitHubMCPContextProvider(
        mcp_client=client,  # type: ignore[arg-type]
        cache=InMemoryCache(),
        settings=Settings(github_mcp_pr_comment_max_chars=500),
    )

    histories = provider.get_file_review_history(
        "owner",
        "repo",
        "main",
        ["src/widget.py"],
        current_pr_number=7,
        commits_per_file=3,
        prs_per_file=3,
        comments_per_pr=4,
        max_total_chars=2000,
    )

    assert len(histories) == 1
    comments = histories[0].comments
    assert [c.pr_number for c in comments] == [8, 8]
    assert comments[0].source == "review_comment"
    assert comments[0].comment_path == "src/widget.py"
    assert "fallback branch" in comments[0].body
    assert comments[1].source == "issue_comment"
    assert "non-fatal" in comments[1].body

    pr_lookup_calls = [
        call for call in client.calls if call[0] == "get_pull_requests_for_commit"
    ]
    assert [call[1]["commit_sha"] for call in pr_lookup_calls] == ["aaa111", "bbb222", "ccc333"]


def test_file_review_history_fails_soft_when_commits_tool_missing() -> None:
    class MissingCommitsClient:
        def call_tool(self, name: str, args: dict) -> dict:
            if name == "get_commits_for_path":
                raise MCPToolError("MCP tool 'get_commits_for_path' returned an error: Unknown tool")
            raise AssertionError(f"unexpected tool: {name}")

    provider = GitHubMCPContextProvider(
        mcp_client=MissingCommitsClient(),  # type: ignore[arg-type]
        cache=InMemoryCache(),
        settings=Settings(),
    )

    histories = provider.get_file_review_history(
        "owner",
        "repo",
        "main",
        ["src/widget.py"],
        current_pr_number=7,
    )

    assert len(histories) == 1
    assert histories[0].file_path == "src/widget.py"
    assert histories[0].comments == []
    assert histories[0].warnings
    assert "commits_fetch_failed:MCPToolError" in histories[0].warnings[0]


def test_file_review_history_skips_unknown_commits_tool_before_calling() -> None:
    class MissingListedToolClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def list_tools(self) -> list[str]:
            return ["get_pull_request", "get_issue_comments"]

        def call_tool(self, name: str, args: dict) -> dict:
            self.calls.append(name)
            raise AssertionError(f"unexpected tool call: {name}")

    client = MissingListedToolClient()
    provider = GitHubMCPContextProvider(
        mcp_client=client,  # type: ignore[arg-type]
        cache=InMemoryCache(),
        settings=Settings(),
    )

    histories = provider.get_file_review_history(
        "owner",
        "repo",
        "main",
        ["src/widget.py"],
    )

    assert histories[0].comments == []
    assert histories[0].warnings == ["commits_fetch_failed:missing_mcp_tool:get_commits_for_path"]
    assert client.calls == []
