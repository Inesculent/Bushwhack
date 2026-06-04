"""query_mental_model: pull bounded excerpts from BehavioralSpec; append exploration_ledger entries."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from src.config import Settings, get_settings
from src.domain.schemas import BehavioralSpec
from src.domain.state import GraphState
from src.infrastructure.behavioral_spec_store import BehavioralSpecStore


def _dedupe_key(query: str, task_id: Optional[str], topic: Optional[str]) -> str:
    payload = {"q": query.strip().lower()[:500], "task": task_id or "", "topic": (topic or "").strip().lower()[:120]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def _extract_relevant_excerpt(text: str, query: str, max_chars: int) -> str:
    if not text.strip():
        return ""
    q_terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2][:12]
    if not q_terms:
        out = text.strip()
        return out[:max_chars] + ("..." if len(out) > max_chars else "")

    lowered = text.lower()
    best_pos = -1
    best_score = 0
    for term in q_terms:
        pos = lowered.find(term)
        if pos >= 0:
            score = len(term)
            if score > best_score:
                best_score = score
                best_pos = pos
    if best_pos < 0:
        out = text.strip()
        return out[:max_chars] + ("..." if len(out) > max_chars else "")

    start = max(0, best_pos - max_chars // 3)
    chunk = text[start : start + max_chars]
    return chunk + ("..." if start + max_chars < len(text) else "")


def _summarize_spec_for_query(spec: BehavioralSpec, query: str, max_chars: int) -> str:
    question_lines = []
    for question in spec.contract_questions[:12]:
        parts = [
            question.owner,
            question.dimension,
            question.expected_behavior,
            question.breach_question,
        ]
        line = " | ".join(part.strip() for part in parts if part and part.strip())
        if line:
            question_lines.append(line[:500])
    sections = [
        ("intent", spec.intent_summary),
        ("behavior", spec.behavioral_expectations),
        ("contract_questions", "\n".join(question_lines)),
        ("contracts", spec.contract_boundaries),
        ("history", spec.historical_precedents),
        ("risks", spec.risk_hypotheses),
        ("guidance", spec.reviewer_guidance),
        ("uncertainties", spec.uncertainties),
    ]
    parts: List[str] = []
    used = 0
    for label, body in sections:
        excerpt = _extract_relevant_excerpt(body, query, max(400, max_chars // 4))
        if not excerpt.strip():
            continue
        block = f"## {label}\n{excerpt.strip()}"
        if used + len(block) > max_chars:
            remain = max_chars - used - 20
            if remain > 80:
                parts.append(f"## {label}\n{excerpt.strip()[:remain]}...")
            break
        parts.append(block)
        used += len(block) + 2
    if spec.evidence_refs and used < max_chars:
        refs = "\n".join(f"- {r.kind}: {r.ref} — {r.note}" for r in spec.evidence_refs[:8])
        tail = f"## evidence_refs\n{refs}"
        if used + len(tail) <= max_chars:
            parts.append(tail)
    out = "\n\n".join(parts).strip()
    if len(out) > max_chars:
        return out[: max_chars - 3] + "..."
    return out or "(empty behavioral spec)"


def query_mental_model(
    *,
    state: GraphState,
    query: str,
    topic: str = "",
    task_id: Optional[str] = None,
    caller: str = "query_mental_model",
    settings: Optional[Settings] = None,
    store: Optional[BehavioralSpecStore] = None,
) -> Dict[str, Any]:
    """
    Return {answer, exploration_ledger list patch, skipped, skip_reason, dedupe_key}.
    Caller merges exploration_ledger via LangGraph reducer.
    """
    settings = settings or get_settings()
    ref = state.get("behavioral_spec_ref")
    ledger_prior = list(state.get("exploration_ledger") or [])
    dk = _dedupe_key(query, task_id, topic)

    for prev in ledger_prior:
        if isinstance(prev, dict) and prev.get("dedupe_key") == dk and prev.get("kind") == "mental_model_query":
            return {
                "answer": str(prev.get("answer_full") or prev.get("answer_preview") or ""),
                "exploration_ledger": [],
                "skipped": True,
                "skip_reason": "dedupe_cache_hit",
                "dedupe_key": dk,
            }

    used = sum(1 for e in ledger_prior if isinstance(e, dict) and e.get("kind") == "mental_model_query")
    if used >= int(settings.reviewer_mental_model_max_queries_per_run):
        return {
            "answer": "",
            "exploration_ledger": [],
            "skipped": True,
            "skip_reason": "query_budget_exceeded",
            "dedupe_key": dk,
        }

    if settings.reviewer_legacy_planner_mode or not ref:
        return {
            "answer": "",
            "exploration_ledger": [],
            "skipped": True,
            "skip_reason": "no_behavioral_spec_ref_or_legacy_mode",
            "dedupe_key": dk,
        }

    store = store or BehavioralSpecStore(settings)
    try:
        spec = store.read(ref)
    except FileNotFoundError:
        return {
            "answer": "",
            "exploration_ledger": [],
            "skipped": True,
            "skip_reason": "spec_file_missing",
            "dedupe_key": dk,
        }

    max_chars = int(settings.reviewer_mental_model_max_answer_chars)
    answer = _summarize_spec_for_query(spec, query, max_chars)
    q_preview = query.strip()[:220]
    a_preview = answer[:420]

    entry: Dict[str, Any] = {
        "kind": "mental_model_query",
        "dedupe_key": dk,
        "query_preview": q_preview,
        "answer_preview": a_preview,
        "answer_full": answer[: max_chars + 500],  # still bounded; full answer for same-branch reuse
        "topic": topic or "",
        "task_id": task_id or "",
        "caller": caller,
    }
    return {
        "answer": answer,
        "exploration_ledger": [entry],
        "skipped": False,
        "skip_reason": "",
        "dedupe_key": dk,
    }
