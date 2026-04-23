"""LangGraph wiring for the solo-agent one-shot ACR worker.

Mirrors ``src.orchestration.graph`` (single-node StateGraph with optional Redis
checkpointer), but deliberately lives in its own package so solo-agent runs
never share infrastructure or artifacts with the multi-agent baseline.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from langgraph.checkpoint.redis import RedisSaver
from langgraph.graph import END, START, StateGraph

from src.config import get_settings
from src.domain.state import GraphState
from src.solo_agent.worker import NODE_NAME, worker_node

logger = logging.getLogger(__name__)


def build_graph(checkpointer: Any = None):
    builder = StateGraph(GraphState)
    builder.add_node(NODE_NAME, worker_node)
    builder.add_edge(START, NODE_NAME)
    builder.add_edge(NODE_NAME, END)

    if checkpointer is None:
        return builder.compile()
    return builder.compile(checkpointer=checkpointer)


def run_solo(state: GraphState) -> Dict[str, Any]:
    """Invoke the solo-agent graph with Redis checkpointing when available."""
    settings = get_settings()
    if not settings.redis_enabled:
        graph = build_graph()
        return graph.invoke(state)

    thread_id = state.get("run_id", "solo_agent")
    try:
        with RedisSaver.from_conn_string(settings.redis_url) as checkpointer:
            graph = build_graph(checkpointer=checkpointer)
            return graph.invoke(
                state,
                config={"configurable": {"thread_id": thread_id}},
            )
    except Exception as exc:
        logger.warning(
            "Redis checkpoint unavailable for solo-agent run; continuing without checkpointing: %s: %s",
            exc.__class__.__name__,
            exc,
        )
        graph = build_graph()
        result = graph.invoke(state)
        metadata = dict(result.get("metadata", {}))
        metadata["checkpoint_warning"] = (
            f"Redis checkpoint unavailable; ran without checkpointing: {exc.__class__.__name__}: {exc}"
        )
        result["metadata"] = metadata
        return result


graph = build_graph()
