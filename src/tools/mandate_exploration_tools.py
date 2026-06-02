"""Bounded repository tools for mandate_explorer (bootstrap + targeted modes)."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.config import Settings, get_settings
from src.domain.schemas import CommunitySemanticSummary
from src.domain.state import GraphState
from src.orchestration.context.review_context import (
    LazyReviewContextProvider,
    structural_neighbor_summary,
    symbol_call_edges_for_file,
)
from src.orchestration.nodes.application.planner import _target_files

logger = logging.getLogger(__name__)

TOOL_NAMES = frozenset(
    {
        "read_file",
        "search_code",
        "graph_neighbors",
        "symbol_call_edges",
        "community_digest",
        "git_history",
        "list_changed_files",
    }
)


def tool_dedupe_key(tool: str, args: Dict[str, Any]) -> str:
    payload = {"tool": tool.strip().lower(), "args": args}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def _git_recent_messages(repo_path: str, *, max_lines: int = 8) -> str:
    try:
        proc = subprocess.run(
            ["git", "log", "-n", str(max_lines), "--oneline", "--no-decorate"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return ""
        return proc.stdout.strip()[:4000]
    except Exception as exc:  # noqa: BLE001
        logger.debug("git_history skipped: %s", exc)
        return ""


def _git_log_for_paths(repo_path: str, paths: List[str], *, max_lines: int = 8) -> str:
    if not paths:
        return _git_recent_messages(repo_path, max_lines=max_lines)
    try:
        proc = subprocess.run(
            ["git", "log", "-n", str(max_lines), "--oneline", "--no-decorate", "--", *paths],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return ""
        return proc.stdout.strip()[:4000]
    except Exception as exc:  # noqa: BLE001
        logger.debug("git_history paths skipped: %s", exc)
        return ""


def _community_digest_for_changed_files(
    state: GraphState,
    changed_files: Sequence[str],
    *,
    max_communities: int = 6,
    max_chars: int = 6000,
) -> str:
    summaries_raw = state.get("community_summaries") or []
    changed = {p.strip().replace("\\", "/") for p in changed_files if p.strip()}
    lines: List[str] = []
    used = 0
    for raw in summaries_raw:
        cs: CommunitySemanticSummary | None = None
        if isinstance(raw, CommunitySemanticSummary):
            cs = raw
        elif isinstance(raw, dict):
            try:
                cs = CommunitySemanticSummary.model_validate(raw)
            except Exception:
                continue
        if cs is None:
            continue
        touch = False
        for fs in cs.file_summaries:
            fp = ""
            if hasattr(fs, "file_node_id"):
                fp = str(getattr(fs, "file_node_id", "")).removeprefix("file:")
            elif isinstance(fs, dict):
                fp = str(fs.get("file_path") or fs.get("file_node_id", "")).removeprefix("file:")
            if fp and fp in changed:
                touch = True
                break
        if not touch and changed:
            continue
        block = f"Community {cs.community_id} ({cs.label}): {cs.purpose}"
        if used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block) + 1
        if len(lines) >= max_communities:
            break
    if not lines and summaries_raw:
        for raw in list(summaries_raw)[:max_communities]:
            if isinstance(raw, CommunitySemanticSummary):
                cs = raw
            elif isinstance(raw, dict):
                try:
                    cs = CommunitySemanticSummary.model_validate(raw)
                except Exception:
                    continue
            else:
                continue
            block = f"Community {cs.community_id} ({cs.label}): {cs.purpose}"
            if used + len(block) > max_chars:
                break
            lines.append(block)
            used += len(block) + 1
    return "\n".join(lines) if lines else "(no community summaries)"


class MandateToolExecutor:
    """Execute mandate explorer tools with caps and dedupe cache."""

    def __init__(
        self,
        provider: LazyReviewContextProvider,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings or get_settings()
        self._dedupe_cache: Dict[str, str] = {}

    def execute(
        self,
        state: GraphState,
        tool: str,
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        name = tool.strip().lower()
        if name not in TOOL_NAMES:
            return {
                "ok": False,
                "text": f"Unknown tool: {tool}",
                "dedupe_key": tool_dedupe_key(name, args),
                "cached": False,
            }
        dk = tool_dedupe_key(name, args)
        if dk in self._dedupe_cache:
            return {
                "ok": True,
                "text": self._dedupe_cache[dk],
                "dedupe_key": dk,
                "cached": True,
            }

        max_obs = int(self._settings.reviewer_mandate_explorer_max_observation_chars)
        text = ""
        try:
            self._provider._ensure_started(state)  # noqa: SLF001
            if name == "list_changed_files":
                files = _target_files(state)
                text = "\n".join(f"- {f}" for f in files[:40]) or "(none)"
            elif name == "read_file":
                fp = str(args.get("file_path") or "")
                full = bool(args.get("full_file"))
                if full:
                    text = self._provider.read_full_file(
                        fp, max_chars=min(max_obs, self._settings.review_full_file_max_chars)
                    )
                else:
                    text = self._provider.read_file_slice(fp, max_chars=max_obs)
            elif name == "search_code":
                query = str(args.get("query") or "")
                fps = args.get("file_paths")
                paths = [str(p) for p in fps] if isinstance(fps, list) else None
                hits = self._provider.search_bounded(query, max_hits=12, file_paths=paths)
                parts = [f"{h.file_path}:{h.line_number}: {h.content[:300]}" for h in hits[:12]]
                text = "\n".join(parts) if parts else "(no hits)"
            elif name == "graph_neighbors":
                fp = str(args.get("file_path") or "")
                text = structural_neighbor_summary(state, fp) or "(no neighbors)"
            elif name == "symbol_call_edges":
                fp = str(args.get("file_path") or "")
                edges = symbol_call_edges_for_file(state, fp)
                if not edges:
                    text = "(no call edges)"
                else:
                    lines = [f"{sym} -> {', '.join(tgts)}" for sym, tgts in sorted(edges.items())]
                    text = "\n".join(lines[:32])
            elif name == "community_digest":
                files = _target_files(state)
                limit = int(args.get("max_communities") or 6)
                text = _community_digest_for_changed_files(
                    state, files, max_communities=limit, max_chars=max_obs
                )
            elif name == "git_history":
                repo = str(state.get("repo_path") or "")
                paths_arg = args.get("paths")
                paths = [str(p) for p in paths_arg] if isinstance(paths_arg, list) else _target_files(state)[:8]
                if repo and Path(repo).is_dir():
                    text = _git_log_for_paths(repo, paths) or "(no git log)"
                else:
                    text = "(git unavailable — remote repo)"
        except Exception as exc:  # noqa: BLE001
            text = f"tool_error:{exc.__class__.__name__}: {exc}"

        if len(text) > max_obs:
            text = text[: max_obs - 20] + "\n... [truncated]"
        self._dedupe_cache[dk] = text
        return {"ok": True, "text": text, "dedupe_key": dk, "cached": False}
