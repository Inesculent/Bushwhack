"""Actor-critic planning: draft tasks, critique vs BehavioralSpec, revise, emit canonical planner state."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from src.config import Settings, get_settings
from src.domain.schemas import ReviewTask
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import append_trace, trace_from_exception, trace_llm_call
from src.orchestration.nodes.application.planner import (
    ReviewPlanOutput,
    _normalize_tasks,
    _trace_enabled,
    build_planner_state_update,
    finalize_emitted_tasks,
    run_planner_generation,
    validate_surface_bound_plan,
)
from src.orchestration.context.context_packets import (
    build_plan_critic_packet,
    build_plan_revision_packet,
    packet_to_prompt_sections,
)
from src.orchestration.prompts.renderer import render_reviewer_prompt
from src.orchestration.routing.mandate_plan_coupling import JointPlanCritiqueOutput

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")


def make_draft_planner_node(settings: Settings | None = None, *, use_llm: bool = True):
    node_name = "draft_planner"

    def draft_planner_node(state: GraphState) -> Dict[str, Any]:
        resolved = settings or get_settings()
        tasks, summary, warnings, llm_tokens, llm_trace = run_planner_generation(
            state, model_key=resolved.reviewer_planner_model_key, use_llm=use_llm
        )
        meta = dict(state.get("metadata", {}) or {})
        ac = dict(meta.get("actor_critic_planner") or {})
        ac.update(
            {
                "phase": "draft",
                "summary": summary,
                "warnings": list(warnings),
                "draft_tasks": [t.model_dump(mode="json") for t in tasks],
                "revision_count": 0,
                "aligned": False,
            }
        )
        meta["actor_critic_planner"] = ac
        if _trace_enabled(state):
            trace_logger.info("TRACE %s run_id=%s tasks=%s", node_name, state.get("run_id"), len(tasks))
        return {
            "metadata": meta,
            "node_history": [node_name],
            "token_usage": llm_tokens,
            "llm_trace": llm_trace,
        }

    return draft_planner_node


def make_plan_critic_node(settings: Settings | None = None, *, use_llm: bool = True):
    node_name = "plan_critic"

    def plan_critic_node(state: GraphState) -> Dict[str, Any]:
        resolved = settings or get_settings()
        meta = dict(state.get("metadata", {}) or {})
        ac = dict(meta.get("actor_critic_planner") or {})
        draft_raw = ac.get("draft_tasks") or []
        draft_tasks = [ReviewTask.model_validate(row) for row in draft_raw]
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []
        aligned = True
        gaps = ""
        rev_instr = ""
        warnings: List[str] = list(ac.get("warnings") or [])

        exploration_requests: List[Dict[str, Any]] = []
        mandate_adequate = True

        if use_llm and draft_tasks:
            try:
                critic_packet = build_plan_critic_packet(state, draft_tasks, settings=resolved)
                prompt = render_reviewer_prompt(
                    "mental_model/joint_plan_critic.md",
                    packet_to_prompt_sections(critic_packet),
                )
                llm = Models.worker(JointPlanCritiqueOutput, model_key=resolved.reviewer_planner_model_key)
                traced = trace_llm_call(
                    llm,
                    prompt,
                    state=state,
                    node_name=node_name,
                    model_key=resolved.reviewer_planner_model_key,
                    schema_name="JointPlanCritiqueOutput",
                    input_summary={"draft_task_count": len(draft_tasks)},
                )
                invoke_result = traced.result
                out = parse_structured_output(invoke_result, JointPlanCritiqueOutput)
                llm_tokens = traced.tokens
                llm_trace = append_trace(llm_trace, traced)
                aligned = bool(out.aligned)
                gaps = out.gaps.strip()
                rev_instr = out.revision_instructions.strip()
                mandate_adequate = bool(out.mandate_adequate)
                exploration_requests = [
                    r.model_dump(mode="json") for r in (out.exploration_requests or [])
                ]
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}")
                logger.warning("%s LLM fallback aligned=True: %s", node_name, exc)
                aligned = True

        ac["aligned"] = aligned
        ac["mandate_adequate"] = mandate_adequate
        ac["exploration_requests"] = exploration_requests
        ac["last_critique"] = {"gaps": gaps, "revision_instructions": rev_instr}
        ac["phase"] = "critic"
        ac["warnings"] = warnings
        meta["actor_critic_planner"] = ac
        meta.setdefault("mental_model_metrics", {})
        mm = dict(meta.get("mental_model_metrics") or {})
        mm["plan_critic_aligned"] = aligned
        meta["mental_model_metrics"] = mm
        return {"metadata": meta, "node_history": [node_name], "token_usage": llm_tokens, "llm_trace": llm_trace}

    return plan_critic_node


def make_plan_revision_node(settings: Settings | None = None, *, use_llm: bool = True):
    node_name = "plan_revision"

    def plan_revision_node(state: GraphState) -> Dict[str, Any]:
        resolved = settings or get_settings()
        meta = dict(state.get("metadata", {}) or {})
        ac = dict(meta.get("actor_critic_planner") or {})
        draft_raw = ac.get("draft_tasks") or []
        draft_tasks = [ReviewTask.model_validate(row) for row in draft_raw]
        critique = dict(ac.get("last_critique") or {})
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []
        warnings: List[str] = list(ac.get("warnings") or [])
        new_tasks = draft_tasks

        if use_llm:
            try:
                revision_packet = build_plan_revision_packet(
                    state, draft_tasks, critique, settings=resolved
                )
                sections_map = packet_to_prompt_sections(revision_packet)
                prompt = render_reviewer_prompt(
                    "mental_model/plan_revision.md",
                    {
                        "Behavioral mandate excerpt": sections_map.get(
                            "behavioral_mandate_excerpt",
                            sections_map.get("Behavioral mandate excerpt", ""),
                        ),
                        "Current tasks JSON": sections_map.get(
                            "Current tasks JSON",
                            json.dumps([t.model_dump() for t in draft_tasks], indent=2)[:12000],
                        ),
                        "Critique gaps": sections_map.get("Critique gaps", str(critique.get("gaps", ""))),
                        "Revision instructions": sections_map.get(
                            "Revision instructions",
                            str(critique.get("revision_instructions", "")),
                        ),
                    },
                )
                llm = Models.planner(ReviewPlanOutput, model_key=resolved.reviewer_planner_model_key)
                traced = trace_llm_call(
                    llm,
                    prompt,
                    state=state,
                    node_name=node_name,
                    model_key=resolved.reviewer_planner_model_key,
                    schema_name="ReviewPlanOutput",
                    input_summary={"draft_task_count": len(draft_tasks)},
                )
                invoke_result = traced.result
                response = parse_structured_output(invoke_result, ReviewPlanOutput)
                llm_tokens = traced.tokens
                llm_trace = append_trace(llm_trace, traced)
                new_tasks = _normalize_tasks(response.tasks, state)
                ac["summary"] = response.summary or ac.get("summary", "")
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}")
                logger.warning("%s LLM fallback keeping draft: %s", node_name, exc)

        rc = int(ac.get("revision_count", 0)) + 1
        ac["draft_tasks"] = [t.model_dump(mode="json") for t in new_tasks]
        ac["revision_count"] = rc
        ac["phase"] = "revision"
        ac["warnings"] = warnings
        meta["actor_critic_planner"] = ac
        from src.orchestration.routing.mandate_plan_coupling import increment_coupled_cycle

        cycle_update = increment_coupled_cycle({**state, "metadata": meta})
        meta = cycle_update.get("metadata", meta)
        return {"metadata": meta, "node_history": [node_name], "token_usage": llm_tokens, "llm_trace": llm_trace}

    return plan_revision_node


def make_mandate_finalize_node(settings: Settings | None = None, *, use_llm: bool = True):
    """Optional polish via mandate_synthesizer when spec is still thin."""

    node_name = "mandate_finalize"

    def mandate_finalize_node(state: GraphState) -> Dict[str, Any]:
        from src.orchestration.nodes.mental_model import make_mandate_synthesizer_node

        meta = dict(state.get("metadata", {}) or {})
        slot = dict(meta.get("mental_model", {}) or {})
        loop = dict(slot.get("coupled_loop", {}) or {})
        loop["last_route"] = "mandate_finalize"
        slot["coupled_loop"] = loop
        meta["mental_model"] = slot
        patch_seq = int(slot.get("patch_seq", 0))
        if patch_seq <= 1 and use_llm:
            synth = make_mandate_synthesizer_node(settings, use_llm=use_llm)
            out = synth({**state, "metadata": meta})
            out["node_history"] = [node_name]
            return out
        return {"metadata": meta, "node_history": [f"{node_name}:skipped"]}

    return mandate_finalize_node


def make_plan_emit_node():
    node_name = "plan_emit"

    def blocked_update(
        state: GraphState,
        *,
        ac: Dict[str, Any],
        warnings: List[str],
        reason: str,
        plan_validation: Dict[str, Any],
    ) -> Dict[str, Any]:
        diagnostics = {
            **plan_validation,
            "blocked": True,
            "blocked_reason": reason,
            "critic_gaps": dict(ac.get("last_critique") or {}),
            "revision_count": int(ac.get("revision_count", 0)),
            "plan_critic_aligned": ac.get("aligned"),
        }
        out = build_planner_state_update(
            state,
            [],
            "Review planning blocked before execution; see metadata.review_planner.plan_validation.",
            [*warnings, reason],
            0,
            [],
            node_history_name=node_name,
            metadata_extra={
                "blocked": True,
                "blocked_reason": reason,
                "plan_validation": diagnostics,
            },
        )
        out["next_step"] = "blocked"
        meta2 = dict(out["metadata"])
        meta2["actor_critic_review"] = {
            "revision_count": ac.get("revision_count", 0),
            "aligned": ac.get("aligned"),
            "blocked": True,
            "blocked_reason": reason,
        }
        out["metadata"] = meta2
        return out

    def plan_emit_node(state: GraphState) -> Dict[str, Any]:
        meta = dict(state.get("metadata", {}) or {})
        ac = dict(meta.get("actor_critic_planner") or {})
        draft_raw = ac.get("draft_tasks") or []
        tasks = [ReviewTask.model_validate(row) for row in draft_raw] or []
        tasks = finalize_emitted_tasks(tasks, state)
        summary = str(ac.get("summary") or "Actor-critic review plan.")
        warnings = [str(w) for w in (ac.get("warnings") or []) if w]
        plan_validation = validate_surface_bound_plan(tasks, state)
        resolved = get_settings()
        loop = dict((meta.get("mental_model") or {}).get("coupled_loop") or {})
        critic_exhausted = bool(ac.get("aligned") is False) and (
            int(ac.get("revision_count", 0)) >= int(resolved.reviewer_actor_critic_max_plan_revisions)
            or int(loop.get("cycles", 0)) >= int(resolved.reviewer_mandate_plan_max_cycles)
        )
        blocked = critic_exhausted or not bool(plan_validation.get("ok"))
        if blocked:
            reason = "plan_critic_misaligned_after_budget" if critic_exhausted else "surface_plan_validation_failed"
            return blocked_update(
                state,
                ac=ac,
                warnings=warnings,
                reason=reason,
                plan_validation=plan_validation,
            )
        if not tasks:
            tasks, summary, warn2, _t, _trace = run_planner_generation(state, use_llm=False)
            warnings.extend(warn2)
            plan_validation = validate_surface_bound_plan(tasks, state)
            if not bool(plan_validation.get("ok")):
                return blocked_update(
                    state,
                    ac=ac,
                    warnings=warnings,
                    reason="surface_plan_validation_failed",
                    plan_validation=plan_validation,
                )
        out = build_planner_state_update(
            state,
            tasks,
            summary,
            warnings,
            0,
            [],
            node_history_name=node_name,
            metadata_extra={"plan_validation": plan_validation},
        )
        meta2 = dict(out["metadata"])
        ac_done = dict(meta.get("actor_critic_planner") or {})
        meta2["actor_critic_review"] = {
            "revision_count": ac_done.get("revision_count", 0),
            "aligned": ac_done.get("aligned"),
        }
        out["metadata"] = meta2
        return out

    return plan_emit_node


def route_plan_critic(state: GraphState) -> str:
    """Coupled mandate-plan routing after joint critic."""
    from src.orchestration.routing.mandate_plan_coupling import route_joint_critic

    return route_joint_critic(state)
