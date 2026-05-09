"""General critiquer node producing candidate findings."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.config import get_settings
from src.domain.schemas import CandidateFinding, CritiquerOutput, FocusedContextRequest, ReviewTask
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import extract_total_tokens_from_llm_result, parse_structured_output
from src.orchestration.context.review_context import (
    LazyReviewContextProvider,
    structural_critiquer_context_excerpt,
)
from src.orchestration.nodes.application.worker import ReviewTaskContext
from src.orchestration.prompts.renderer import render_reviewer_prompt
from src.orchestration.routing.critiquer_focus import auto_focus_requests
from src.orchestration.routing.normalize_critiquer_candidates import normalize_critiquer_candidates

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _task_from_state(state: GraphState) -> ReviewTask | None:
    task_id = state.get("current_task_id")
    registry = state.get("task_registry", {}) or {}
    if not task_id or task_id not in registry:
        return None
    return registry[task_id]


def _normalize_candidates(task: ReviewTask, candidates: List[CandidateFinding]) -> List[CandidateFinding]:
    return normalize_critiquer_candidates(task, candidates)


def _normalize_focus_requests(
    task: ReviewTask,
    candidates: List[CandidateFinding],
    requests: List[FocusedContextRequest],
) -> List[FocusedContextRequest]:
    candidate_ids = {candidate.candidate_id for candidate in candidates}
    candidate_id_aliases = {
        candidate.candidate_id.rsplit(":", 1)[-1]: candidate.candidate_id for candidate in candidates
    }
    fallback_candidate_id = candidates[0].candidate_id if candidates else task.id
    normalized: List[FocusedContextRequest] = []
    seen: set[str] = set()

    for index, request in enumerate(requests, start=1):
        request_id = request.request_id.strip() or f"{task.id}:focus:{index}"
        if request_id in seen:
            request_id = f"{request_id}:{index}"
        seen.add(request_id)
        candidate_id = request.candidate_id.strip()
        if candidate_id not in candidate_ids:
            candidate_id = candidate_id_aliases.get(candidate_id, fallback_candidate_id)
        normalized.append(
            request.model_copy(
                update={
                    "request_id": request_id,
                    "candidate_id": candidate_id,
                    "requested_by_specialty": request.requested_by_specialty or "general",
                }
            )
        )
    return normalized


def _render_critiquer_prompt(state: GraphState, task: ReviewTask, context_rendered: str) -> str:
    return render_reviewer_prompt(
        "critiquer.md",
        {
            "Assigned Task": (
                f"Task ID: {task.id}\n"
                f"Task title: {task.title}\n"
                f"Task description: {task.description}\n"
                f"Target files: {task.target_files}"
            ),
            "Direct Context Gathered By Tools": context_rendered,
            "Git Diff Excerpt": (state.get("git_diff", "") or "")[:16000],
        },
    )


def make_general_critiquer_node(
    context_provider: LazyReviewContextProvider,
    model_key: str | None = None,
    use_llm: bool = True,
):
    node_name = "general_critiquer"

    def general_critiquer_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        task = _task_from_state(state)
        if task is None:
            return {"node_history": [f"{node_name}:skipped"]}

        llm_tokens = 0

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE critiquer_start run_id=%s task_id=%s files=%s",
                run_id,
                task.id,
                task.target_files,
            )

        context: ReviewTaskContext = context_provider.collect_for_task(state=state, task=task)
        warnings: List[str] = list(context.warnings)
        candidates: List[CandidateFinding] = []
        summary = ""
        initial_requests: List[FocusedContextRequest] = []

        context_rendered = context.render()
        struct_excerpt = structural_critiquer_context_excerpt(state, task.target_files)
        if struct_excerpt:
            context_rendered = f"{context_rendered}\n\n{struct_excerpt}"

        if use_llm:
            selected_model = model_key or getattr(get_settings(), "reviewer_worker_model_key", None)
            try:
                llm = Models.worker(CritiquerOutput, model_key=selected_model)
                invoke_result = llm.invoke(_render_critiquer_prompt(state, task, context_rendered))
                response = parse_structured_output(invoke_result, CritiquerOutput)
                llm_tokens = extract_total_tokens_from_llm_result(invoke_result)
                candidates = _normalize_candidates(task=task, candidates=response.candidates)
                warnings.extend(response.warnings)
                summary = response.summary
                initial_requests = _normalize_focus_requests(
                    task=task,
                    candidates=candidates,
                    requests=list(response.initial_focus_requests) + auto_focus_requests(task, candidates),
                )
            except Exception as exc:  # noqa: BLE001
                warning = f"critiquer_llm_failed:{exc.__class__.__name__}: {exc}"
                warnings.append(warning)
                logger.warning(
                    "%s failed run_id=%s task_id=%s reason=%s: %s",
                    node_name,
                    run_id,
                    task.id,
                    exc.__class__.__name__,
                    exc,
                )

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE critiquer_done run_id=%s task_id=%s candidates=%s",
                run_id,
                task.id,
                len(candidates),
            )

        metadata = dict(state.get("metadata", {}) or {})
        if context.ast_included_files:
            prev = metadata.get("ast_included_files")
            base = list(prev) if isinstance(prev, list) else []
            metadata["ast_included_files"] = sorted(
                {
                    p.strip().replace("\\", "/")
                    for p in base + context.ast_included_files
                    if isinstance(p, str) and p.strip()
                }
            )
        crit_meta = dict(metadata.get("general_critiquer", {}) or {})
        crit_meta.setdefault("by_task", {})
        if isinstance(crit_meta["by_task"], dict):
            crit_meta["by_task"][task.id] = {
                "summary": summary,
                "candidate_count": len(candidates),
                "warnings": warnings,
                "initial_focus_requests": [r.model_dump() for r in initial_requests],
            }
        metadata["general_critiquer"] = crit_meta

        return {
            "candidate_findings": candidates,
            "focused_context_requests": initial_requests,
            "task_status_by_id": {task.id: "completed"},
            "metadata": metadata,
            "node_history": [node_name],
            "token_usage": llm_tokens,
        }

    return general_critiquer_node
