"""Trace helpers for bounded, trace-gated LLM request/response logging."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import BaseModel

from src.domain.state import GraphState
from src.infrastructure.llm.token_usage import (
    extract_token_usage_details_from_llm_result,
    extract_total_tokens_from_llm_result,
)

trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

_LOCK = threading.Lock()
_REQUEST_COUNTER = 0
_LIVE_TOTALS: defaultdict[str, int] = defaultdict(int)
_PREVIEW_CHARS = 240


@dataclass(frozen=True)
class TracedLLMResult:
    result: Any
    tokens: int
    trace_records: list[dict[str, Any]]


def trace_enabled(state: GraphState | None) -> bool:
    metadata = (state or {}).get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def append_trace(
    existing: list[dict[str, Any]] | None,
    traced: TracedLLMResult,
) -> list[dict[str, Any]]:
    return list(existing or []) + list(traced.trace_records)


def trace_from_exception(exc: BaseException) -> list[dict[str, Any]]:
    records = getattr(exc, "llm_trace_records", None)
    if isinstance(records, list):
        return [record for record in records if isinstance(record, dict)]
    return []


def trace_llm_call(
    llm: Any,
    prompt: Any,
    *,
    state: GraphState | None,
    node_name: str,
    model_key: str | None = None,
    schema_name: str | None = None,
    request_label: str = "",
    input_summary: Mapping[str, Any] | None = None,
) -> TracedLLMResult:
    """Invoke ``llm`` and emit bounded live/raw trace records when --trace is enabled."""
    if not trace_enabled(state):
        result = llm.invoke(prompt)
        return TracedLLMResult(
            result=result,
            tokens=extract_total_tokens_from_llm_result(result),
            trace_records=[],
        )

    run_id = str((state or {}).get("run_id", "unknown"))
    request_id = _next_request_id(run_id, node_name)
    request_record: dict[str, Any] = {
        "event": "llm_request",
        "run_id": run_id,
        "node": node_name,
        "request_id": request_id,
        "model_key": model_key or "",
        "schema": schema_name or "",
        "label": request_label,
        "input": _safe_mapping(input_summary or {}),
        "prompt": _summarize_text(prompt),
        "live_total_tokens_before": _live_total(run_id),
    }
    _log_record(request_record)

    started = time.perf_counter()
    try:
        result = llm.invoke(prompt)
    except Exception as exc:
        error_record = {
            "event": "llm_error",
            "run_id": run_id,
            "node": node_name,
            "request_id": request_id,
            "model_key": model_key or "",
            "schema": schema_name or "",
            "label": request_label,
            "elapsed_ms": _elapsed_ms(started),
            "error_type": exc.__class__.__name__,
            "error": _preview(str(exc)),
        }
        _log_record(error_record)
        setattr(exc, "llm_trace_records", [request_record, error_record])
        raise

    details = extract_token_usage_details_from_llm_result(result)
    tokens = extract_total_tokens_from_llm_result(result)
    total_for_live = tokens if tokens else int(details.get("total_tokens") or 0)
    live_total = _add_live_total(run_id, total_for_live)
    response_record = {
        "event": "llm_response",
        "run_id": run_id,
        "node": node_name,
        "request_id": request_id,
        "model_key": model_key or "",
        "schema": schema_name or "",
        "label": request_label,
        "elapsed_ms": _elapsed_ms(started),
        "token_usage": details,
        "total_tokens": tokens,
        "live_total_tokens": live_total,
        "output": _summarize_invoke_result(result),
    }
    _log_record(response_record)
    return TracedLLMResult(
        result=result,
        tokens=tokens,
        trace_records=[request_record, response_record],
    )


def _next_request_id(run_id: str, node_name: str) -> str:
    global _REQUEST_COUNTER
    with _LOCK:
        _REQUEST_COUNTER += 1
        seq = _REQUEST_COUNTER
    safe_node = node_name.replace(" ", "_")
    return f"{run_id}:{safe_node}:{seq}"


def _live_total(run_id: str) -> int:
    with _LOCK:
        return int(_LIVE_TOTALS[run_id])


def _add_live_total(run_id: str, tokens: int) -> int:
    with _LOCK:
        _LIVE_TOTALS[run_id] += max(0, int(tokens or 0))
        return int(_LIVE_TOTALS[run_id])


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _preview(text: str, *, max_chars: int = _PREVIEW_CHARS) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


def _summarize_text(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else str(value)
    return {
        "type": type(value).__name__,
        "chars": len(text),
        "lines": text.count("\n") + (1 if text else 0),
        "sha256_16": _hash_text(text),
        "preview": _preview(text),
    }


def _safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _safe_value(val) for key, val in value.items()}


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _preview(value)
    if isinstance(value, BaseModel):
        return _summarize_model(value)
    if isinstance(value, Mapping):
        return {str(k): _safe_value(v) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple, set)):
        return {"count": len(value), "preview": [_safe_value(v) for v in list(value)[:5]]}
    return _preview(str(value))


def _summarize_model(model: BaseModel) -> dict[str, Any]:
    data = model.model_dump(mode="json")
    return {
        "type": model.__class__.__name__,
        "fields": _summarize_mapping_shape(data),
    }


def _summarize_mapping_shape(data: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in list(data.items())[:20]:
        if isinstance(value, str):
            out[key] = {"type": "str", "chars": len(value), "preview": _preview(value, max_chars=120)}
        elif isinstance(value, list):
            out[key] = {"type": "list", "count": len(value)}
        elif isinstance(value, dict):
            out[key] = {"type": "dict", "keys": list(value.keys())[:10], "count": len(value)}
        else:
            out[key] = value if value is None or isinstance(value, (bool, int, float)) else type(value).__name__
    return out


def _raw_content(invoke_result: Any) -> str:
    raw = invoke_result.get("raw") if isinstance(invoke_result, dict) else invoke_result
    content = getattr(raw, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content or "")


def _summarize_invoke_result(invoke_result: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"type": type(invoke_result).__name__}
    if isinstance(invoke_result, dict):
        parsed = invoke_result.get("parsed")
        summary["keys"] = sorted(str(k) for k in invoke_result.keys())
        if isinstance(parsed, BaseModel):
            summary["parsed"] = _summarize_model(parsed)
        elif parsed is None and "parsed" in invoke_result:
            summary["parsed"] = None
        elif parsed is not None:
            summary["parsed_type"] = type(parsed).__name__
    elif isinstance(invoke_result, BaseModel):
        summary["parsed"] = _summarize_model(invoke_result)

    raw_text = _raw_content(invoke_result)
    if raw_text:
        summary["raw_text"] = _summarize_text(raw_text)
    return summary


def _log_record(record: dict[str, Any]) -> None:
    trace_logger.info("TRACE %s %s", record["event"], json.dumps(record, sort_keys=True))
