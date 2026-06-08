"""Routing for coupled mandate-plan loop."""

from __future__ import annotations

from typing import Any, List

from pydantic import BaseModel, Field

from src.config import get_settings
from src.domain.state import GraphState
from src.orchestration.context.mandate_loop_context import coupled_loop_meta, should_skip_bootstrap_explorer
class ExplorationRequestItem(BaseModel):
    file_path: str = Field(default="")
    symbol: str = Field(default="")
    question: str = Field(default="")


class JointPlanCritiqueOutput(BaseModel):
    aligned: bool = Field(description="True when plan and mandate are adequate.")
    gaps: str = Field(default="")
    revision_instructions: str = Field(default="")
    mandate_adequate: bool = Field(default=True)
    exploration_requests: List[ExplorationRequestItem] = Field(default_factory=list)


def route_after_intent(state: GraphState) -> str:
    """Bootstrap explorer or skip to patch when resuming."""
    if should_skip_bootstrap_explorer(state):
        return "mandate_patch"
    return "mandate_explorer"


def route_joint_critic(state: GraphState) -> str:
    settings = get_settings()
    meta = state.get("metadata", {}) or {}
    ac = meta.get("actor_critic_planner") or {}
    if ac.get("aligned"):
        return "mandate_finalize"
    cycle = int(coupled_loop_meta(state).get("cycles", 0))
    if cycle >= int(settings.reviewer_mandate_plan_max_cycles):
        return "mandate_finalize"
    if int(ac.get("revision_count", 0)) >= int(settings.reviewer_actor_critic_max_plan_revisions):
        return "mandate_finalize"
    reqs = ac.get("exploration_requests") or []
    if (
        reqs
        and _targeted_budget_remaining(state, settings)
    ):
        return "mandate_explorer_targeted"
    return "plan_revision"


def _targeted_budget_remaining(state: GraphState, settings: Any) -> bool:
    loop = coupled_loop_meta(state)
    targeted = int((loop.get("explorer_invocations") or {}).get("targeted", 0))
    max_cycles = int(settings.reviewer_mandate_plan_max_cycles)
    return targeted < max_cycles


def route_after_mandate_patch(state: GraphState) -> str:
    """First patch leads to draft_planner; later patches lead to plan_revision."""
    meta = state.get("metadata", {}) or {}
    ac = meta.get("actor_critic_planner") or {}
    draft = ac.get("draft_tasks") or []
    if draft:
        return "plan_revision"
    return "draft_planner"


def increment_coupled_cycle(state: GraphState) -> dict[str, Any]:
    meta = dict(state.get("metadata", {}) or {})
    slot = dict(meta.get("mental_model", {}) or {})
    loop = dict(slot.get("coupled_loop", {}) or {})
    loop["cycles"] = int(loop.get("cycles", 0)) + 1
    slot["coupled_loop"] = loop
    meta["mental_model"] = slot
    return {"metadata": meta}
