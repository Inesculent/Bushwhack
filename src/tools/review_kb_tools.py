"""Repository KB query tools.

``query_review_kb`` remains as the compatibility/review-time entry point.
``query_repository_kb`` is the core repository-scoped query surface.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence

from src.config import Settings, get_settings
from src.domain.schemas import ReviewKBRecord
from src.domain.state import GraphState
from src.infrastructure.review_kb import load_review_kb, query_loaded_review_kb


def _snapshot_root_from_state(state: GraphState) -> str:
    root = str(state.get("snapshot_root") or "").strip()
    if root:
        return root
    meta = state.get("metadata") or {}
    snap = meta.get("exploration_snapshot") if isinstance(meta, dict) else None
    if isinstance(snap, dict):
        return str(snap.get("snapshot_root") or "").strip()
    return ""


def _dedupe_key(
    query: str,
    path: Optional[str],
    symbol: Optional[str],
    topics: Sequence[str] | None,
    task_id: Optional[str],
    use_review_overlay: bool,
) -> str:
    payload = {
        "q": query.strip().lower()[:500],
        "path": (path or "").strip().lower(),
        "symbol": (symbol or "").strip().lower(),
        "topics": [str(t).strip().lower() for t in (topics or [])],
        "task": task_id or "",
        "overlay": bool(use_review_overlay),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def _record_line(record: ReviewKBRecord) -> str:
    locations: List[str] = []
    for evidence in record.evidence[:2]:
        if evidence.file_path:
            loc = evidence.file_path
            if evidence.line_start:
                loc += f":{evidence.line_start}"
            locations.append(loc)
    tags = ", ".join(record.tags[:6])
    tag_text = f" [{tags}]" if tags else ""
    loc_text = f" ({', '.join(locations)})" if locations else ""
    source_ids = record.metadata.get("source_record_ids") or []
    source_text = ""
    if source_ids:
        source_text = f" sources={', '.join(str(x) for x in source_ids[:5])}"
    return (
        f"- {record.kind} `{record.id}` confidence={record.confidence}"
        f"{loc_text}{tag_text}{source_text}: {record.summary}"
    )


def _render_answer(records: Sequence[ReviewKBRecord], related: Sequence[ReviewKBRecord], max_chars: int) -> str:
    lines: List[str] = []
    if records:
        lines.append("## primary")
        lines.extend(_record_line(r) for r in records)
    if related:
        lines.append("## related")
        lines.extend(_record_line(r) for r in related)
    if not lines:
        return "(no review KB matches)"
    answer = "\n".join(lines)
    if len(answer) > max_chars:
        return answer[: max_chars - 3] + "..."
    return answer


def query_review_kb(
    *,
    state: GraphState,
    query: str,
    path: Optional[str] = None,
    symbol: Optional[str] = None,
    community_id: Optional[int] = None,
    topics: Optional[List[str]] = None,
    include_dependencies: bool = True,
    use_review_overlay: bool = True,
    max_results: int = 8,
    task_id: Optional[str] = None,
    caller: str = "query_review_kb",
    settings: Optional[Settings] = None,
    ledger_kind: str = "review_kb_query",
) -> Dict[str, Any]:
    """
    Return bounded repository KB matches plus an exploration_ledger patch.

    The result shape mirrors query_mental_model enough for existing orchestration:
    {answer, result, exploration_ledger, skipped, skip_reason, dedupe_key}.
    """
    settings = settings or get_settings()
    ledger_prior = list(state.get("exploration_ledger") or [])
    dk = _dedupe_key(query, path, symbol, topics, task_id, use_review_overlay)

    for prev in ledger_prior:
        if isinstance(prev, dict) and prev.get("dedupe_key") == dk and prev.get("kind") == ledger_kind:
            return {
                "answer": str(prev.get("answer_full") or prev.get("answer_preview") or ""),
                "result": prev.get("result") or {},
                "exploration_ledger": [],
                "skipped": True,
                "skip_reason": "dedupe_cache_hit",
                "dedupe_key": dk,
            }

    snapshot_root = _snapshot_root_from_state(state)
    if not snapshot_root:
        return {
            "answer": "",
            "result": {},
            "exploration_ledger": [],
            "skipped": True,
            "skip_reason": "no_snapshot_root",
            "dedupe_key": dk,
        }

    try:
        kb = load_review_kb(snapshot_root)
    except FileNotFoundError:
        return {
            "answer": "",
            "result": {},
            "exploration_ledger": [],
            "skipped": True,
            "skip_reason": "review_kb_missing",
            "dedupe_key": dk,
        }

    result = query_loaded_review_kb(
        kb,
        query=query,
        path=path,
        symbol=symbol,
        community_id=community_id,
        topics=topics or [],
        include_dependencies=include_dependencies,
        use_review_overlay=use_review_overlay,
        max_results=max(1, int(max_results)),
    )
    max_chars = int(settings.reviewer_mental_model_max_answer_chars)
    answer = _render_answer(result.primary_records, result.related_records, max_chars)
    entry: Dict[str, Any] = {
        "kind": ledger_kind,
        "dedupe_key": dk,
        "query_preview": query.strip()[:220],
        "answer_preview": answer[:420],
        "answer_full": answer[: max_chars + 500],
        "result": result.model_dump(mode="json"),
        "path": path or "",
        "symbol": symbol or "",
        "topics": topics or [],
        "use_review_overlay": use_review_overlay,
        "task_id": task_id or "",
        "caller": caller,
    }
    return {
        "answer": answer,
        "result": result.model_dump(mode="json"),
        "exploration_ledger": [entry],
        "skipped": False,
        "skip_reason": "",
        "dedupe_key": dk,
    }


def query_repository_kb(
    *,
    state: GraphState,
    query: str,
    path: Optional[str] = None,
    symbol: Optional[str] = None,
    community_id: Optional[int] = None,
    topics: Optional[List[str]] = None,
    include_dependencies: bool = True,
    max_results: int = 8,
    task_id: Optional[str] = None,
    caller: str = "query_repository_kb",
    settings: Optional[Settings] = None,
) -> Dict[str, Any]:
    """Core repository KB query without PR/review overlay boosts."""
    return query_review_kb(
        state=state,
        query=query,
        path=path,
        symbol=symbol,
        community_id=community_id,
        topics=topics,
        include_dependencies=include_dependencies,
        use_review_overlay=False,
        max_results=max_results,
        task_id=task_id,
        caller=caller,
        settings=settings,
        ledger_kind="repository_kb_query",
    )
