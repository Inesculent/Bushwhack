from typing import Any
import logging

from langgraph.graph import END, START, StateGraph

from src.config import get_settings
from src.domain.interfaces import IASTParser
from src.domain.state import GraphState
from src.infrastructure.factory import (
    build_ast_parser,
    build_cache_service,
    build_preflight_service,
    build_snapshot_pointer_store,
    build_snapshot_writer,
)
from src.infrastructure.redis_checkpoint import (
    assert_redis_checkpoint_writable,
    redis_checkpoint_saver,
)
from src.orchestration.nodes.exploration.explorer import explorer_node
from src.orchestration.nodes.exploration.community_semantic_agent import make_community_semantic_agent_node
from src.orchestration.nodes.exploration.phase2_routing import semantic_phase2_should_run
from src.orchestration.nodes.exploration.semantic_dispatch import (
    make_semantic_dispatch_node,
    route_semantic_dispatch,
)
from src.orchestration.nodes.exploration.semantic_merge import make_semantic_merge_node
from src.orchestration.nodes.exploration.snapshot_pin import make_snapshot_pin_node
from src.orchestration.nodes.exploration.structural_extractor import make_structural_extractor_node
from src.orchestration.nodes.exploration.unverified_call_resolver import (
    make_unverified_call_resolver_node,
    route_after_unverified_call_resolver,
)


logger = logging.getLogger(__name__)


def _route_after_structural_context(state: GraphState) -> str:
    if semantic_phase2_should_run(state):
        return "semantic_dispatch"
    return END


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

    snapshot_writer = build_snapshot_writer(settings)
    pointer_store = build_snapshot_pointer_store(settings)

    builder = StateGraph(GraphState)
    builder.add_node("explorer", explorer_node)
    builder.add_node("structural_extractor", structural_extractor_node)
    builder.add_node("semantic_dispatch", make_semantic_dispatch_node(settings))
    builder.add_node("community_semantic_agent", make_community_semantic_agent_node(settings=settings))
    builder.add_node(
        "unverified_call_resolver",
        make_unverified_call_resolver_node(ast_parser=ast_parser, settings=settings),
    )
    builder.add_node("semantic_merge", make_semantic_merge_node(settings=settings))
    builder.add_node(
        "snapshot_pin",
        make_snapshot_pin_node(snapshot_writer, pointer_store, settings=settings),
    )
    builder.add_edge(START, "explorer")
    builder.add_edge("explorer", "structural_extractor")
    builder.add_conditional_edges(
        "structural_extractor",
        _route_after_structural_context,
        {
            "semantic_dispatch": "semantic_dispatch",
            END: END,
        },
    )
    builder.add_conditional_edges("semantic_dispatch", route_semantic_dispatch)
    builder.add_edge("community_semantic_agent", "semantic_dispatch")
    builder.add_conditional_edges(
        "unverified_call_resolver",
        route_after_unverified_call_resolver,
        {
            "unverified_call_resolver": "unverified_call_resolver",
            "semantic_merge": "semantic_merge",
        },
    )
    builder.add_edge("semantic_merge", "snapshot_pin")
    builder.add_edge("snapshot_pin", END)
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
        assert_redis_checkpoint_writable(
            settings.redis_url,
            namespace=settings.redis_namespace,
        )
        with redis_checkpoint_saver(settings) as checkpointer:
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

