from __future__ import annotations

import logging
from typing import Iterable, List, Sequence

from src.config import Settings
from src.domain.interfaces import ICacheService, IGitHubContextProvider
from src.domain.schemas import (
    GitHubFileReviewHistory,
    GitHubIssueComment,
    GitHubIssueContext,
    GitHubPullRequestContext,
    GitHubReviewHistoryComment,
    RepoDocument,
    RepoDocsBundle,
    RepoMetadata,
    RepoStructure,
    RepoStructureEntry,
)
from src.infrastructure.mcp.client import MCPClient

logger = logging.getLogger(__name__)


class GitHubMCPContextProvider(IGitHubContextProvider):
    """GitHub MCP-backed provider for docs and PR context."""

    def __init__(self, mcp_client: MCPClient, cache: ICacheService, settings: Settings) -> None:
        self._client = mcp_client
        self._cache = cache
        self._settings = settings
        self._available_tools: set[str] | None = None

    def get_repo_docs(
        self,
        owner: str,
        repo: str,
        ref: str,
        paths: Sequence[str],
    ) -> RepoDocsBundle:
        max_total = self._settings.github_mcp_doc_max_total_chars
        documents: List[RepoDocument] = []
        warnings: List[str] = []
        total_chars = 0

        for path in _normalize_paths(paths):
            doc, warning = self._get_repo_doc(owner, repo, ref, path)
            if warning:
                warnings.append(warning)
            if doc is None:
                continue
            total_chars += len(doc.content)
            if total_chars > max_total:
                warnings.append("docs_total_chars_limit_reached")
                break
            documents.append(doc)

        return RepoDocsBundle(
            repo=f"{owner}/{repo}",
            ref=ref,
            documents=documents,
            warnings=warnings,
        )

    def get_pull_request(
        self,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> GitHubPullRequestContext | None:
        cache_key = self._cache_key("pr", owner, repo, str(pull_number))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return GitHubPullRequestContext.model_validate(cached)

        payload = self._client.call_tool(
            "get_pull_request",
            {"owner": owner, "repo": repo, "pull_number": pull_number},
        )
        if payload.get("error"):
            logger.warning(
                "GitHub MCP pull request fetch failed %s/%s#%s: %s",
                owner,
                repo,
                pull_number,
                payload.get("error"),
            )
            return None

        context = GitHubPullRequestContext.model_validate(payload)
        self._cache.set(cache_key, context.model_dump(mode="json"), self._settings.github_mcp_cache_ttl_seconds)
        return context

    def get_repo_structure(
        self,
        owner: str,
        repo: str,
        path: str = "",
        ref: str = "",
    ) -> RepoStructure:
        payload = self._client.call_tool(
            "get_repo_structure",
            {"owner": owner, "repo": repo, "path": path, "ref": ref},
        )
        if payload.get("error"):
            return RepoStructure(
                owner=owner,
                repo=repo,
                path=path or "",
                ref=ref or None,
                entries=[],
                error=str(payload.get("error")),
            )
        raw_entries = payload.get("entries", [])
        entries: List[RepoStructureEntry] = []
        if isinstance(raw_entries, list):
            for item in raw_entries:
                if isinstance(item, dict):
                    entries.append(RepoStructureEntry.model_validate(item))
        return RepoStructure(
            owner=owner,
            repo=repo,
            path=payload.get("path") or path or "",
            ref=payload.get("ref") or (ref or None),
            entries=entries,
            error=payload.get("error"),
        )

    def get_repo_metadata(self, owner: str, repo: str) -> RepoMetadata | None:
        cache_key = self._cache_key("repo_meta", owner, repo)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return RepoMetadata.model_validate(cached)
        payload = self._client.call_tool(
            "get_repo_metadata",
            {"owner": owner, "repo": repo},
        )
        if payload.get("error"):
            logger.warning(
                "GitHub MCP repo metadata fetch failed %s/%s: %s",
                owner,
                repo,
                payload.get("error"),
            )
            return None
        meta = RepoMetadata.model_validate(payload)
        self._cache.set(cache_key, meta.model_dump(mode="json"), self._settings.github_mcp_cache_ttl_seconds)
        return meta

    def get_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
    ) -> GitHubIssueContext | None:
        cache_key = self._cache_key("issue", owner, repo, str(issue_number))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return GitHubIssueContext.model_validate(cached)

        payload = self._client.call_tool(
            "get_issue",
            {"owner": owner, "repo": repo, "issue_number": issue_number},
        )
        if payload.get("error"):
            logger.warning(
                "GitHub MCP issue fetch failed %s/%s#%s: %s",
                owner,
                repo,
                issue_number,
                payload.get("error"),
            )
            return None

        context = GitHubIssueContext.model_validate(payload)
        self._cache.set(cache_key, context.model_dump(mode="json"), self._settings.github_mcp_cache_ttl_seconds)
        return context

    def get_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        limit: int,
    ) -> List[GitHubIssueComment]:
        capped = max(0, min(limit, self._settings.github_mcp_pr_max_comments))
        if capped == 0:
            return []

        cache_key = self._cache_key("issue_comments", owner, repo, str(issue_number), str(capped))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return [GitHubIssueComment.model_validate(item) for item in cached.get("comments", [])]

        try:
            payload = self._client.call_tool(
                "get_issue_comments",
                {"owner": owner, "repo": repo, "issue_number": issue_number, "limit": capped},
            )
        except Exception as exc:  # noqa: BLE001 - optional GitHub context
            logger.warning(
                "GitHub MCP issue comments fetch failed %s/%s#%s: %s: %s",
                owner,
                repo,
                issue_number,
                exc.__class__.__name__,
                exc,
            )
            return []
        if payload.get("error"):
            logger.warning(
                "GitHub MCP issue comments fetch failed %s/%s#%s: %s",
                owner,
                repo,
                issue_number,
                payload.get("error"),
            )
            return []

        comments_raw = payload.get("comments", [])
        comments: List[GitHubIssueComment] = []
        for item in comments_raw:
            comment = GitHubIssueComment.model_validate(item)
            comment.body = _truncate(comment.body, self._settings.github_mcp_pr_comment_max_chars)
            comments.append(comment)

        self._cache.set(
            cache_key,
            {"comments": [c.model_dump(mode="json") for c in comments]},
            self._settings.github_mcp_cache_ttl_seconds,
        )
        return comments

    def get_file_review_history(
        self,
        owner: str,
        repo: str,
        ref: str,
        paths: Sequence[str],
        *,
        current_pr_number: int | None = None,
        commits_per_file: int = 12,
        prs_per_file: int = 3,
        comments_per_pr: int = 30,
        max_total_chars: int = 8000,
    ) -> List[GitHubFileReviewHistory]:
        cache_key = self._cache_key(
            "file_review_history",
            owner,
            repo,
            ref,
            ",".join(_normalize_paths(paths)),
            str(current_pr_number or ""),
            str(commits_per_file),
            str(prs_per_file),
            str(comments_per_pr),
            str(max_total_chars),
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return [
                GitHubFileReviewHistory.model_validate(item)
                for item in cached.get("history", [])
            ]

        remaining_chars = max(0, int(max_total_chars))
        histories: List[GitHubFileReviewHistory] = []
        seen_pr_by_path: dict[str, set[int]] = {}

        for path in _normalize_paths(paths):
            warnings: List[str] = []
            comments: List[GitHubReviewHistoryComment] = []
            seen_pr_by_path[path] = set()
            if not self._has_tool("get_commits_for_path"):
                warnings.append("commits_fetch_failed:missing_mcp_tool:get_commits_for_path")
                histories.append(GitHubFileReviewHistory(file_path=path, warnings=warnings))
                continue
            try:
                commits = self._client.call_tool(
                    "get_commits_for_path",
                    {
                        "owner": owner,
                        "repo": repo,
                        "path": path,
                        "ref": ref,
                        "limit": max(1, int(commits_per_file)),
                    },
                )
            except Exception as exc:  # noqa: BLE001 - optional history enrichment
                warnings.append(f"commits_fetch_failed:{exc.__class__.__name__}: {exc}")
                histories.append(GitHubFileReviewHistory(file_path=path, warnings=warnings))
                continue
            if commits.get("error"):
                warnings.append(f"commits_fetch_failed:{commits.get('error')}")
                histories.append(GitHubFileReviewHistory(file_path=path, warnings=warnings))
                continue

            for commit in list(commits.get("commits") or [])[: max(1, int(commits_per_file))]:
                if remaining_chars <= 0:
                    warnings.append("review_history_total_chars_limit_reached")
                    break
                if len(seen_pr_by_path[path]) >= max(1, int(prs_per_file)):
                    break
                sha = str(commit.get("sha") or "")
                if not sha:
                    continue
                try:
                    prs = self._client.call_tool(
                        "get_pull_requests_for_commit",
                        {"owner": owner, "repo": repo, "commit_sha": sha},
                    )
                except Exception as exc:  # noqa: BLE001 - optional history enrichment
                    warnings.append(f"commit_prs_fetch_failed:{sha}:{exc.__class__.__name__}: {exc}")
                    continue
                if prs.get("error"):
                    warnings.append(f"commit_prs_fetch_failed:{sha}:{prs.get('error')}")
                    continue
                for pr in prs.get("pull_requests") or []:
                    if remaining_chars <= 0:
                        break
                    number = _as_positive_int(pr.get("number"))
                    if number is None:
                        continue
                    if current_pr_number is not None and number == current_pr_number:
                        continue
                    if number in seen_pr_by_path[path]:
                        continue
                    if str(pr.get("state") or "").lower() != "closed" or not pr.get("merged_at"):
                        continue
                    seen_pr_by_path[path].add(number)
                    added, remaining_chars = self._history_comments_for_pr(
                        owner=owner,
                        repo=repo,
                        path=path,
                        pr=pr,
                        commit_sha=sha,
                        comments_per_pr=comments_per_pr,
                        remaining_chars=remaining_chars,
                    )
                    comments.extend(added)
                    if len(seen_pr_by_path[path]) >= max(1, int(prs_per_file)):
                        break

            histories.append(
                GitHubFileReviewHistory(
                    file_path=path,
                    comments=comments,
                    warnings=warnings,
                )
            )

        self._cache.set(
            cache_key,
            {"history": [h.model_dump(mode="json") for h in histories]},
            self._settings.github_mcp_cache_ttl_seconds,
        )
        return histories

    def _history_comments_for_pr(
        self,
        *,
        owner: str,
        repo: str,
        path: str,
        pr: dict,
        commit_sha: str,
        comments_per_pr: int,
        remaining_chars: int,
    ) -> tuple[List[GitHubReviewHistoryComment], int]:
        if comments_per_pr <= 0:
            return [], remaining_chars
        number = int(pr["number"])
        try:
            review_payload = self._client.call_tool(
                "get_pull_request_review_comments",
                {
                    "owner": owner,
                    "repo": repo,
                    "pull_number": number,
                    "limit": max(0, int(comments_per_pr)),
                },
            )
        except Exception:
            review_payload = {"comments": []}
        review_comments = []
        if not review_payload.get("error"):
            review_comments = [
                raw
                for raw in review_payload.get("comments") or []
                if _same_path(str(raw.get("path") or ""), path)
            ]

        out: List[GitHubReviewHistoryComment] = []
        for raw in review_comments:
            body = _truncate(str(raw.get("body") or ""), self._settings.github_mcp_pr_comment_max_chars)
            if not body.strip():
                continue
            if remaining_chars <= 0:
                break
            clipped = body[:remaining_chars]
            remaining_chars -= len(clipped)
            out.append(
                GitHubReviewHistoryComment(
                    file_path=path,
                    pr_number=number,
                    pr_title=str(pr.get("title") or ""),
                    pr_html_url=pr.get("html_url"),
                    commit_sha=commit_sha,
                    author=raw.get("author"),
                    created_at=raw.get("created_at"),
                    body=clipped,
                    comment_path=str(raw.get("path") or ""),
                    line=_as_positive_int(raw.get("line")),
                    source="review_comment",
                )
            )

        issue_budget = max(0, int(comments_per_pr) - len(review_comments))
        if issue_budget <= 0 or remaining_chars <= 0:
            return out, remaining_chars
        issue_comments = self.get_issue_comments(owner, repo, number, issue_budget)
        for comment in issue_comments[:issue_budget]:
            body = _truncate(comment.body, self._settings.github_mcp_pr_comment_max_chars)
            if not body.strip():
                continue
            if remaining_chars <= 0:
                break
            clipped = body[:remaining_chars]
            remaining_chars -= len(clipped)
            out.append(
                GitHubReviewHistoryComment(
                    file_path=path,
                    pr_number=number,
                    pr_title=str(pr.get("title") or ""),
                    pr_html_url=pr.get("html_url"),
                    commit_sha=commit_sha,
                    author=comment.author,
                    created_at=comment.created_at,
                    body=clipped,
                    comment_path="",
                    line=None,
                    source="issue_comment",
                )
            )
        return out, remaining_chars

    def _get_repo_doc(
        self,
        owner: str,
        repo: str,
        ref: str,
        path: str,
    ) -> tuple[RepoDocument | None, str | None]:
        cache_key = self._cache_key("doc", owner, repo, ref, path)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return RepoDocument.model_validate(cached), None

        payload = self._client.call_tool(
            "get_file_content",
            {"owner": owner, "repo": repo, "path": path, "ref": ref},
        )
        if payload.get("error"):
            return None, f"doc_fetch_failed:{path}:{payload.get('error')}"

        content = payload.get("content") or ""
        truncated = False
        max_chars = self._settings.github_mcp_doc_max_chars
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = True

        doc = RepoDocument(path=path, ref=ref, content=content, truncated=truncated)
        self._cache.set(cache_key, doc.model_dump(mode="json"), self._settings.github_mcp_cache_ttl_seconds)
        return doc, None

    @staticmethod
    def _cache_key(*parts: str) -> str:
        sanitized = [p.strip().replace(" ", "-") for p in parts if p]
        return "github_mcp:" + ":".join(sanitized)

    def _has_tool(self, name: str) -> bool:
        list_tools = getattr(self._client, "list_tools", None)
        if not callable(list_tools):
            return True
        if self._available_tools is None:
            try:
                self._available_tools = set(list_tools())
            except Exception as exc:  # noqa: BLE001 - provider should fail open
                logger.warning("GitHub MCP list_tools failed: %s: %s", exc.__class__.__name__, exc)
                self._available_tools = set()
                return True
        return name in self._available_tools


def _normalize_paths(paths: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in paths:
        normalized = (raw or "").strip().lstrip("/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _same_path(left: str, right: str) -> bool:
    return left.strip().replace("\\", "/").lstrip("/") == right.strip().replace("\\", "/").lstrip("/")


def _as_positive_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _truncate(value: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    return value[:max_chars]
