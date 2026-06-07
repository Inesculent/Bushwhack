"""Parse misrouted not_applicable rationales for salvage promotion in cleanup."""

from __future__ import annotations

import re
from typing import Literal

ReviewCategory = Literal["security", "logic", "performance", "general", "other"]

# (substring in rationale, target specialty) — order matters (first match wins).
_REDIRECT_HINTS: tuple[tuple[str, ReviewCategory], ...] = (
    ("belongs in security", "security"),
    ("belongs under security", "security"),
    ("security reflector", "security"),
    ("security domain", "security"),
    ("not a performance", "security"),
    ("not primarily performance", "security"),
    ("belongs in logic", "logic"),
    ("correctness issue", "logic"),
    ("logic reflector", "logic"),
    ("belongs in performance", "performance"),
    ("performance reflector", "performance"),
)


def parse_misroute_redirect_category(rationale: str) -> ReviewCategory | None:
    """Infer the specialty a not_applicable report points at, if any."""
    blob = (rationale or "").lower()
    if not blob.strip():
        return None
    for phrase, category in _REDIRECT_HINTS:
        if phrase in blob:
            return category
    match = re.search(
        r"(?:reclassif(?:y|ied)|route|redirect(?:ed)?)\s+(?:to|as|under)\s+"
        r"(security|logic|performance|general)",
        blob,
    )
    if match:
        return match.group(1)  # type: ignore[return-value]
    return None
