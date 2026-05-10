"""Actor-critic planning: draft tasks, critique vs BehavioralSpec, revise, emit canonical planner state."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from src.config import Settings, get_settings
from src.domain.schemas import BehavioralSpec, ReviewTask
from src.domain.state import GraphState
from src.infrastructure.behavioral_spec_store import BehavioralSpecStore
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import extract_total_tokens_from_llm_result, parse_structured_output
from src.orchestration.nodes.application.planner import (
    ReviewPlanOutput,
    _normalize_tasks,
    _trace_enabled,
    build_planner_state_update,
    run_planner_generation,
)
from src.orchestration.prompts.renderer import render_reviewer_prompt

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")


class PlanCritiqueOutput(BaseModel):
    aligned: bool = Field(
        description="True if draft review tasks adequately cover behavioral and contractual boundaries.",
    )
    gaps: str = Field(default="", description="Missing coverage or misaligned tasks (compact).")
    revision_instructions: str = Field(
        default="",
        description="Concrete instructions to improve the task list without prescribing specific bugs.",
    )


def _behavioral_excerpt_for_critic(ref: str | None, settings: Settings) -> str:
    if not ref:
        return "(no behavioral spec ref)"
    try:
        spec = BehavioralSpecStore(settings).read(ref)
        blob = spec.model_dump_json(indent=2)
        return blob[:8000] + ("..." if len(blob) > 8000 else "")
    except Exception as exc:  # noqa: BLE001
        return f"(failed to load behavioral spec: {exc.__class__.__name__})"


def make_draft_planner_node(settings: Settings | None = None, *, use_llm: bool = True):
    node_name = "draft_planner"

    def draft_planner_node(state: GraphState) -> Dict[str, Any]:
        resolved = settings or get_settings()
        tasks, summary, warnings, llm_tokens = run_planner_generation(
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
        ref = state.get("behavioral_spec_ref")
        excerpt = _behavioral_excerpt_for_critic(ref if isinstance(ref, str) else None, resolved)
        llm_tokens = 0
        aligned = True
        gaps = ""
        rev_instr = ""
        warnings: List[str] = list(ac.get("warnings") or [])

        if use_llm and draft_tasks:
            try:
                prompt = render_reviewer_prompt(
                    "mental_model/plan_critic.md",
                    {
                        "Behavioral mandate excerpt": excerpt,
                        "Draft tasks JSON": json.dumps([t.model_dump() for t in draft_tasks], indent=2)[:12000],
                    },
                )
                llm = Models.worker(PlanCritiqueOutput, model_key=resolved.reviewer_planner_model_key)
                invoke_result = llm.invoke(prompt)
                out = parse_structured_output(invoke_result, PlanCritiqueOutput)
                llm_tokens = extract_total_tokens_from_llm_result(invoke_result)
                aligned = bool(out.aligned)
                gaps = out.gaps.strip()
                rev_instr = out.revision_instructions.strip()
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}")
                logger.warning("%s LLM fallback aligned=True: %s", node_name, exc)
                aligned = True

        ac["aligned"] = aligned
        ac["last_critique"] = {"gaps": gaps, "revision_instructions": rev_instr}
        ac["phase"] = "critic"
        ac["warnings"] = warnings
        meta["actor_critic_planner"] = ac
        meta.setdefault("mental_model_metrics", {})
        mm = dict(meta.get("mental_model_metrics") or {})
        mm["plan_critic_aligned"] = aligned
        meta["mental_model_metrics"] = mm
        return {"metadata": meta, "node_history": [node_name], "token_usage": llm_tokens}

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
        excerpt = _behavioral_excerpt_for_critic(
            state.get("behavioral_spec_ref") if isinstance(state.get("behavioral_spec_ref"), str) else None,
            resolved,
        )
        llm_tokens = 0
        warnings: List[str] = list(ac.get("warnings") or [])
        new_tasks = draft_tasks

        if use_llm:
            try:
                prompt = render_reviewer_prompt(
                    "mental_model/plan_revision.md",
                    {
                        "Behavioral mandate excerpt": excerpt,
                        "Current tasks JSON": json.dumps([t.model_dump() for t in draft_tasks], indent=2)[:12000],
                        "Critique gaps": str(critique.get("gaps", "")),
                        "Revision instructions": str(critique.get("revision_instructions", "")),
                    },
                )
                llm = Models.planner(ReviewPlanOutput, model_key=resolved.reviewer_planner_model_key)
                invoke_result = llm.invoke(prompt)
                response = parse_structured_output(invoke_result, ReviewPlanOutput)
                llm_tokens = extract_total_tokens_from_llm_result(invoke_result)
                new_tasks = _normalize_tasks(response.tasks, state)
                ac["summary"] = response.summary or ac.get("summary", "")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}")
                logger.warning("%s LLM fallback keeping draft: %s", node_name, exc)

        rc = int(ac.get("revision_count", 0)) + 1
        ac["draft_tasks"] = [t.model_dump(mode="json") for t in new_tasks]
        ac["revision_count"] = rc
        ac["phase"] = "revision"
        ac["warnings"] = warnings
        meta["actor_critic_planner"] = ac
        return {"metadata": meta, "node_history": [node_name], "token_usage": llm_tokens}

    return plan_revision_node


def make_plan_emit_node():
    node_name = "plan_emit"

    def plan_emit_node(state: GraphState) -> Dict[str, Any]:
        meta = dict(state.get("metadata", {}) or {})
        ac = dict(meta.get("actor_critic_planner") or {})
        draft_raw = ac.get("draft_tasks") or []
        tasks = [ReviewTask.model_validate(row) for row in draft_raw] or []
        summary = str(ac.get("summary") or "Actor-critic review plan.")
        warnings = [str(w) for w in (ac.get("warnings") or []) if w]
        if not tasks:
            tasks, summary, warn2, _t = run_planner_generation(state, use_llm=False)
            warnings.extend(warn2)
        out = build_planner_state_update(
            state,
            tasks,
            summary,
            warnings,
            0,
            node_history_name=node_name,
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
    settings = get_settings()
    meta = state.get("metadata", {}) or {}
    ac = meta.get("actor_critic_planner") or {}
    if ac.get("aligned"):
        return "plan_emit"
    if int(ac.get("revision_count", 0)) >= int(settings.reviewer_actor_critic_max_plan_revisions):
        return "plan_emit"
    return "plan_revision"
