"""General critiquer node producing candidate findings."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.config import get_settings
from src.domain.schemas import CandidateFinding, CritiquerOutput, FocusedContextRequest, ReviewTask
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import (
    parse_structured_output,
    salvage_structured_output_from_raw,
)
from src.infrastructure.llm.trace import append_trace, trace_from_exception, trace_llm_call
from src.orchestration.context.focus_request_scope import (
    allowed_review_paths,
    clamp_focused_context_request,
)
from src.orchestration.context.context_packets import (
    build_critiquer_packet,
    packet_to_prompt_sections,
)
from src.orchestration.prompts.renderer import render_reviewer_prompt
from src.orchestration.routing.critiquer_focus import auto_focus_requests
from src.orchestration.routing.normalize_critiquer_candidates import normalize_critiquer_candidates
from src.orchestration.routing.review_obligations import (
    derive_review_obligations,
    evaluate_review_obligations,
)

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


def _normalize_candidates(
    task: ReviewTask,
    candidates: List[CandidateFinding],
    *,
    pipeline_slot: dict | None = None,
    git_diff: str = "",
) -> List[CandidateFinding]:
    file_contents = None
    if pipeline_slot:
        te = pipeline_slot.get("task_evidence")
        if isinstance(te, dict):
            raw = te.get("file_contents")
            if isinstance(raw, dict):
                file_contents = raw
    normalized, anchor_warnings, duplicate_map = normalize_critiquer_candidates(
        task,
        candidates,
        file_contents=file_contents,
        git_diff=git_diff,
    )
    if duplicate_map:
        slot = pipeline_slot or {}
        slot["semantic_dedupe_duplicates"] = duplicate_map
    if anchor_warnings:
        slot = pipeline_slot or {}
        existing = slot.get("line_anchor_warnings")
        if isinstance(existing, list):
            existing.extend(anchor_warnings)
        else:
            slot["line_anchor_warnings"] = list(anchor_warnings)
    return normalized


def _normalize_focus_requests(
    state: GraphState,
    task: ReviewTask,
    candidates: List[CandidateFinding],
    requests: List[FocusedContextRequest],
) -> List[FocusedContextRequest]:
    candidate_ids = {candidate.candidate_id for candidate in candidates}
    candidate_id_aliases = {
        candidate.candidate_id.rsplit(":", 1)[-1]: candidate.candidate_id for candidate in candidates
    }
    fallback_candidate_id = candidates[0].candidate_id if candidates else task.id
    candidates_by_id = {c.candidate_id: c for c in candidates}
    scope = allowed_review_paths(state, task_target_files=task.target_files)
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
        cand = candidates_by_id.get(candidate_id)
        scoped = clamp_focused_context_request(
            request,
            scope,
            fallback_path=(cand.file_path if cand else None) or (task.target_files[0] if task.target_files else None),
        )
        normalized.append(
            scoped.model_copy(
                update={
                    "request_id": request_id,
                    "candidate_id": candidate_id,
                    "requested_by_specialty": scoped.requested_by_specialty or "general",
                }
            )
        )
    return normalized


def _render_critiquer_prompt(
    state: GraphState,
    task: ReviewTask,
    pipeline_slot: Dict[str, Any],
) -> str:
    packet = build_critiquer_packet(state, task, pipeline_slot)
    return render_reviewer_prompt("critiquer.md", packet_to_prompt_sections(packet))


_COMPACT_OUTPUT_APPENDIX = (
    "\n\n## OUTPUT BUDGET (retry — required)\n"
    "Your previous response exceeded the length limit. Return at most 6 candidates. "
    "Keep each content, evidence_summary, and failure_mode under 400 characters. "
    "Keep summary under 500 characters. No prose outside the schema fields."
)
def _is_length_finish_error(exc: Exception) -> bool:
    if "LengthFinish" in exc.__class__.__name__:
        return True
    msg = str(exc).lower()
    return "length limit" in msg or "length_finish" in msg


def _invoke_critiquer_llm(
    *,
    state: GraphState,
    task: ReviewTask,
    pipeline_slot: Dict[str, Any],
    model_key: str | None,
    compact: bool,
    appendix: str = "",
) -> tuple[Any, int, List[Dict[str, Any]]]:
    settings = get_settings()
    prompt = _render_critiquer_prompt(state, task, pipeline_slot)
    if compact:
        prompt = f"{prompt}{_COMPACT_OUTPUT_APPENDIX}"
    if appendix:
        prompt = f"{prompt}{appendix}"
    llm = Models.worker(
        CritiquerOutput,
        model_key=model_key,
        max_completion_tokens=settings.reviewer_critiquer_max_completion_tokens,
    )
    label = "compact" if compact else "primary"
    if appendix:
        label = f"{label}+appendix"
    traced = trace_llm_call(
        llm,
        prompt,
        state=state,
        node_name="general_critiquer",
        model_key=model_key,
        schema_name="CritiquerOutput",
        request_label=label,
        input_summary={
            "task_id": task.id,
            "specialty": task.specialty,
            "target_files": task.target_files,
        },
    )
    return traced.result, traced.tokens, traced.trace_records


def _needs_orthogonal_recall(task: ReviewTask, response: CritiquerOutput) -> bool:
    return False


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
        llm_trace: List[Dict[str, Any]] = []

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE critiquer_start run_id=%s task_id=%s files=%s pipeline_cache=%s",
                run_id,
                task.id,
                task.target_files,
                use_pipeline_cache,
            )

        warnings: List[str] = []
        pipeline_slot: Dict[str, Any] = {}
        ast_files: List[str] = []

        meta_raw = state.get("metadata", {}) or {}
        pipe = meta_raw.get("critique_pipeline", {}) or {}
        by_task = pipe.get("by_task", {}) or {}
        slot = by_task.get(task.id) if isinstance(by_task, dict) else None
        if isinstance(slot, dict) and (
            slot.get("direct_context") or slot.get("context_packet")
        ):
            pipeline_slot = dict(slot)
            warnings.extend(str(w) for w in (slot.get("warnings") or []) if w)
            raw_ast = slot.get("ast_included_files") or []
            if isinstance(raw_ast, list):
                ast_files = [str(p) for p in raw_ast if isinstance(p, str) and p.strip()]
        elif use_pipeline_cache:
            warnings.append("critiquer_missing_pipeline_slot")
        else:
            from src.orchestration.context.context_packets import (
                build_critique_packet,
                packet_to_storage_dict,
                probe_direct_context_for_task,
            )
            from src.orchestration.context.task_evidence import build_task_evidence

            ctx0 = context_provider.collect_for_critique(state=state, task=task)
            warnings.extend(ctx0.warnings)
            bundle0 = build_task_evidence(state, task, context_provider, ctx0)
            probe_packet = build_critique_packet(
                state,
                task,
                ctx0,
                provider=context_provider,
                code_evidence=bundle0.rendered,
                evidence_metadata=bundle0.to_storage_dict(),
            )
            stored = packet_to_storage_dict(probe_packet)
            code_fb, fb_warn = probe_direct_context_for_task(stored)
            if bundle0.rendered.strip():
                code_fb = bundle0.rendered
            warnings.extend(fb_warn)
            pipeline_slot = {
                "context_packet": stored,
                "task_evidence": bundle0.to_storage_dict(),
                "direct_context": code_fb,
            }
            pipeline_slot["coverage_obligations"] = derive_review_obligations(
                task,
                pipeline_slot["task_evidence"],
            )
            ast_files = list(ctx0.ast_included_files)

        candidates: List[CandidateFinding] = []
        summary = ""
        initial_requests: List[FocusedContextRequest] = []
        audit_coverage: List[dict[str, Any]] = []
        if use_llm:
            selected_model = model_key or getattr(get_settings(), "reviewer_worker_model_key", None)
            invoke_result: Any = None
            try:
                invoke_result, llm_tokens, call_trace = _invoke_critiquer_llm(
                    state=state,
                    task=task,
                    pipeline_slot=pipeline_slot,
                    model_key=selected_model,
                    compact=False,
                )
                llm_trace.extend(call_trace)
                try:
                    response = parse_structured_output(invoke_result, CritiquerOutput)
                except (ValueError, TypeError) as parse_exc:
                    if _is_length_finish_error(parse_exc) or (
                        isinstance(invoke_result, dict) and invoke_result.get("parsed") is None
                    ):
                        salvaged = salvage_structured_output_from_raw(invoke_result, CritiquerOutput)
                        if salvaged is not None:
                            warnings.append("critiquer_llm_salvaged:partial_json_from_raw")
                            response = salvaged
                        else:
                            raise parse_exc
                    else:
                        raise
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                if _is_length_finish_error(exc) or (
                    isinstance(exc, (ValueError, TypeError))
                    and invoke_result is not None
                    and isinstance(invoke_result, dict)
                    and invoke_result.get("parsed") is None
                ):
                    try:
                        warnings.append("critiquer_llm_retry:reason=length")
                        invoke_result, retry_tokens, call_trace = _invoke_critiquer_llm(
                            state=state,
                            task=task,
                            pipeline_slot=pipeline_slot,
                            model_key=selected_model,
                            compact=True,
                        )
                        llm_tokens += retry_tokens
                        llm_trace.extend(call_trace)
                        response = parse_structured_output(invoke_result, CritiquerOutput)
                        candidates = _normalize_candidates(
                            task=task,
                            candidates=response.candidates,
                            pipeline_slot=pipeline_slot,
                            git_diff=state.get("git_diff", "") or "",
                        )
                        warnings.extend(response.warnings)
                        summary = response.summary
                        audit_coverage = [
                            row.model_dump() if hasattr(row, "model_dump") else dict(row)
                            for row in response.audit_coverage
                        ]
                        initial_requests = _normalize_focus_requests(
                            state,
                            task,
                            candidates,
                            list(response.initial_focus_requests)
                            + auto_focus_requests(task, candidates),
                        )
                    except Exception as retry_exc:  # noqa: BLE001
                        llm_trace.extend(trace_from_exception(retry_exc))
                        exc = retry_exc
                        warning = f"critiquer_llm_failed:{exc.__class__.__name__}: {exc}"
                        warnings.append(warning)
                        logger.warning(
                            "%s failed after length retry run_id=%s task_id=%s reason=%s: %s",
                            node_name,
                            run_id,
                            task.id,
                            exc.__class__.__name__,
                            exc,
                        )
                else:
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
            else:
                candidates = _normalize_candidates(
                    task=task,
                    candidates=response.candidates,
                    pipeline_slot=pipeline_slot,
                    git_diff=state.get("git_diff", "") or "",
                )
                warnings.extend(response.warnings)
                summary = response.summary
                audit_coverage = [
                    row.model_dump() if hasattr(row, "model_dump") else dict(row)
                    for row in response.audit_coverage
                ]
                initial_requests = _normalize_focus_requests(
                    state,
                    task,
                    candidates,
                    list(response.initial_focus_requests) + auto_focus_requests(task, candidates),
                )

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE critiquer_done run_id=%s task_id=%s candidates=%s",
                run_id,
                task.id,
                len(candidates),
            )

        metadata = dict(state.get("metadata", {}) or {})
        coverage_eval = evaluate_review_obligations(
            pipeline_slot.get("coverage_obligations") or [],
            candidates,
            audit_coverage,
        )
        warnings.extend(coverage_eval.get("warnings") or [])
        if coverage_eval.get("obligations"):
            pipe_meta = dict(metadata.get("critique_pipeline", {}) or {})
            by_task_meta = dict(pipe_meta.get("by_task", {}) or {})
            slot_meta = dict(by_task_meta.get(task.id, {}) or {})
            slot_meta["coverage_evaluation"] = coverage_eval
            by_task_meta[task.id] = slot_meta
            pipe_meta["by_task"] = by_task_meta
            metadata["critique_pipeline"] = pipe_meta
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
            task_meta = {
                "summary": summary,
                "candidate_count": len(candidates),
                "warnings": warnings,
                "initial_focus_requests": [r.model_dump() for r in initial_requests],
                "audit_coverage": audit_coverage,
                "coverage_obligations": coverage_eval,
            }
            anchor_warn = pipeline_slot.get("line_anchor_warnings")
            if isinstance(anchor_warn, list) and anchor_warn:
                task_meta["line_anchor_warnings"] = list(anchor_warn)
                warnings.extend(anchor_warn)
            crit_meta["by_task"][task.id] = task_meta
        metadata["general_critiquer"] = crit_meta

        integrity = dict(metadata.get("candidate_integrity", {}) or {})
        by_task = dict(integrity.get("by_task", {}) or {})
        integrity_ids = [c.candidate_id for c in candidates]
        slot = pipeline_slot or {}
        dup_map = slot.get("semantic_dedupe_duplicates")
        if isinstance(dup_map, dict):
            for dropped in dup_map.values():
                if isinstance(dropped, list):
                    integrity_ids.extend(str(cid) for cid in dropped if cid)
        by_task[task.id] = {
            "candidate_ids": sorted(set(integrity_ids)),
            "candidate_count": len(candidates),
            "coverage_counts": coverage_eval.get("counts", {}),
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
            "llm_trace": llm_trace,
        }

    return general_critiquer_node
