"""General critiquer node producing candidate findings."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.config import get_settings
from src.domain.schemas import CandidateFinding, CritiquerOutput, FocusedContextRequest, ReviewTask
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.orchestration.context.review_context import LazyReviewContextProvider
from src.orchestration.nodes.application.worker import ReviewTaskContext
from src.orchestration.prompts.renderer import render_reviewer_prompt

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
    normalized: List[CandidateFinding] = []
    for index, cand in enumerate(candidates, start=1):
        cid = cand.candidate_id.strip() or f"{task.id}:c{index}"
        if not cid.startswith(task.id):
            cid = f"{task.id}:{cid}"
        normalized.append(
            cand.model_copy(
                update={
                    "candidate_id": cid,
                    "patch_task_id": task.id,
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

        if use_llm:
            selected_model = model_key or getattr(get_settings(), "reviewer_worker_model_key", None)
            try:
                llm = Models.worker(CritiquerOutput, model_key=selected_model)
                response = llm.invoke(_render_critiquer_prompt(state, task, context.render()))
                candidates = _normalize_candidates(task=task, candidates=response.candidates)
                warnings.extend(response.warnings)
                summary = response.summary
                initial_requests = list(response.initial_focus_requests)
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

        metadata = dict(state.get("metadata", {}))
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
            "focused_context_requests": [],
            "task_status_by_id": {task.id: "completed"},
            "metadata": metadata,
            "node_history": [node_name],
        }

    return general_critiquer_node
