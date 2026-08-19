from __future__ import annotations

from typing import Any

from src.domain.schemas import FocusedContextRequest, SearchResult
from src.orchestration.nodes.application.focused_context import make_focused_context_node


class _Provider:
    def _ensure_started(self, _state: dict[str, Any]) -> None:
        return None

    def read_file_window(self, *_args: Any, **_kwargs: Any) -> str:
        return ""

    def read_file_slice(self, *_args: Any, **_kwargs: Any) -> str:
        return ""

    def read_full_file(self, *_args: Any, **_kwargs: Any) -> str:
        return ""

    def ast_entities_for_file(self, *_args: Any, **_kwargs: Any) -> tuple[list[Any], list[str]]:
        return [], []

    def search_bounded(self, *_args: Any, **_kwargs: Any) -> list[SearchResult]:
        return []


class _PathMismatchProvider(_Provider):
    def search_bounded(self, *_args: Any, **_kwargs: Any) -> list[SearchResult]:
        return [
            SearchResult(
                file_path="src/other.py",
                line_number=7,
                content="hit",
                context_lines=[],
            )
        ]


class _UnavailableProvider(_Provider):
    def _ensure_started(self, _state: dict[str, Any]) -> None:
        raise RuntimeError("context provider unavailable")


class _ThinProvider(_Provider):
    def read_file_slice(self, *_args: Any, **_kwargs: Any) -> str:
        return "def other():\n    pass\n"


def _request(**updates: Any) -> FocusedContextRequest:
    values = {
        "request_id": "r1",
        "candidate_id": "c1",
        "requested_by_specialty": "logic",
        "file_paths": ["src/app.py"],
        "symbol_queries": [],
        "text_queries": ["why the changed function should return because callers rely on it"],
        "reason": "need focused evidence",
    }
    values.update(updates)
    return FocusedContextRequest(**values)


def test_focused_context_records_sanitized_query_and_no_hits() -> None:
    node = make_focused_context_node(_Provider())  # type: ignore[arg-type]

    out = node({"focused_context_requests": [_request()], "focused_context_results": {}})

    row = out["metadata"]["focused_context"]["diagnostics"][0]
    assert "sanitized_query" in row["outcomes"]
    assert "no_hits" in row["outcomes"]
    assert out["metadata"]["focused_context"]["focused_effective_path_count"] == 0


def test_focused_context_records_path_mismatch_and_tool_unavailable() -> None:
    mismatch = make_focused_context_node(_PathMismatchProvider())(  # type: ignore[arg-type]
        {
            "focused_context_requests": [_request(text_queries=["needle"])],
            "focused_context_results": {},
        }
    )
    mismatch_row = mismatch["metadata"]["focused_context"]["diagnostics"][0]
    assert "path_mismatch" in mismatch_row["outcomes"]
    assert mismatch["metadata"]["focused_context"]["focused_effective_path_count"] == 1

    unavailable = make_focused_context_node(_UnavailableProvider())(  # type: ignore[arg-type]
        {"focused_context_requests": [_request()], "focused_context_results": {}}
    )
    unavailable_row = unavailable["metadata"]["focused_context"]["diagnostics"][0]
    assert "tool_unavailable" in unavailable_row["outcomes"]


def test_focused_context_records_missing_full_and_symbol_context() -> None:
    missing_full = make_focused_context_node(_Provider())(  # type: ignore[arg-type]
        {
            "focused_context_requests": [_request(file_read_mode="full", text_queries=[])],
            "focused_context_results": {},
        }
    )
    missing_full_row = missing_full["metadata"]["focused_context"]["diagnostics"][0]
    assert "missing_requested_file_context" in missing_full_row["outcomes"]
    assert "missing_full_file_context" in missing_full_row["outcomes"]

    thin_symbol = make_focused_context_node(_ThinProvider())(  # type: ignore[arg-type]
        {
            "focused_context_requests": [
                _request(symbol_queries=["allocate_slots"], text_queries=[])
            ],
            "focused_context_results": {},
        }
    )
    thin_row = thin_symbol["metadata"]["focused_context"]["diagnostics"][0]
    assert "thin_symbol_context" in thin_row["outcomes"]
    assert "missing_symbol_context" in thin_row["outcomes"]
    assert "focused_context_thin_symbol_context" in thin_symbol["metadata"]["focused_context"]["health_warnings"]
