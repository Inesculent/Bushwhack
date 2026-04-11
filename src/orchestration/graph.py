from typing import Any
import logging

from langgraph.checkpoint.redis import RedisSaver
from langgraph.graph import END, START, StateGraph

from src.config import get_settings
from src.domain.interfaces import IASTParser
from src.domain.state import GraphState
from src.infrastructure.factory import build_ast_parser, build_cache_service, build_preflight_service
from src.orchestration.nodes.exploration.explorer import explorer_node
from src.orchestration.nodes.exploration.structural_extractor import make_structural_extractor_node


logger = logging.getLogger(__name__)


def build_graph(checkpointer: Any = None):
    settings = get_settings()
    preflight_service = build_preflight_service()
    ast_parser: IASTParser | None = None

    if settings.ast_enabled:
        try:
            ast_parser = build_ast_parser(settings=settings, cache=build_cache_service())
        except Exception as exc:
            if not settings.ast_fallback_to_search:
                raise
            logger.warning(
                "AST parser startup failed; continuing with degraded structural extraction. reason=%s",
                exc,
            )

    structural_extractor_node = make_structural_extractor_node(
        preflight_service=preflight_service,
        ast_parser=ast_parser,
    )

    builder = StateGraph(GraphState)
    builder.add_node("explorer", explorer_node)
    builder.add_node("structural_extractor", structural_extractor_node)
    builder.add_edge(START, "explorer")
    builder.add_edge("explorer", "structural_extractor")
    builder.add_edge("structural_extractor", END)
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

