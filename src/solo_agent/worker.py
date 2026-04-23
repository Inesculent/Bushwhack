"""Solo-agent worker node: one-shot ACR without code context.

Builds the tagged prompt from ``state["metadata"]["pr_title"]`` /
``state["metadata"]["pr_description"]`` and ``state["git_diff"]``, calls a raw
chat model (deliberately NOT ``Models.worker(schema)`` — the prompt produces
free-form tagged output, not JSON), then emits both the raw transcript and the
parsed ``ReviewFinding``s via the GraphState patch.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from src.config import get_settings
from src.domain.schemas import ReviewFinding
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.solo_agent.parser import new_finding_prefix, parse_solo_response
from src.solo_agent.prompt import PROMPT_VERSION, render

logger = logging.getLogger(__name__)

EXPERIMENT_TAG = "solo_agent"
NODE_NAME = "solo_worker"


def _extract_text(response: Any) -> str:
    """Best-effort extraction of a string body from a LangChain chat response."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(response)


def _extract_token_usage(response: Any) -> int:
    """Pull an integer total-token count off common LangChain response shapes."""
    metadata = getattr(response, "usage_metadata", None)
    if isinstance(metadata, dict):
        total = metadata.get("total_tokens")
        if isinstance(total, int):
            return total
    response_metadata = getattr(response, "response_metadata", None)
    if isinstance(response_metadata, dict):
        token_usage = response_metadata.get("token_usage") or response_metadata.get("usage")
        if isinstance(token_usage, dict):
            total = token_usage.get("total_tokens")
            if isinstance(total, int):
                return total
    return 0


def worker_node(state: GraphState) -> Dict[str, Any]:
    settings = get_settings()

    run_id = state.get("run_id", "")
    existing_metadata: Dict[str, Any] = dict(state.get("metadata", {}))
    pr_title = str(existing_metadata.get("pr_title", ""))
    pr_description = str(existing_metadata.get("pr_description", ""))
    diff_hunk = state.get("git_diff", "") or ""

    rendered = render(
        pr_title=pr_title,
        pr_description=pr_description,
        diff_hunk=diff_hunk,
        max_diff_chars=settings.solo_agent_max_diff_chars,
    )

    model_key = settings.solo_agent_model_key or Models.DEFAULT_ROLE_MODELS["worker"]
    llm = Models.get(model_key)

    logger.info(
        "solo_worker invoking model run_id=%s model=%s prompt_version=%s diff_truncated=%s diff_chars_dropped=%s",
        run_id,
        model_key,
        PROMPT_VERSION,
        rendered.diff_truncated,
        rendered.diff_chars_dropped,
    )

    start = time.perf_counter()
    response = llm.invoke(rendered.text)
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    raw_text = _extract_text(response)
    token_usage = _extract_token_usage(response)

    prefix = new_finding_prefix(run_id=run_id or None)
    parse_result = parse_solo_response(
        response_text=raw_text,
        finding_id_prefix=prefix,
        default_file_path=str(existing_metadata.get("pr_path", "unknown")) or "unknown",
    )

    findings: List[ReviewFinding] = parse_result.findings

    metadata_patch: Dict[str, Any] = dict(existing_metadata)
    metadata_patch.update(
        {
            "experiment": EXPERIMENT_TAG,
            "prompt_version": PROMPT_VERSION,
            "solo_agent_model": model_key,
            "solo_agent_elapsed_ms": elapsed_ms,
            "solo_agent_diff_truncated": rendered.diff_truncated,
            "solo_agent_diff_chars_dropped": rendered.diff_chars_dropped,
            "solo_agent_raw_response": raw_text,
            "solo_agent_parse_warnings": parse_result.warnings,
            "solo_agent_had_end_tag": parse_result.had_end_tag,
            "solo_agent_finding_count": len(findings),
        }
    )

    logger.info(
        "solo_worker complete run_id=%s findings=%s warnings=%s had_end_tag=%s elapsed_ms=%s tokens=%s",
        run_id,
        len(findings),
        len(parse_result.warnings),
        parse_result.had_end_tag,
        elapsed_ms,
        token_usage,
    )

    return {
        "findings": findings,
        "metadata": metadata_patch,
        "node_history": [NODE_NAME],
        "token_usage": token_usage,
    }
