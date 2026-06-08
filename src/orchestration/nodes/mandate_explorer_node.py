"""ReAct-style mandate explorer (bootstrap + targeted modes)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from src.config import Settings, get_settings
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import trace_from_exception, trace_llm_call
from src.orchestration.context.mandate_loop_context import (
    already_observed_paths,
    explorer_mode,
    mandate_explorer_git_diff_cap,
    mm_meta,
)
from src.orchestration.context.review_context import LazyReviewContextProvider
from src.orchestration.nodes.application.planner import _target_files
from src.orchestration.prompts.renderer import render_reviewer_prompt
from src.tools.mandate_exploration_tools import (
    MandateToolExecutor,
    _community_digest_for_changed_files,
    tool_dedupe_key,
)

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")


class ExplorationRequestItem(BaseModel):
    file_path: str = Field(default="")
    symbol: str = Field(default="")
    question: str = Field(default="")


class MandateExplorerToolCall(BaseModel):
    tool: str = Field(description="Tool name from allowed set.")
    file_path: str = Field(default="")
    query: str = Field(default="")
    full_file: bool = Field(default=False)
    paths: List[str] = Field(default_factory=list)


class MandateExplorerStepOutput(BaseModel):
    action: Literal["tool", "finish"] = Field(description="tool or finish")
    tool_call: Optional[MandateExplorerToolCall] = None
    finish_summary: str = Field(default="", description="When action=finish, brief summary of findings.")


def _explorer_mode(state: GraphState) -> str:
    slot = mm_meta(state)[1]
    mode = slot.get("explorer_mode") or state.get("mandate_explorer_mode")
    return str(mode or "bootstrap")


def _tool_args_from_call(call: MandateExplorerToolCall) -> Dict[str, Any]:
    tool = call.tool.strip().lower()
    args: Dict[str, Any] = {}
    if call.file_path:
        args["file_path"] = call.file_path
    if call.query:
        args["query"] = call.query
    if call.full_file:
        args["full_file"] = True
    if call.paths:
        args["paths"] = call.paths
    return args


def _ledger_entry_from_tool(
    *,
    tool: str,
    args: Dict[str, Any],
    result: Dict[str, Any],
    mode: str,
    step: int,
) -> Dict[str, Any]:
    preview = str(result.get("text", ""))[:420]
    return {
        "kind": "mandate_tool_observation",
        "tool": tool,
        "args_preview": str(args)[:240],
        "result_preview": preview,
        "answer_preview": preview,
        "dedupe_key": result.get("dedupe_key") or tool_dedupe_key(tool, args),
        "caller": "mandate_explorer",
        "explorer_mode": mode,
        "step": step,
        "patch_seq_applied": 0,
        "cached": bool(result.get("cached")),
    }


def render_explorer_prompt(
    state: GraphState,
    *,
    retry_feedback: str = "",
) -> str:
    settings = get_settings()
    meta, slot = mm_meta(state)
    mode = _explorer_mode(state)
    intent = dict(slot.get("intent_extractor") or {})
    changed = "\n".join(f"- {f}" for f in _target_files(state)[:40])
    digest = _community_digest_for_changed_files(
        state,
        _target_files(state),
        max_chars=settings.reviewer_mandate_bootstrap_digest_max_chars * 3,
    )
    if mode == "bootstrap":
        mode_instructions = (
            "Bootstrap: survey changed files and diff-visible contracts. "
            "No draft review tasks exist yet. Use 3–6 tool calls then finish."
        )
        observed = "(n/a — bootstrap)"
        requests = "(n/a — bootstrap)"
    else:
        mode_instructions = (
            "Targeted: address critic exploration_requests only; "
            "do not repeat bootstrap file reads unless a request requires deeper detail."
        )
        observed = "\n".join(f"- {p}" for p in already_observed_paths(state)[:32]) or "(none)"
        reqs_raw = slot.get("exploration_requests") or []
        lines: List[str] = []
        for r in reqs_raw:
            if isinstance(r, dict):
                lines.append(
                    f"- file={r.get('file_path','')} symbol={r.get('symbol','')} "
                    f"Q: {r.get('question','')}"
                )
            else:
                lines.append(str(r))
        requests = "\n".join(lines) if lines else "(none)"

    return render_reviewer_prompt(
        "mental_model/mandate_explorer.md",
        {
            "explorer_mode": mode,
            "mode_instructions": mode_instructions,
            "intent_summary": str(intent.get("intent_summary", ""))[:3000],
            "changed_files": changed or "(none)",
            "git_diff_excerpt": (state.get("git_diff", "") or "")[
                : mandate_explorer_git_diff_cap(state)
            ],
            "community_digest": digest,
            "already_observed": observed,
            "exploration_requests": requests,
            "retry_feedback": retry_feedback or "(none)",
        },
    )


def run_explorer_step(
    state: GraphState,
    provider: LazyReviewContextProvider,
    *,
    settings: Settings | None = None,
    use_llm: bool = True,
) -> tuple[MandateExplorerStepOutput | None, int, List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
    """Single agent step; returns (parsed, tokens, ledger_patch, warnings, llm_trace)."""
    settings = settings or get_settings()
    warnings: List[str] = []
    ledger_patch: List[Dict[str, Any]] = []
    llm_trace: List[Dict[str, Any]] = []
    if not use_llm:
        return (
            MandateExplorerStepOutput(action="finish", finish_summary="LLM disabled."),
            0,
            ledger_patch,
            warnings,
            llm_trace,
        )
    meta, slot = mm_meta(state)
    step = int(state.get("mandate_explorer_step_idx") or 0) + 1
    retry = str(state.get("mandate_explorer_retry_feedback") or "")
    try:
        llm = Models.worker(MandateExplorerStepOutput, model_key=settings.reviewer_worker_model_key)
        prompt = render_explorer_prompt(state, retry_feedback=retry)
        traced = trace_llm_call(
            llm,
            prompt,
            state=state,
            node_name="mandate_explorer",
            model_key=settings.reviewer_worker_model_key,
            schema_name="MandateExplorerStepOutput",
            request_label=_explorer_mode(state),
            input_summary={"step": step, "mode": _explorer_mode(state)},
        )
        invoke_result = traced.result
        parsed = parse_structured_output(invoke_result, MandateExplorerStepOutput)
        tokens = traced.tokens
        llm_trace.extend(traced.trace_records)
    except Exception as exc:  # noqa: BLE001
        llm_trace.extend(trace_from_exception(exc))
        warnings.append(f"mandate_explorer_llm_failed:{exc.__class__.__name__}")
        logger.warning("mandate_explorer step failed: %s", exc)
        return None, 0, ledger_patch, warnings, llm_trace

    if parsed.action == "tool" and parsed.tool_call is not None:
        executor = MandateToolExecutor(provider, settings=settings)
        args = _tool_args_from_call(parsed.tool_call)
        result = executor.execute(state, parsed.tool_call.tool, args)
        mode = _explorer_mode(state)
        if not result.get("cached"):
            ledger_patch.append(
                _ledger_entry_from_tool(
                    tool=parsed.tool_call.tool,
                    args=args,
                    result=result,
                    mode=mode,
                    step=step,
                )
            )
    return parsed, tokens, ledger_patch, warnings, llm_trace


def make_mandate_explorer_node(
    context_provider: LazyReviewContextProvider,
    settings: Settings | None = None,
    *,
    use_llm: bool = True,
):
    """Graph node wrapping one explorer step (subgraph invokes multiple times)."""

    def mandate_explorer_node(state: GraphState) -> Dict[str, Any]:
        resolved = settings or get_settings()
        meta, slot = mm_meta(state)
        mode = _explorer_mode(state)
        loop = dict(slot.get("coupled_loop", {}) or {})
        inv = dict(loop.get("explorer_invocations", {}) or {})
        inv[mode] = int(inv.get(mode, 0)) + 1
        loop["explorer_invocations"] = inv

        parsed, tokens, ledger_patch, warnings, llm_trace = run_explorer_step(
            state, context_provider, settings=resolved, use_llm=use_llm
        )
        prev_idx = int(state.get("mandate_explorer_step_idx") or 0)
        step_idx = prev_idx + 1
        finish = parsed is not None and parsed.action == "finish"
        last_summary = (parsed.finish_summary if parsed else "")[:500]

        mode_key = str(state.get("mandate_explorer_mode") or explorer_mode(state))
        if mode_key == "targeted":
            budget = max(1, int(resolved.reviewer_mandate_targeted_max_steps))
        else:
            budget = max(1, int(resolved.reviewer_mandate_bootstrap_max_steps))
        if step_idx >= budget:
            finish = True
            if not last_summary:
                last_summary = f"step budget ({budget}) reached"

        slot["coupled_loop"] = loop
        slot["explorer_run"] = {
            "mode": mode,
            "step_idx": step_idx,
            "finished": finish,
            "last_summary": last_summary,
        }
        if mode == "bootstrap" and finish:
            slot["bootstrap_completed"] = True
        meta["mental_model"] = slot

        if meta.get("review_trace_enabled"):
            trace_logger.info(
                "TRACE mandate_explorer run_id=%s mode=%s step=%s finish=%s ledger_add=%s",
                state.get("run_id"),
                mode,
                step_idx,
                finish,
                len(ledger_patch),
            )

        out: Dict[str, Any] = {
            "metadata": meta,
            "mandate_explorer_step_idx": step_idx,
            "mandate_explorer_finished": finish,
            "mandate_explorer_last_summary": last_summary,
            "node_history": [f"mandate_explorer:{mode}:{step_idx}"],
            "token_usage": tokens,
            "llm_trace": llm_trace,
        }
        if ledger_patch:
            out["exploration_ledger"] = ledger_patch
        if warnings:
            loop["warnings"] = list(loop.get("warnings", [])) + warnings
            slot["coupled_loop"] = loop
            meta["mental_model"] = slot
            out["metadata"] = meta
        return out

    return mandate_explorer_node


def make_mandate_explorer_bootstrap_node(
    context_provider: LazyReviewContextProvider,
    settings: Settings | None = None,
    *,
    use_llm: bool = True,
):
    """Run full bootstrap explorer subgraph."""

    def node(state: GraphState) -> Dict[str, Any]:
        from src.orchestration.mandate_explorer_graph import run_mandate_explorer_subgraph

        if not use_llm:
            meta = dict(state.get("metadata", {}) or {})
            slot = dict(meta.get("mental_model", {}) or {})
            slot["bootstrap_completed"] = True
            meta["mental_model"] = slot
            return {"metadata": meta, "node_history": ["mandate_explorer_bootstrap:skipped"]}

        result = run_mandate_explorer_subgraph(state, context_provider, mode="bootstrap")
        merged: Dict[str, Any] = {"node_history": ["mandate_explorer_bootstrap"]}
        merged.update(result)
        return merged

    return node


def make_mandate_explorer_targeted_node(
    context_provider: LazyReviewContextProvider,
    settings: Settings | None = None,
    *,
    use_llm: bool = True,
):
    """Run targeted explorer after joint critic requests."""

    def node(state: GraphState) -> Dict[str, Any]:
        from src.orchestration.mandate_explorer_graph import run_mandate_explorer_subgraph

        meta = dict(state.get("metadata", {}) or {})
        slot = dict(meta.get("mental_model", {}) or {})
        ac = dict(meta.get("actor_critic_planner") or {})
        slot["exploration_requests"] = list(ac.get("exploration_requests") or [])
        slot["explorer_mode"] = "targeted"
        meta["mental_model"] = slot
        inner = {**state, "metadata": meta}

        if not use_llm:
            return {"metadata": meta, "node_history": ["mandate_explorer_targeted:skipped"]}

        result = run_mandate_explorer_subgraph(inner, context_provider, mode="targeted")
        merged: Dict[str, Any] = {"node_history": ["mandate_explorer_targeted"]}
        merged.update(result)
        return merged

    return node
