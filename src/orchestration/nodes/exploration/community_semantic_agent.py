"""Per-community semantic summarization (Phase 2)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from src.config import Settings, get_settings
from src.domain.schemas import (
    CommunityAgentOutput,
    CommunitySemanticSummary,
    CommunityWorkItem,
    FileSemanticSummary,
    SymbolSemanticSummary,
    UnverifiedCallTarget,
)
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.local_status import (
    is_local_model,
    is_timeout_exception,
    local_llm_server_active,
    sleep_for_retry,
    status_urls,
)
from src.infrastructure.llm.token_usage import extract_total_tokens_from_llm_result, parse_structured_output
from src.orchestration.prompts.exploration_prompts import render_community_semantic_prompt

logger = logging.getLogger(__name__)


def _is_local_model(model_key: str) -> bool:
    return is_local_model(model_key)


def _status_urls(base_url: str) -> List[str]:
    return status_urls(base_url)


def _is_timeout_exception(exc: Exception) -> bool:
    return is_timeout_exception(exc)


def _local_llm_server_active(settings: Settings) -> tuple[bool, str]:
    return local_llm_server_active(settings)


def _sleep_for_retry(settings: Settings, attempt: int, deadline: float | None = None) -> None:
    sleep_for_retry(settings.semantic_agent_retry_backoff_seconds, attempt, deadline)


def make_community_semantic_agent_node(
    *,
    settings: Settings | None = None,
    model_key: str | None = None,
    use_llm: bool = True,
):
    """Process a single ``CommunityWorkItem`` delivered via ``Send()``."""

    def community_semantic_agent_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        raw = state.get("semantic_community_work_item")
        if not raw:
            return {"node_history": ["community_semantic_agent:skipped"]}
        try:
            item = CommunityWorkItem.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("community_semantic_agent invalid work item run_id=%s err=%s", run_id, exc)
            return {"node_history": [f"community_semantic_agent:invalid:{exc.__class__.__name__}"]}

        resolved_settings = settings or get_settings()
        selected_model = model_key or resolved_settings.semantic_model_key
        repo_path = str(state.get("repo_path", "") or "")

        if not use_llm:
            stub = CommunitySemanticSummary(
                community_id=item.community_id,
                label="Stub Community",
                purpose="LLM disabled for tests.",
                file_summaries=[
                    FileSemanticSummary(
                        file_node_id=f"file:{p}",
                        purpose="Not analyzed (stub).",
                        key_symbols=[],
                        confidence=0.3,
                    )
                    for p in item.file_paths[:3]
                ],
                symbol_summaries=[],
                unverified_calls=[],
                cross_community_dependencies=sorted(set(item.target_communities_hint)),
                confidence=0.3,
            )
            return {
                "community_summaries": [stub],
                "node_history": [f"community_semantic_agent:{item.community_id}"],
                "metadata": {"semantic_phase2": {"community_stub": item.community_id}},
            }

        prompt = render_community_semantic_prompt(repo_path=repo_path, item=item)
        llm_tokens = 0
        warnings: List[str] = []
        attempts = 0
        last_exc: Exception | None = None
        max_attempts = resolved_settings.semantic_agent_max_retries + 1
        timeout_deadline = (
            time.monotonic() + resolved_settings.semantic_agent_timeout_patience_seconds
            if _is_local_model(selected_model) and resolved_settings.semantic_agent_timeout_patience_seconds > 0
            else None
        )
        attempt = 0
        while True:
            attempt += 1
            attempts = attempt
            try:
                llm = Models.worker(CommunityAgentOutput, model_key=selected_model)
                invoke_result = llm.invoke(prompt)
                parsed = parse_structured_output(invoke_result, CommunityAgentOutput)
                llm_tokens = extract_total_tokens_from_llm_result(invoke_result)
                warnings = [*warnings, *parsed.warnings]
                summary = parsed.summary
                summary = summary.model_copy(
                    update={
                        "community_id": item.community_id,
                        "cross_community_dependencies": sorted(
                            {c for c in item.target_communities_hint if c >= 0}
                        ),
                    }
                )
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                timeout_with_patience_left = (
                    _is_timeout_exception(exc)
                    and timeout_deadline is not None
                    and time.monotonic() < timeout_deadline
                )
                if timeout_with_patience_left:
                    server_active, status_detail = _local_llm_server_active(resolved_settings)
                    if server_active:
                        warnings.append("llm_timeout_server_active")
                        remaining = max(0, int(timeout_deadline - time.monotonic()))
                        logger.warning(
                            "community_semantic_agent LLM timeout but server active run_id=%s community=%s "
                            "attempt=%s patience_remaining_seconds=%s status=%s err=%s",
                            run_id,
                            item.community_id,
                            attempt,
                            remaining,
                            status_detail,
                            exc,
                        )
                        _sleep_for_retry(resolved_settings, attempt, timeout_deadline)
                        continue
                    logger.warning(
                        "community_semantic_agent LLM timeout and server status inactive run_id=%s "
                        "community=%s attempt=%s status=%s err=%s",
                        run_id,
                        item.community_id,
                        attempt,
                        status_detail,
                        exc,
                    )
                if attempt >= max_attempts:
                    break
                logger.warning(
                    "community_semantic_agent LLM retry run_id=%s community=%s attempt=%s/%s err=%s",
                    run_id,
                    item.community_id,
                    attempt,
                    max_attempts,
                    exc,
                )
                _sleep_for_retry(resolved_settings, attempt)

        if last_exc is not None:
            logger.warning(
                "community_semantic_agent LLM failure run_id=%s community=%s err=%s",
                run_id,
                item.community_id,
                last_exc,
            )
            warnings.append(f"llm_error:{last_exc.__class__.__name__}")
            summary = CommunitySemanticSummary(
                community_id=item.community_id,
                label="Degraded summary",
                purpose=f"Community agent failed: {last_exc.__class__.__name__}",
                file_summaries=[],
                symbol_summaries=[],
                unverified_calls=[],
                cross_community_dependencies=sorted({c for c in item.target_communities_hint if c >= 0}),
                confidence=0.1,
            )

        flat_targets: List[UnverifiedCallTarget] = []
        for call in summary.unverified_calls:
            flat_targets.append(
                call.model_copy(
                    update={
                        "source_community_id": item.community_id,
                    }
                )
            )

        meta = dict(state.get("metadata", {}))
        meta.setdefault("semantic_phase2", {})
        meta["semantic_phase2"][f"community_{item.community_id}"] = {
            "model": selected_model,
            "warnings": warnings,
            "tokens": llm_tokens,
            "attempts": attempts,
        }

        return {
            "community_summaries": [summary],
            "unverified_call_targets": flat_targets,
            "metadata": meta,
            "node_history": [f"community_semantic_agent:{item.community_id}"],
            "token_usage": llm_tokens,
        }

    return community_semantic_agent_node
