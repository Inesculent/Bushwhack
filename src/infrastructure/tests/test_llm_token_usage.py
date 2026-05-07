"""Tests for LangChain token usage helpers."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, Field

from src.infrastructure.llm.token_usage import (
    extract_total_tokens_from_llm_result,
    extract_total_tokens_from_message,
    parse_structured_output,
)


class _Sample(BaseModel):
    x: int = Field(default=1)


def test_extract_from_usage_metadata() -> None:
    class _Msg:
        usage_metadata = {"total_tokens": 42}
        response_metadata = None

    assert extract_total_tokens_from_message(_Msg()) == 42


def test_extract_from_response_metadata_token_usage_dict() -> None:
    class _Msg:
        usage_metadata = None
        response_metadata = {"token_usage": {"total_tokens": 99}}

    assert extract_total_tokens_from_message(_Msg()) == 99


def test_extract_from_structured_include_raw_dict() -> None:
    class _Raw:
        usage_metadata = {"total_tokens": 7}
        response_metadata = None

    wrapped: dict[str, Any] = {"parsed": _Sample(x=2), "raw": _Raw()}
    assert extract_total_tokens_from_llm_result(wrapped) == 7


def test_parse_structured_dict_with_parsed_model() -> None:
    model = _Sample(x=3)
    out = parse_structured_output({"parsed": model, "raw": object()}, _Sample)
    assert out.x == 3


def test_parse_legacy_plain_model() -> None:
    model = _Sample(x=5)
    assert parse_structured_output(model, _Sample).x == 5


def test_parse_structured_parsed_none_raises() -> None:
    with pytest.raises(ValueError, match="parsed is None"):
        parse_structured_output({"parsed": None, "raw": object()}, _Sample)
