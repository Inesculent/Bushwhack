"""Normalize focused-context search queries for sandbox ripgrep (not prose / MCP docs)."""

from __future__ import annotations

import re
from typing import List

from src.domain.schemas import FocusedContextRequest

_MAX_TEXT_QUERY_LEN = 80
_MAX_SYMBOL_QUERY_LEN = 64
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,48}\b")
_PROSE_MARKERS = (
    " the ",
    " when ",
    " function ",
    " returns ",
    " because ",
    " should ",
    " can ",
    " despite ",
    " during ",
    " pipeline ",
    " malicious ",
    " catastrophic ",
)


def _looks_like_prose(query: str) -> bool:
    q = f" {query.lower()} "
    if len(query) > _MAX_TEXT_QUERY_LEN:
        return True
    if query.count(" ") >= 6:
        return True
    if any(marker in q for marker in _PROSE_MARKERS):
        return True
    return False


def _extract_identifiers(text: str, *, limit: int = 4) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for match in _IDENTIFIER_RE.finditer(text):
        token = match.group(0)
        if token.lower() in {"the", "and", "for", "with", "from", "return", "none", "true", "false"}:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= limit:
            break
    return out


def sanitize_symbol_query(query: str) -> str | None:
    raw = (query or "").strip()
    if not raw:
        return None
    if _looks_like_prose(raw):
        ids = _extract_identifiers(raw, limit=1)
        return ids[0] if ids else None
    if len(raw) > _MAX_SYMBOL_QUERY_LEN:
        return raw[:_MAX_SYMBOL_QUERY_LEN]
    return raw


def sanitize_text_query(query: str) -> str | None:
    raw = (query or "").strip()
    if not raw:
        return None
    # Ripgrep-friendly alternation is allowed when short.
    if "|" in raw and len(raw) <= _MAX_TEXT_QUERY_LEN and not _looks_like_prose(raw):
        return raw
    if _looks_like_prose(raw):
        ids = _extract_identifiers(raw, limit=3)
        if not ids:
            return None
        if len(ids) == 1:
            return ids[0]
        return "|".join(ids)
    if len(raw) > _MAX_TEXT_QUERY_LEN:
        return raw[:_MAX_TEXT_QUERY_LEN]
    return raw


def sanitize_focused_context_request(request: FocusedContextRequest) -> FocusedContextRequest:
    """Drop or shorten queries that would trigger useless doc/MCP fallbacks."""
    symbols: List[str] = []
    seen_sym: set[str] = set()
    for sym in request.symbol_queries:
        cleaned = sanitize_symbol_query(sym)
        if cleaned and cleaned not in seen_sym:
            seen_sym.add(cleaned)
            symbols.append(cleaned)

    texts: List[str] = []
    seen_txt: set[str] = set()
    for tq in request.text_queries:
        cleaned = sanitize_text_query(tq)
        if cleaned and cleaned not in seen_txt:
            seen_txt.add(cleaned)
            texts.append(cleaned)

    if symbols == list(request.symbol_queries) and texts == list(request.text_queries):
        return request
    return request.model_copy(update={"symbol_queries": symbols, "text_queries": texts})
