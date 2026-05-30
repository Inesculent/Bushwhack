"""Fan-out dispatch for Phase 2 community semantic agents."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from langgraph.types import Send

from src.config import Settings, get_settings
from src.domain.schemas import StructuralTopologySummary
from src.domain.state import GraphState
from src.infrastructure.community_context import plan_community_dispatch
from src.infrastructure.review_kb import build_review_kb, compatibility_summaries_from_kb
from src.orchestration.nodes.exploration.repository_kb_distillation import distill_repository_kb
from src.orchestration.routing.send_payload import payload_for_send

logger = logging.getLogger(__name__)


def _changed_file_paths_from_diff(git_diff: str) -> set[str]:
    """Extract repo-relative paths from a unified git diff."""
    paths: set[str] = set()
    for raw_line in (git_diff or "").splitlines():
        line = raw_line.strip()
        if line.startswith("diff --git "):
            parts = line.split()
            for part in parts[2:4]:
                if part.startswith(("a/", "b/")):
                    paths.add(part[2:])
        elif line.startswith(("+++ b/", "--- a/")):
            paths.add(line[6:])
    return {p for p in paths if p and p != "/dev/null"}


def make_semantic_dispatch_node(settings: Settings | None = None, *, use_llm: bool = True):
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

        changed_file_paths = _changed_file_paths_from_diff(state.get("git_diff", "") or "")
        kb_bundle = build_review_kb(
            run_id=str(state.get("run_id") or ""),
            repo_path=str(state.get("repo_path") or ""),
            graph_payload=graph_payload,
            topology=topo,
            changed_file_paths=changed_file_paths,
        )
        llm_tokens = 0
        distillation_warnings: List[str] = []
        kb_bundle, llm_tokens, distillation_warnings = distill_repository_kb(
            kb_bundle,
            settings=resolved_settings,
            use_llm=use_llm,
        )
        compatibility = compatibility_summaries_from_kb(kb_bundle)
        trivial, work = plan_community_dispatch(
            topo,
            graph_payload,
            resolved_settings,
            changed_file_paths=changed_file_paths,
        )
        meta["semantic_phase2"] = {
            "dispatch": "review_kb",
            "trivial_communities": len(trivial),
            "pending_community_agents": 0,
            "legacy_pending_community_agents": len(work),
            "max_parallel_agents": resolved_settings.semantic_max_parallel_agents,
            "changed_file_count": len(changed_file_paths),
            "dispatch_cursor": 0,
            "dispatch_total": 0,
            "review_kb": {
                "schema_version": kb_bundle.manifest.schema_version,
                "counts": dict(kb_bundle.manifest.counts),
                "coverage": dict(kb_bundle.manifest.coverage),
                "kb_scope": "repository",
                "overlay_changed_files": len(kb_bundle.review_overlay.get("changed_files") or []),
                "distillation_warnings": distillation_warnings[:20],
            },
        }
        out: Dict[str, Any] = {
            "community_summaries": compatibility or trivial,
            "repository_kb_summary_records": [
                r.model_dump(mode="json") for r in kb_bundle.summaries
            ],
            "metadata": meta,
            "node_history": ["semantic_dispatch:review_kb"],
            "semantic_community_work_queue": [],
            "semantic_dispatch_cursor": 0,
            "token_usage": llm_tokens,
        }
        return out

    return semantic_dispatch_node


def route_semantic_dispatch(state: GraphState) -> Any:
    """Route one bounded wave of community agents, or continue to resolver."""
    queue = state.get("semantic_community_work_queue") or []
    if not queue:
        return "unverified_call_resolver"
    meta = (state.get("metadata") or {}).get("semantic_phase2", {})
    batch_size = meta.get("max_parallel_agents")
    if not isinstance(batch_size, int):
        batch_size = get_settings().semantic_max_parallel_agents
    cursor = int(state.get("semantic_dispatch_cursor") or 0)
    if cursor >= len(queue):
        return "unverified_call_resolver"
    end = min(cursor + max(1, batch_size), len(queue))
    return [
        Send(
            "community_semantic_agent",
            payload_for_send(state, semantic_community_work_item=item),
        )
        for item in queue[cursor:end]
    ]
