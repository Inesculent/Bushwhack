"""Sanitized shallow copies of GraphState for LangGraph Send branches."""

from __future__ import annotations

from typing import Any, Dict

from src.domain.state import GraphState

# Keys that must never be copied into parallel worker payloads (defense in depth).
_STRIP_KEYS = frozenset(
    {
        "behavioral_spec",
        "exploration_ledger_full",
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
    payload.update(overrides)
    return payload
