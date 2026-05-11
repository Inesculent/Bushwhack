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


def _render_critiquer_prompt(
    state: GraphState,
    task: ReviewTask,
    context_rendered: str,
    *,
    mental_model_excerpt: str = "",
    exploration_ledger_snippet: str = "",
) -> str:
    sections: Dict[str, str] = {
        "Assigned Task": (
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Task description: {task.description}\n"
            f"Target files: {task.target_files}"
        ),
        "Direct Context Gathered By Tools": context_rendered,
        "Git Diff Excerpt": (state.get("git_diff", "") or "")[:16000],
    }
    if mental_model_excerpt.strip():
        sections["Mental model excerpt (optional, pull-based)"] = mental_model_excerpt.strip()
    if exploration_ledger_snippet.strip():
        sections["Mental model query log (bounded)"] = exploration_ledger_snippet.strip()
    return render_reviewer_prompt("critiquer.md", sections)


def make_general_critiquer_node(
    context_provider: LazyReviewContextProvider,
    model_key: str | None = None,
    use_llm: bool = True,
    *,
    use_pipeline_cache: bool = False,
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
                "TRACE critiquer_start run_id=%s task_id=%s files=%s pipeline_cache=%s",
                run_id,
                task.id,
                task.target_files,
                use_pipeline_cache,
            )

        warnings: List[str] = []
        context_rendered = ""
        mental_model_excerpt = ""
        exploration_ledger_snippet = ""
        ast_files: List[str] = []

        if use_pipeline_cache:
            meta_raw = state.get("metadata", {}) or {}
            pipe = meta_raw.get("critique_pipeline", {}) or {}
            by_task = pipe.get("by_task", {}) or {}
            slot = by_task.get(task.id) if isinstance(by_task, dict) else None
            if isinstance(slot, dict) and slot.get("direct_context"):
                context_rendered = str(slot["direct_context"])
                mental_model_excerpt = str(slot.get("mental_model_excerpt") or "")
                warnings.extend(str(w) for w in (slot.get("warnings") or []) if w)
                raw_ast = slot.get("ast_included_files") or []
                if isinstance(raw_ast, list):
                    ast_files = [str(p) for p in raw_ast if isinstance(p, str) and p.strip()]
            else:
                ctx_fb = context_provider.collect_for_task(state=state, task=task)
                warnings.extend(ctx_fb.warnings)
                context_rendered = ctx_fb.render()
                ast_files = list(ctx_fb.ast_included_files)
        else:
            ctx0 = context_provider.collect_for_task(state=state, task=task)
            warnings.extend(ctx0.warnings)
            context_rendered = ctx0.render()
            ast_files = list(ctx0.ast_included_files)

        struct_excerpt = structural_critiquer_context_excerpt(state, task.target_files)
        if struct_excerpt:
            context_rendered = f"{context_rendered}\n\n{struct_excerpt}"

        ledger_rows = state.get("exploration_ledger") or []
        if ledger_rows:
            from src.orchestration.prompts.ledger_formatter import format_exploration_ledger_for_prompt

            snippet, stats = format_exploration_ledger_for_prompt(
                ledger_rows,
                task_id=task.id,
                target_files=task.target_files,
            )
            exploration_ledger_snippet = snippet
            metadata_for_metrics = dict(state.get("metadata", {}) or {})
            mm = dict(metadata_for_metrics.get("mental_model_metrics") or {})
            mm["ledger_formatter_rendered"] = int(mm.get("ledger_formatter_rendered", 0)) + stats.rendered
            mm["ledger_formatter_deduped"] = int(mm.get("ledger_formatter_deduped", 0)) + stats.deduped
            metadata_for_metrics["mental_model_metrics"] = mm
            state = {**state, "metadata": metadata_for_metrics}

        candidates: List[CandidateFinding] = []
        summary = ""
        initial_requests: List[FocusedContextRequest] = []
        if use_llm:
            selected_model = model_key or getattr(get_settings(), "reviewer_worker_model_key", None)
            try:
                llm = Models.worker(CritiquerOutput, model_key=selected_model)
                invoke_result = llm.invoke(
                    _render_critiquer_prompt(
                        state,
                        task,
                        context_rendered,
                        mental_model_excerpt=mental_model_excerpt,
                        exploration_ledger_snippet=exploration_ledger_snippet,
                    )
                )
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
        if ast_files:
            prev = metadata.get("ast_included_files")
            base = list(prev) if isinstance(prev, list) else []
            metadata["ast_included_files"] = sorted(
                {
                    p.strip().replace("\\", "/")
                    for p in base + ast_files
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

        integrity = dict(metadata.get("candidate_integrity", {}) or {})
        by_task = dict(integrity.get("by_task", {}) or {})
        by_task[task.id] = {
            "candidate_ids": [c.candidate_id for c in candidates],
            "candidate_count": len(candidates),
        }
        integrity["by_task"] = by_task
        metadata["candidate_integrity"] = integrity

        return {
            "candidate_findings": candidates,
            "focused_context_requests": initial_requests,
            "task_status_by_id": {task.id: "completed"},
            "metadata": metadata,
            "node_history": [node_name],
            "token_usage": llm_tokens,
        }

    return general_critiquer_node
