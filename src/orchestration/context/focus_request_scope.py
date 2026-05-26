"""Clamp focused-context file paths to the PR review scope (diff + task targets)."""

from __future__ import annotations

from typing import Iterable, Sequence

from src.domain.schemas import FocusedContextRequest
from src.domain.state import GraphState
from src.orchestration.nodes.application.planner import _extract_files_from_diff


def _norm_path(path: str) -> str:
    return path.strip().replace("\\", "/")


def allowed_review_paths(
    state: GraphState,
    *,
    task_target_files: Sequence[str] | None = None,
    candidate_file_path: str | None = None,
) -> frozenset[str]:
    """Paths the reviewer may read for this PR (diff-visible files plus explicit anchors)."""
    paths: list[str] = []
    paths.extend(_extract_files_from_diff(state.get("git_diff", "") or ""))
    if task_target_files:
        paths.extend(task_target_files)
    if candidate_file_path and candidate_file_path.strip():
        paths.append(candidate_file_path.strip())
    return frozenset(_norm_path(p) for p in paths if p and str(p).strip())


def clamp_focused_context_request(
    request: FocusedContextRequest,
    allowed: frozenset[str],
    *,
    fallback_path: str | None = None,
) -> FocusedContextRequest:
    """Drop out-of-scope file_paths so sandbox/MCP work stays on the changed surface."""
    if not allowed:
        return request

    kept: list[str] = []
    seen: set[str] = set()
    for raw in request.file_paths:
        fp = _norm_path(str(raw))
        if not fp or fp in seen:
            continue
        if fp not in allowed:
            continue
        seen.add(fp)
        kept.append(fp)

    if not kept and fallback_path:
        fb = _norm_path(fallback_path)
        if fb in allowed and fb not in seen:
            kept.append(fb)

    if kept == list(request.file_paths):
        return request
    return request.model_copy(update={"file_paths": kept})
