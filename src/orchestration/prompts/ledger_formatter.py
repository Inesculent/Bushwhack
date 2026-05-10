"""Bounded formatting of exploration_ledger for prompts (avoid raw append-only bloat)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set


@dataclass
class LedgerFormatStats:
    """Metrics for observability."""

    total_entries: int = 0
    rendered: int = 0
    deduped: int = 0
    omitted_irrelevant: int = 0
    truncated_chars: int = 0


def _norm(s: str) -> str:
    return s.strip().lower()


def _dedupe_key(entry: Dict[str, Any]) -> str:
    raw = entry.get("dedupe_key")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    blob = json.dumps(entry, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _entry_relevance_score(
    entry: Dict[str, Any],
    *,
    task_id: Optional[str],
    target_files: Sequence[str],
    candidate_ids: Optional[Sequence[str]],
) -> int:
    score = 0
    e_task = entry.get("task_id")
    if task_id and isinstance(e_task, str) and e_task == task_id:
        score += 10
    if candidate_ids:
        cid = entry.get("candidate_id")
        if isinstance(cid, str) and cid in candidate_ids:
            score += 8
    q = str(entry.get("query_preview") or entry.get("query") or "")
    for fp in target_files:
        fp_n = _norm(fp)
        if fp_n and fp_n in _norm(q):
            score += 3
    return score


def format_exploration_ledger_for_prompt(
    ledger: Sequence[Dict[str, Any]] | None,
    *,
    task_id: Optional[str] = None,
    target_files: Optional[Sequence[str]] = None,
    candidate_ids: Optional[Sequence[str]] = None,
    max_entries: int = 5,
    max_chars: int = 2200,
    header: str = "Recent mental-model queries (bounded)",
) -> tuple[str, LedgerFormatStats]:
    """
    Return a short human-readable block and stats.
    Prefer entries relevant to task_id / target_files / candidate_ids, then most recent.
    """
    stats = LedgerFormatStats()
    if not ledger:
        return ("(no mental-model queries yet)", stats)

    files = [_normalize_path(p) for p in (target_files or []) if isinstance(p, str) and p.strip()]
    cands = list(candidate_ids or [])
    entries = [e for e in ledger if isinstance(e, dict)]
    stats.total_entries = len(entries)

    scored: List[tuple[int, int, Dict[str, Any]]] = []
    for idx, entry in enumerate(entries):
        rel = _entry_relevance_score(entry, task_id=task_id, target_files=files, candidate_ids=cands)
        scored.append((rel, idx, entry))

    scored.sort(key=lambda t: (-t[0], -t[1]))
    seen_keys: Set[str] = set()
    lines: List[str] = []
    chars = 0

    for rel, _idx, entry in scored:
        if len(lines) >= max_entries:
            stats.omitted_irrelevant += 1
            continue
        dk = _dedupe_key(entry)
        if dk in seen_keys:
            stats.deduped += 1
            continue
        seen_keys.add(dk)

        qprev = str(entry.get("query_preview") or entry.get("query") or "")[:240]
        aprev = str(entry.get("answer_preview") or entry.get("answer") or "")[:400]
        line = f"- [{entry.get('caller', '?')}] {qprev}\n  → {aprev}"
        if chars + len(line) > max_chars:
            stats.truncated_chars = max(0, max_chars - chars)
            break
        lines.append(line)
        chars += len(line) + 1
        stats.rendered += 1

    if not lines:
        tail = entries[-max_entries:]
        for entry in tail:
            dk = _dedupe_key(entry)
            if dk in seen_keys:
                stats.deduped += 1
                continue
            seen_keys.add(dk)
            qprev = str(entry.get("query_preview") or "")[:240]
            aprev = str(entry.get("answer_preview") or "")[:400]
            line = f"- [{entry.get('caller', '?')}] {qprev}\n  → {aprev}"
            if chars + len(line) > max_chars:
                break
            lines.append(line)
            chars += len(line) + 1
            stats.rendered += 1

    text = f"{header}:\n" + "\n".join(lines) if lines else f"{header}:\n(none selected)"
    if stats.rendered < stats.total_entries:
        stats.omitted_irrelevant = max(0, stats.total_entries - stats.rendered - stats.deduped)
    return text, stats


def _normalize_path(p: str) -> str:
    return p.strip().replace("\\", "/")
