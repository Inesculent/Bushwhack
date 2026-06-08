"""Bounded prior PR review-history context for mental-model mandate planning."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Sequence
from urllib.parse import urlparse

from src.config import Settings, get_settings
from src.domain.interfaces import IGitHubContextProvider
from src.domain.schemas import GitHubFileReviewHistory, GitHubReviewHistoryComment
from src.domain.state import GraphState
from src.orchestration.nodes.application.planner import _target_files

logger = logging.getLogger(__name__)


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
        parsed = urlparse(value)
        parts = [p for p in parsed.path.split("/") if p]
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


def _resolve_current_pr_number(state: GraphState) -> int | None:
    metadata = state.get("metadata", {}) or {}
    value = metadata.get("review_pr_number") or metadata.get("pr_number")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _resolve_history_ref(
    state: GraphState,
    provider: IGitHubContextProvider,
    owner: str,
    repo: str,
) -> str:
    metadata = state.get("metadata", {}) or {}
    docs_meta = metadata.get("docs_prebrief", {}) if isinstance(metadata, dict) else {}
    if isinstance(docs_meta, dict):
        ref = docs_meta.get("ref")
        if isinstance(ref, str) and ref.strip():
            return ref.strip()
    try:
        repo_meta = provider.get_repo_metadata(owner, repo)
    except Exception as exc:  # noqa: BLE001
        logger.debug("review_history default branch lookup skipped: %s", exc)
        repo_meta = None
    branch = getattr(repo_meta, "default_branch", None) if repo_meta is not None else None
    return branch.strip() if isinstance(branch, str) and branch.strip() else "main"


def _truncate_line(text: str, max_chars: int) -> str:
    squashed = " ".join((text or "").split())
    if len(squashed) <= max_chars:
        return squashed
    return squashed[: max_chars - 3].rstrip() + "..."


def _comment_line(comment: GitHubReviewHistoryComment) -> str:
    loc = comment.comment_path or comment.file_path
    if comment.line:
        loc = f"{loc}:{comment.line}"
    author = comment.author or "unknown"
    body = _truncate_line(comment.body, 280)
    return (
        f"- {comment.file_path}: PR #{comment.pr_number} {comment.pr_title!r} "
        f"({comment.source}, {loc}, {author}): {body}"
    )


def render_review_history(histories: Sequence[GitHubFileReviewHistory], *, max_chars: int) -> str:
    lines: List[str] = [
        "Prior PR review history for changed files. Treat as institutional memory and hypotheses only."
    ]
    used = sum(len(line) + 1 for line in lines)
    for history in histories:
        if not history.comments:
            continue
        lines.append(f"File {history.file_path}:")
        used += len(lines[-1]) + 1
        for comment in history.comments:
            line = _comment_line(comment)
            if used + len(line) + 1 > max_chars:
                lines.append("... [review history truncated]")
                return "\n".join(lines)
            lines.append(line)
            used += len(line) + 1
    return "\n".join(lines)


def _dedupe_key(owner: str, repo: str, ref: str, paths: Sequence[str], current_pr: int | None) -> str:
    payload = {
        "owner": owner,
        "repo": repo,
        "ref": ref,
        "paths": list(paths),
        "current_pr": current_pr or 0,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def make_review_history_context_node(
    *,
    github_provider: IGitHubContextProvider | None,
    settings: Settings | None = None,
):
    node_name = "review_history_context"

    def review_history_context_node(state: GraphState) -> Dict[str, Any]:
        resolved = settings or get_settings()
        metadata = dict(state.get("metadata", {}) or {})
        slot: Dict[str, Any] = {"status": "skipped"}

        if not resolved.github_mcp_review_history_enabled:
            slot["skip_reason"] = "disabled"
            metadata[node_name] = slot
            return {"metadata": metadata, "node_history": [f"{node_name}:skipped"]}
        if github_provider is None:
            slot["skip_reason"] = "no_github_provider"
            metadata[node_name] = slot
            return {"metadata": metadata, "node_history": [f"{node_name}:skipped"]}

        repo_identity = _resolve_repo_identity(state)
        if repo_identity is None:
            slot["skip_reason"] = "missing_repo_identity"
            metadata[node_name] = slot
            return {"metadata": metadata, "node_history": [f"{node_name}:skipped"]}
        owner, repo = repo_identity

        changed_files = _target_files(state)[:20]
        if not changed_files:
            slot["skip_reason"] = "no_changed_files"
            metadata[node_name] = slot
            return {"metadata": metadata, "node_history": [f"{node_name}:skipped"]}

        ref = _resolve_history_ref(state, github_provider, owner, repo)
        current_pr = _resolve_current_pr_number(state)
        try:
            histories = github_provider.get_file_review_history(
                owner,
                repo,
                ref,
                changed_files,
                current_pr_number=current_pr,
                commits_per_file=resolved.github_mcp_review_history_commits_per_file,
                prs_per_file=resolved.github_mcp_review_history_prs_per_file,
                comments_per_pr=resolved.github_mcp_review_history_comments_per_pr,
                max_total_chars=resolved.github_mcp_review_history_max_total_chars,
            )
        except Exception as exc:  # noqa: BLE001 - optional enrichment
            slot["skip_reason"] = f"fetch_failed:{exc.__class__.__name__}"
            metadata[node_name] = slot
            logger.warning("review_history_context failed: %s", exc)
            return {"metadata": metadata, "node_history": [f"{node_name}:skipped"]}

        comment_count = sum(len(h.comments) for h in histories)
        warnings = [w for h in histories for w in h.warnings]
        degraded = any("missing_mcp_tool" in warning for warning in warnings)
        slot.update(
            {
                "status": "degraded" if degraded else "ok" if comment_count else "no_matches",
                "repo": f"{owner}/{repo}",
                "ref": ref,
                "changed_files": changed_files,
                "comment_count": comment_count,
                "warnings": warnings[:20],
                "mcp_degraded": degraded,
            }
        )
        metadata[node_name] = slot
        if comment_count == 0:
            return {"metadata": metadata, "node_history": [node_name]}

        rendered = render_review_history(
            histories,
            max_chars=resolved.github_mcp_review_history_max_total_chars,
        )
        ledger_entry = {
            "kind": "mandate_tool_observation",
            "tool": node_name,
            "args_preview": f"repo={owner}/{repo} ref={ref} files={changed_files[:8]}",
            "result_preview": rendered,
            "answer_preview": rendered[:420],
            "dedupe_key": _dedupe_key(owner, repo, ref, changed_files, current_pr),
            "caller": node_name,
            "patch_seq_applied": 0,
            "cached": False,
        }
        return {
            "metadata": metadata,
            "exploration_ledger": [ledger_entry],
            "node_history": [node_name],
        }

    return review_history_context_node
