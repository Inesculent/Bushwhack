"""Sanitized shallow copies of GraphState for LangGraph Send branches."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping

from src.domain.state import GraphState

# Keys that must never be copied into parallel worker payloads (defense in depth).
_STRIP_KEYS = frozenset(
    {
        "behavioral_spec",
        "exploration_ledger_full",
        "llm_trace",
    }
)


def payload_for_send(state: GraphState, **overrides: Any) -> Dict[str, Any]:
    """
    Shallow copy of state for Send(...), stripping keys that could bloat prompts
    or leak large internal blobs if ever added to GraphState.
    """
    payload: Dict[str, Any] = {}
    for key, value in state.items():
        if key in _STRIP_KEYS:
            continue
        payload[key] = value
    # LangGraph reducers merge every Send branch back into the parent. Start
    # additive fields at their identities so branches do not re-add parent totals.
    payload["token_usage"] = 0
    payload["node_history"] = []
    payload.update(overrides)
    return payload


def subgraph_parent_updates(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    keys: Iterable[str],
    additive_lists: Iterable[str] = (),
    additive_ints: Iterable[str] = (),
    union_dicts: Iterable[str] = (),
) -> Dict[str, Any]:
    """Return only the reducer deltas produced inside a compiled Send subgraph."""
    list_keys = set(additive_lists)
    int_keys = set(additive_ints)
    dict_keys = set(union_dicts)
    updates: Dict[str, Any] = {}
    for key in keys:
        if key not in after:
            continue
        if key in list_keys:
            old = list(before.get(key) or [])
            new = list(after.get(key) or [])
            updates[key] = new[len(old) :] if new[: len(old)] == old else new
        elif key in int_keys:
            updates[key] = int(after.get(key) or 0) - int(before.get(key) or 0)
        elif key in dict_keys:
            old = dict(before.get(key) or {})
            new = dict(after.get(key) or {})
            updates[key] = {name: value for name, value in new.items() if old.get(name) != value}
        else:
            updates[key] = after[key]
    return updates
