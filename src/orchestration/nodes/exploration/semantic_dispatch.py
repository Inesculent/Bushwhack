"""Fan-out dispatch for Phase 2 community semantic agents."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from langgraph.types import Send

from src.config import Settings, get_settings
from src.domain.schemas import StructuralTopologySummary
from src.domain.state import GraphState
from src.infrastructure.community_context import plan_community_dispatch
from src.orchestration.routing.send_payload import payload_for_send

logger = logging.getLogger(__name__)


def make_semantic_dispatch_node(settings: Settings | None = None):
    """Prepare a bounded wave queue for non-trivial communities."""

    def semantic_dispatch_node(state: GraphState) -> Dict[str, Any]:
        resolved_settings = settings or get_settings()
        meta = dict(state.get("metadata", {}))
        sp2 = dict(meta.get("semantic_phase2", {}))
        existing_queue = state.get("semantic_community_work_queue")
        queue_already_planned = sp2.get("dispatch") == "ok"
        if queue_already_planned and existing_queue is not None:
            queue = list(existing_queue)
            batch_size = max(1, resolved_settings.semantic_max_parallel_agents)
            previous_cursor = int(state.get("semantic_dispatch_cursor") or 0)
            next_cursor = min(previous_cursor + batch_size, len(queue))
            sp2["dispatch_cursor"] = next_cursor
            sp2["dispatch_total"] = len(queue)
            sp2["max_parallel_agents"] = batch_size
            meta["semantic_phase2"] = sp2
            return {
                "semantic_dispatch_cursor": next_cursor,
                "metadata": meta,
                "node_history": [f"semantic_dispatch:batch:{next_cursor}/{len(queue)}"],
            }

        topo = state.get("structural_topology")
        graph_payload = state.get("structural_graph_node_link") or {}
        if not isinstance(topo, StructuralTopologySummary):
            try:
                topo = StructuralTopologySummary.model_validate(topo) if topo else None
            except Exception:
                topo = None
        if topo is None or not isinstance(graph_payload, dict):
            logger.warning("semantic_dispatch: missing topology or graph payload")
            return {
                "node_history": ["semantic_dispatch:skipped"],
                "metadata": {"semantic_phase2": {"dispatch": "skipped_missing_inputs"}},
                "semantic_community_work_queue": [],
                "semantic_dispatch_cursor": 0,
            }

        trivial, work = plan_community_dispatch(topo, graph_payload, resolved_settings)
        meta["semantic_phase2"] = {
            "dispatch": "ok",
            "trivial_communities": len(trivial),
            "pending_community_agents": len(work),
            "max_parallel_agents": resolved_settings.semantic_max_parallel_agents,
            "dispatch_cursor": 0,
            "dispatch_total": len(work),
        }
        out: Dict[str, Any] = {
            "community_summaries": trivial,
            "metadata": meta,
            "node_history": ["semantic_dispatch"],
            "semantic_community_work_queue": [w.model_dump(mode="json") for w in work],
            "semantic_dispatch_cursor": 0,
        }
        return out

    return semantic_dispatch_node


def route_semantic_dispatch(state: GraphState) -> Any:
    """Route one bounded wave of community agents, or continue to resolver."""
    queue = state.get("semantic_community_work_queue") or []
    if not queue:
        return "unverified_call_resolver"
    settings = get_settings()
    cursor = int(state.get("semantic_dispatch_cursor") or 0)
    if cursor >= len(queue):
        return "unverified_call_resolver"
    end = min(cursor + max(1, settings.semantic_max_parallel_agents), len(queue))
    return [
        Send(
            "community_semantic_agent",
            payload_for_send(state, semantic_community_work_item=item),
        )
        for item in queue[cursor:end]
    ]
