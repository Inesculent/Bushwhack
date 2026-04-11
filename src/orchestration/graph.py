from typing import Any

from langgraph.checkpoint.redis import RedisSaver
from langgraph.graph import END, START, StateGraph

from src.config import get_settings
from src.domain.state import GraphState
from src.orchestration.nodes.exploration.explorer import explorer_node


def build_graph(checkpointer: Any = None):
    builder = StateGraph(GraphState)
    builder.add_node("explorer", explorer_node)
    builder.add_edge(START, "explorer")
    builder.add_edge("explorer", END)
    if checkpointer is None:
        return builder.compile()
    return builder.compile(checkpointer=checkpointer)


def run_baseline(state: GraphState) -> dict[str, Any]:
    settings = get_settings()
    if not settings.redis_enabled:
        graph = build_graph()
        return graph.invoke(state)

    thread_id = state.get("run_id", "baseline")
    try:
        with RedisSaver.from_conn_string(settings.redis_url) as checkpointer:
            graph = build_graph(checkpointer=checkpointer)
            return graph.invoke(
                state,
                config={"configurable": {"thread_id": thread_id}},
            )
    except Exception as exc:
        graph = build_graph()
        result = graph.invoke(state)
        metadata = dict(result.get("metadata", {}))
        metadata["checkpoint_warning"] = (
            f"Redis checkpoint unavailable; ran without checkpointing: {exc.__class__.__name__}: {exc}"
        )
        result["metadata"] = metadata
        return result


graph = build_graph()

