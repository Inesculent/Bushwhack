"""Best-effort extraction of LLM token counts from LangChain chat responses."""

from __future__ import annotations

from typing import Any, Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def extract_total_tokens_from_message(message: Any) -> int:
    """Return total token count from an AIMessage / BaseMessage or similar."""
    metadata = getattr(message, "usage_metadata", None)
    if isinstance(metadata, dict):
        total = metadata.get("total_tokens")
        if isinstance(total, int):
            return total
    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        token_usage = response_metadata.get("token_usage") or response_metadata.get("usage")
        if isinstance(token_usage, dict):
            total = token_usage.get("total_tokens")
            if isinstance(total, int):
                return total
    return 0


def extract_total_tokens_from_llm_result(invoke_result: Any) -> int:
    """Tokens from a raw chat invoke or a ``with_structured_output(..., include_raw=True)`` dict."""
    if isinstance(invoke_result, dict) and invoke_result.get("raw") is not None:
        return extract_total_tokens_from_message(invoke_result["raw"])
    return extract_total_tokens_from_message(invoke_result)


def parse_structured_output(invoke_result: Any, model_class: Type[T]) -> T:
    """Coerce structured-output invoke payloads into ``model_class``."""
    if isinstance(invoke_result, dict) and "parsed" in invoke_result:
        parsed = invoke_result["parsed"]
        if parsed is None:
            raise ValueError("Structured output parsing failed (parsed is None).")
        if isinstance(parsed, model_class):
            return parsed
        if isinstance(parsed, BaseModel):
            return model_class.model_validate(parsed.model_dump(mode="json"))
        raise TypeError(f"Structured output parsed payload has unexpected type: {type(parsed)!r}")
    if isinstance(invoke_result, model_class):
        return invoke_result
    if isinstance(invoke_result, BaseModel):
        return model_class.model_validate(invoke_result.model_dump(mode="json"))
    if isinstance(invoke_result, dict):
        return model_class.model_validate(invoke_result)
    raise TypeError(f"Cannot coerce structured invoke result to {model_class}: {type(invoke_result)!r}")
