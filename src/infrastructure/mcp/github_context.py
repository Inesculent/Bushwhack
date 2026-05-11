from __future__ import annotations

import logging
from typing import Iterable, List, Sequence

from src.config import Settings
from src.domain.interfaces import ICacheService, IGitHubContextProvider
from src.domain.schemas import (
    GitHubIssueComment,
    GitHubIssueContext,
    GitHubPullRequestContext,
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

        payload = self._client.call_tool(
            "get_issue_comments",
            {"owner": owner, "repo": repo, "issue_number": issue_number, "limit": capped},
        )
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


def _truncate(value: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    return value[:max_chars]
