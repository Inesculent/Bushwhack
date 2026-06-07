from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from pydantic import BaseModel, Field

from src.config import Settings, get_settings
from src.domain.interfaces import IGitHubContextProvider
from src.domain.schemas import GitHubIssueContext, GitHubPullRequestContext, RepoDocument
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import trace_llm_call
from src.orchestration.prompts.exploration_prompts import render_docs_prebrief_prompt

logger = logging.getLogger(__name__)

ISSUE_URL_RE = re.compile(r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)")


class DocsPrebriefOutput(BaseModel):
    summary: str = Field(description="Concise documentation-based summary of the repository.")
    insights: List[str] = Field(default_factory=list, description="Actionable insights for reviewers.")


def make_docs_prebrief_node(
    *,
    github_provider: IGitHubContextProvider | None = None,
    settings: Settings | None = None,
):
    def docs_prebrief_node(state: GraphState) -> Dict[str, Any]:
        resolved_settings = settings or get_settings()
        meta = dict(state.get("metadata", {}) or {})
        if not resolved_settings.docs_prebrief_enabled:
            meta["docs_prebrief"] = {"status": "disabled"}
            return {"metadata": meta, "node_history": ["docs_prebrief:disabled"]}

        repo_identity = _resolve_repo_identity(state)
        if repo_identity is None:
            meta["docs_prebrief"] = {"status": "skipped_missing_repo"}
            return {"metadata": meta, "node_history": ["docs_prebrief:skipped"]}
        owner, repo = repo_identity

        pr_number = _resolve_pr_number(state)
        pr_context = _resolve_pr_context(state, github_provider, owner, repo, pr_number)
        default_branch = _resolve_default_branch(owner, repo, github_provider)
        ref = (pr_context.base_ref if pr_context and pr_context.base_ref else None) or default_branch or "main"

        docs, doc_warnings = _collect_docs(
            state=state,
            owner=owner,
            repo=repo,
            ref=ref,
            github_provider=github_provider,
            settings=resolved_settings,
        )
        if not docs and not pr_context:
            meta["docs_prebrief"] = {
                "status": "skipped_no_docs",
                "warnings": doc_warnings,
            }
            return {"metadata": meta, "node_history": ["docs_prebrief:skipped"]}

        issues = _resolve_linked_issues(pr_context, github_provider, owner, repo)
        comments = _resolve_pr_comments(pr_number, github_provider, owner, repo, resolved_settings)

        prompt = render_docs_prebrief_prompt(
            repo_path=f"{owner}/{repo}",
            docs=_format_docs(docs),
            pr_context=_format_pr_context(pr_context),
            issues=_format_issues(issues),
            comments=_format_comments(comments),
        )

        llm = Models.synthesizer(DocsPrebriefOutput, model_key=resolved_settings.docs_prebrief_model_key)
        traced = trace_llm_call(
            llm,
            prompt,
            state=state,
            node_name="docs_prebrief",
            model_key=resolved_settings.docs_prebrief_model_key,
            schema_name="DocsPrebriefOutput",
            input_summary={
                "repo": f"{owner}/{repo}",
                "doc_count": len(docs),
                "issue_count": len(issues),
                "comment_count": len(comments),
            },
        )
        invoke_result = traced.result
        response = parse_structured_output(invoke_result, DocsPrebriefOutput)
        tokens = traced.tokens

        summary = response.summary or ""
        insights = response.insights or ([summary] if summary else [])

        sources = [f"doc:{doc.path}" for doc in docs]
        if pr_context:
            sources.append(f"pr:{pr_context.number}")
        for issue in issues:
            sources.append(f"issue:{issue.number}")
        if comments:
            sources.append(f"pr_comments:{len(comments)}")
        repository_docs_summary = _repository_docs_summary(docs)
        repository_docs_sources = [f"doc:{doc.path}" for doc in docs]

        meta["docs_prebrief"] = {
            "status": "ok",
            "ref": ref,
            "sources": sources,
            "repository_docs_sources": repository_docs_sources,
            "warnings": doc_warnings,
        }

        return {
            "docs_prebrief_summary": summary,
            "docs_prebrief_sources": sources,
            "repository_docs_summary": repository_docs_summary,
            "repository_docs_sources": repository_docs_sources,
            "global_insights": insights,
            "metadata": meta,
            "node_history": ["docs_prebrief"],
            "token_usage": tokens,
            "llm_trace": traced.trace_records,
        }

    return docs_prebrief_node


def _resolve_repo_identity(state: GraphState) -> tuple[str, str] | None:
    metadata = state.get("metadata", {}) or {}
    candidates = [
        metadata.get("pr_repo"),
        metadata.get("review_repo_url"),
        state.get("repo_path"),
    ]
    for value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        slug = _parse_repo_slug(value.strip())
        if slug is not None:
            return slug
    return None


def _parse_repo_slug(value: str) -> tuple[str, str] | None:
    if "github.com" in value:
        parts = [p for p in value.split("github.com/")[-1].split("/") if p]
        if len(parts) >= 2:
            return parts[0], parts[1]
        return None
    if ":" in value or "\\" in value or value.startswith("/"):
        return None
    if "/" not in value:
        return None
    owner, repo = value.split("/", maxsplit=1)
    if not owner or not repo:
        return None
    return owner, repo


def _resolve_pr_number(state: GraphState) -> int | None:
    metadata = state.get("metadata", {}) or {}
    pr_number = metadata.get("review_pr_number") or metadata.get("pr_number")
    if isinstance(pr_number, int) and pr_number > 0:
        return pr_number
    return None


def _resolve_pr_context(
    state: GraphState,
    github_provider: IGitHubContextProvider | None,
    owner: str,
    repo: str,
    pr_number: int | None,
) -> GitHubPullRequestContext | None:
    metadata = state.get("metadata", {}) or {}
    title = metadata.get("pr_title") or ""
    body = metadata.get("pr_description") or ""
    if title or body:
        return GitHubPullRequestContext(
            number=pr_number or 0,
            title=str(title),
            body=str(body),
            html_url=metadata.get("pr_url"),
        )
    if github_provider is None or pr_number is None:
        return None
    return github_provider.get_pull_request(owner, repo, pr_number)


def _resolve_linked_issues(
    pr_context: GitHubPullRequestContext | None,
    github_provider: IGitHubContextProvider | None,
    owner: str,
    repo: str,
) -> List[GitHubIssueContext]:
    if pr_context is None or github_provider is None:
        return []
    matches = ISSUE_URL_RE.findall(pr_context.body or "")
    issue_numbers: List[int] = []
    for match in matches:
        if match[0] != owner or match[1] != repo:
            continue
        issue_numbers.append(int(match[2]))
    unique_numbers = list(dict.fromkeys(issue_numbers))[:5]
    issues: List[GitHubIssueContext] = []
    for number in unique_numbers:
        issue = github_provider.get_issue(owner, repo, number)
        if issue is not None:
            issues.append(issue)
    return issues


def _resolve_pr_comments(
    pr_number: int | None,
    github_provider: IGitHubContextProvider | None,
    owner: str,
    repo: str,
    settings: Settings,
) -> List[str]:
    if github_provider is None or pr_number is None:
        return []
    comments = github_provider.get_issue_comments(owner, repo, pr_number, settings.github_mcp_pr_max_comments)
    formatted: List[str] = []
    for comment in comments:
        body = comment.body or ""
        if not body:
            continue
        formatted.append(f"{comment.author or 'unknown'}: {body}")
    return formatted


def _resolve_default_branch(
    owner: str,
    repo: str,
    github_provider: IGitHubContextProvider | None,
) -> str | None:
    if github_provider is None:
        return None
    meta = github_provider.get_repo_metadata(owner, repo)
    if meta is None:
        return None
    branch = getattr(meta, "default_branch", None)
    if isinstance(branch, str) and branch.strip():
        return branch.strip()
    return None


def _merge_doc_paths(primary: Sequence[str], secondary: Sequence[str]) -> List[str]:
    seen: set[str] = set()
    merged: List[str] = []
    for raw in list(primary) + list(secondary):
        path = (raw or "").strip().lstrip("/")
        if not path or path in seen:
            continue
        seen.add(path)
        merged.append(path)
    return merged


def _discover_doc_paths(
    owner: str,
    repo: str,
    ref: str,
    github_provider: IGitHubContextProvider | None,
    settings: Settings,
) -> List[str]:
    if github_provider is None or not settings.github_mcp_doc_discovery_enabled:
        return []
    max_paths = settings.github_mcp_doc_discovery_max_paths
    if max_paths <= 0:
        return []

    paths: List[str] = []
    seen: set[str] = set()
    for seed in ("", "docs", ".github"):
        listing = github_provider.get_repo_structure(owner, repo, seed, ref)
        entries = listing.entries if hasattr(listing, "entries") else []
        for entry in entries:
            entry_path = (entry.path or "").strip().lstrip("/")
            if not entry_path or entry_path in seen:
                continue
            lower = entry_path.lower()
            if entry.type == "file" and lower.endswith((".md", ".rst", ".txt")):
                seen.add(entry_path)
                paths.append(entry_path)
                if len(paths) >= max_paths:
                    return paths
    return paths


def _collect_docs(
    *,
    state: GraphState,
    owner: str,
    repo: str,
    ref: str,
    github_provider: IGitHubContextProvider | None,
    settings: Settings,
) -> tuple[List[RepoDocument], List[str]]:
    repo_path = str(state.get("repo_path", "") or "")
    doc_paths = settings.github_mcp_doc_paths
    warnings: List[str] = []

    if repo_path and Path(repo_path).is_dir():
        docs = _collect_local_docs(
            Path(repo_path),
            doc_paths,
            settings.github_mcp_doc_max_chars,
            settings.github_mcp_doc_max_total_chars,
        )
        return docs, warnings

    if github_provider is None:
        warnings.append("github_docs_unavailable")
        return [], warnings

    discovered = _discover_doc_paths(owner, repo, ref, github_provider, settings)
    if discovered:
        warnings.append(f"docs_discovery_paths:{len(discovered)}")
        doc_paths = _merge_doc_paths(discovered, doc_paths)
    bundle = github_provider.get_repo_docs(owner, repo, ref, doc_paths)
    warnings.extend(bundle.warnings)
    return list(bundle.documents), warnings


def _collect_local_docs(
    repo_root: Path,
    paths: Sequence[str],
    max_chars_per_file: int,
    max_total_chars: int,
) -> List[RepoDocument]:
    docs: List[RepoDocument] = []
    total_chars = 0

    for raw in paths:
        rel = raw.strip().lstrip("/")
        if not rel:
            continue
        target = (repo_root / rel).resolve()
        try:
            target.relative_to(repo_root.resolve())
        except ValueError:
            continue
        if not target.is_file():
            continue
        content = target.read_text(encoding="utf-8", errors="replace")
        truncated = False
        if len(content) > max_chars_per_file:
            content = content[:max_chars_per_file]
            truncated = True
        total_chars += len(content)
        if total_chars > max_total_chars:
            break
        docs.append(RepoDocument(path=rel, ref=None, content=content, truncated=truncated))
    return docs


def _format_docs(docs: Iterable[RepoDocument]) -> str:
    blocks: List[str] = []
    for doc in docs:
        header = f"# {doc.path}"
        blocks.append(f"{header}\n{doc.content}")
    return "\n\n".join(blocks) or "(none)"


def _repository_docs_summary(docs: Sequence[RepoDocument]) -> str:
    """Build a PR-agnostic docs brief for repository-level KB distillation."""
    blocks: List[str] = []
    for doc in docs[:5]:
        lines = []
        for raw in doc.content.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#") or len(lines) < 6:
                lines.append(line[:240])
            if len(lines) >= 8:
                break
        if lines:
            blocks.append(f"{doc.path}: " + " ".join(lines))
    return "\n".join(blocks)[:4000]


def _format_pr_context(pr_context: GitHubPullRequestContext | None) -> str:
    if pr_context is None:
        return "(none)"
    return (
        f"Title: {pr_context.title}\n"
        f"Body:\n{pr_context.body or '(none)'}"
    )


def _format_issues(issues: Iterable[GitHubIssueContext]) -> str:
    blocks: List[str] = []
    for issue in issues:
        blocks.append(f"Issue #{issue.number}: {issue.title}\n{issue.body}")
    return "\n\n".join(blocks) or "(none)"


def _format_comments(comments: Iterable[str]) -> str:
    lines = list(comments)
    return "\n".join(lines) or "(none)"
