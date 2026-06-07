"""Tests for focused-context query sanitization."""

from __future__ import annotations

from src.domain.schemas import FocusedContextRequest
from src.orchestration.context.focused_query_sanitize import (
    sanitize_focused_context_request,
    sanitize_text_query,
)


def test_sanitize_text_query_rejects_prose() -> None:
    prose = (
        "When mode parameter is not 'Equal', 'Starts With', or 'Ends With', "
        "function returns None implicitly, breaking pipeline contract"
    )
    cleaned = sanitize_text_query(prose)
    assert cleaned is not None
    assert len(cleaned) <= 80
    assert "When mode parameter" not in cleaned


def test_sanitize_focused_context_request_strips_long_queries() -> None:
    req = FocusedContextRequest(
        request_id="r1",
        candidate_id="c1",
        requested_by_specialty="logic",
        file_paths=["comfy_extras/nodes_string.py"],
        symbol_queries=[],
        text_queries=[
            "Catastrophic backtracking: malicious regex patterns can cause CPU DoS "
            "by exhausting computation during re.search execution, despite re.error handling."
        ],
    )
    out = sanitize_focused_context_request(req)
    assert len(out.text_queries) <= 3
    for tq in out.text_queries:
        assert len(tq) <= 80
