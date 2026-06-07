"""Compiled mandate explorer subgraph (ReAct loop)."""

from __future__ import annotations

from typing import Any, Dict

from langgraph.graph import END, START, StateGraph

from src.config import get_settings
from src.domain.state import GraphState
from src.infrastructure.llm.langsmith import configure_langsmith_environment
from src.orchestration.context.mandate_loop_context import explorer_mode
from src.orchestration.context.review_context import LazyReviewContextProvider
from src.orchestration.nodes.mandate_explorer_node import make_mandate_explorer_node


def _step_budget(state: GraphState) -> int:
    settings = get_settings()
    mode = str(state.get("mandate_explorer_mode") or explorer_mode(state))
    if mode == "targeted":
        return max(1, int(settings.reviewer_mandate_targeted_max_steps))
    return max(1, int(settings.reviewer_mandate_bootstrap_max_steps))


def _explorer_step_idx(state: GraphState) -> int:
    idx = state.get("mandate_explorer_step_idx")
    if isinstance(idx, int):
        return idx
    meta = state.get("metadata") or {}
    mm = meta.get("mental_model") if isinstance(meta, dict) else {}
    run = mm.get("explorer_run") if isinstance(mm, dict) else {}
    if not isinstance(run, dict):
        run = {}
    return int(run.get("step_idx") or 0)


def _explorer_finished(state: GraphState) -> bool:
    if state.get("mandate_explorer_finished"):
        return True
    meta = state.get("metadata") or {}
    mm = meta.get("mental_model") if isinstance(meta, dict) else {}
    run = mm.get("explorer_run") if isinstance(mm, dict) else {}
    if not isinstance(run, dict):
        run = {}
    return bool(run.get("finished"))


def _explorer_route(state: GraphState) -> str:
    if _explorer_finished(state):
        return "done"
    if _explorer_step_idx(state) >= _step_budget(state):
        return "done"
    return "step"


def build_mandate_explorer_graph(context_provider: LazyReviewContextProvider):
    """Compile explorer: repeated agent steps until finish or budget."""
    configure_langsmith_environment(get_settings())
    builder = StateGraph(GraphState)
    step_node = make_mandate_explorer_node(context_provider)
    builder.add_node("mandate_explorer_step", step_node)

    def init_node(state: GraphState) -> Dict[str, Any]:
        """Reset per-invoke counters (START runs once; step loop must not re-enter init)."""
        return {
            "mandate_explorer_step_idx": 0,
            "mandate_explorer_finished": False,
            "mandate_explorer_retry_feedback": "",
            "node_history": ["mandate_explorer_graph:init"],
        }

    builder.add_node("mandate_explorer_init", init_node)
    builder.add_edge(START, "mandate_explorer_init")
    builder.add_edge("mandate_explorer_init", "mandate_explorer_step")
    builder.add_conditional_edges(
        "mandate_explorer_step",
        _explorer_route,
        {"step": "mandate_explorer_step", "done": END},
    )
    return builder.compile()


def run_mandate_explorer_subgraph(
    state: GraphState,
    context_provider: LazyReviewContextProvider,
    *,
    mode: str,
) -> Dict[str, Any]:
    """Invoke explorer subgraph; set mode in metadata before call."""
    meta = dict(state.get("metadata", {}) or {})
    slot = dict(meta.get("mental_model", {}) or {})
    slot["explorer_mode"] = mode
    slot["explorer_run"] = {"step_idx": 0, "finished": False, "mode": mode}
    meta["mental_model"] = slot
    inner_state = {
        **state,
        "metadata": meta,
        "mandate_explorer_mode": mode,
        "mandate_explorer_step_idx": 0,
        "mandate_explorer_finished": False,
    }
    graph = build_mandate_explorer_graph(context_provider)
    result = graph.invoke(inner_state)
    keys = (
        "exploration_ledger",
        "metadata",
        "token_usage",
        "node_history",
        "mandate_explorer_step_idx",
        "mandate_explorer_finished",
        "mandate_explorer_last_summary",
    )
    return {k: result[k] for k in keys if k in result}
