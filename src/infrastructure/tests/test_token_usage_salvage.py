"""Tests for structured-output salvage from raw LLM messages."""

from __future__ import annotations

from types import SimpleNamespace

from src.domain.schemas import CritiquerOutput
from src.infrastructure.llm.token_usage import salvage_structured_output_from_raw


def test_salvage_structured_output_from_raw_json_fence() -> None:
    invoke_result = {
        "parsed": None,
        "raw": SimpleNamespace(
            content='```json\n{"summary": "ok", "candidates": [], "warnings": []}\n```'
        ),
    }
    out = salvage_structured_output_from_raw(invoke_result, CritiquerOutput)
    assert out is not None
    assert out.summary == "ok"
