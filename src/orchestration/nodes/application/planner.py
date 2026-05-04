from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List

from pydantic import BaseModel, Field

from src.config import get_settings
from src.domain.schemas import ReviewTask
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.orchestration.prompts.renderer import render_reviewer_prompt

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

WORKER_SPECIALTIES = ("security", "logic", "performance", "general")
MAX_PLANNER_DIFF_CHARS = 20000
MAX_PLANNER_RELATED_ITEMS = 8


class ReviewPlanOutput(BaseModel):
    summary: str = Field(description="Concise explanation of the planned review strategy.")
    tasks: List[ReviewTask] = Field(default_factory=list)


def _dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        normalized = value.strip().replace("\\", "/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _extract_files_from_structural_graph(state: GraphState) -> List[str]:
    graph_payload = state.get("structural_graph_node_link") or {}
    nodes = graph_payload.get("nodes", []) if isinstance(graph_payload, dict) else []
    file_paths: List[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("node_type") == "file" and isinstance(node.get("file_path"), str):
            file_paths.append(node["file_path"])
    return _dedupe_preserve_order(file_paths)


def _extract_files_from_diff(git_diff: str) -> List[str]:
    file_paths: List[str] = []
    for line in git_diff.splitlines():
        if line.startswith("+++ b/"):
            file_paths.append(line.removeprefix("+++ b/"))
        elif line.startswith("--- a/"):
            file_paths.append(line.removeprefix("--- a/"))
        elif line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                file_paths.append(parts[3].removeprefix("b/"))
    return _dedupe_preserve_order(path for path in file_paths if path != "/dev/null")


def _target_files(state: GraphState) -> List[str]:
    diff_files = _extract_files_from_diff(state.get("git_diff", "") or "")
    if diff_files:
        return diff_files
    structural_files = _extract_files_from_structural_graph(state)
    if structural_files:
        return structural_files
    return []


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _default_tasks(state: GraphState) -> List[ReviewTask]:
    files = _target_files(state)
    task_specs = [
        (
            "security",
            "Security review",
            "Review the change for authorization, injection, secrets, unsafe file/network access, and deserialization risks.",
        ),
        (
            "logic",
            "Correctness review",
            "Review the change for behavioral regressions, edge cases, broken state transitions, and API contract violations.",
        ),
        (
            "performance",
            "Performance review",
            "Review the change for repeated expensive work, accidental quadratic behavior, N+1 access patterns, and concurrency bottlenecks.",
        ),
        (
            "general",
            "General review",
            "Review maintainability, error handling, tests, and integration consistency for the changed code.",
        ),
    ]
    return [
        ReviewTask(
            id=f"review-{specialty}",
            title=title,
            description=description,
            target_files=files,
            specialty=specialty,  # type: ignore[arg-type]
            depth=1,
        )
        for specialty, title, description in task_specs
    ]


def _flatten_planner_tasks(tasks: List[ReviewTask]) -> List[ReviewTask]:
    """Turn hierarchical planner output into executable leaf tasks for LangGraph Send fan-out."""
    flattened: List[ReviewTask] = []
    for task in tasks:
        if task.subtasks:
            flattened.extend(_flatten_planner_tasks(task.subtasks))
        else:
            flattened.append(task)
    return flattened


def _normalize_tasks(tasks: List[ReviewTask], state: GraphState) -> List[ReviewTask]:
    fallback_files = _target_files(state)
    normalized: List[ReviewTask] = []
    used_ids: set[str] = set()

    for index, task in enumerate(_flatten_planner_tasks(tasks), start=1):
        specialty = task.specialty if task.specialty in WORKER_SPECIALTIES else "general"
        task_id = task.id.strip() or f"review-{specialty}-{index}"
        if task_id in used_ids:
            task_id = f"{task_id}-{index}"
        used_ids.add(task_id)
        normalized.append(
            task.model_copy(
                update={
                    "id": task_id,
                    "specialty": specialty,
                    "target_files": _dedupe_preserve_order(task.target_files or fallback_files),
                    "subtasks": [],
                }
            )
        )

    return normalized or _default_tasks(state)


def _structural_routing_hints(state: GraphState, changed_files: List[str]) -> Dict[str, Any]:
    """Summarize only changed-file structural signals useful for task routing."""
    graph_payload = state.get("structural_graph_node_link") or {}
    topology = state.get("structural_topology")
    if not isinstance(graph_payload, dict):
        return {"changed_files": changed_files}

    nodes = graph_payload.get("nodes", [])
    edges = graph_payload.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return {"changed_files": changed_files}

    node_by_id = {
        str(node.get("id", "")): node
        for node in nodes
        if isinstance(node, dict) and node.get("id") is not None
    }
    file_node_ids = {path: f"file:{path}" for path in changed_files}

    neighbor_ids_by_file: Dict[str, set[str]] = {path: set() for path in changed_files}
    edge_counts_by_file: Dict[str, int] = {path: 0 for path in changed_files}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        for path, file_node_id in file_node_ids.items():
            if source == file_node_id and target:
                neighbor_ids_by_file[path].add(target)
                edge_counts_by_file[path] += 1
            elif target == file_node_id and source:
                neighbor_ids_by_file[path].add(source)
                edge_counts_by_file[path] += 1

    community_by_id = topology.node_to_community if topology is not None else {}
    community_stats: Dict[int, Dict[str, Any]] = {}
    if topology is not None:
        community_stats = {
            community.community_id: {
                "cohesion": community.cohesion,
                "file_count": community.file_count,
                "symbol_count": community.symbol_count,
            }
            for community in topology.communities
        }

    file_ids_by_community: Dict[int, List[str]] = {}
    for node_id, node in node_by_id.items():
        if node.get("node_type") != "file":
            continue
        cid = community_by_id.get(node_id)
        if cid is None:
            continue
        fp = node.get("file_path")
        if isinstance(fp, str):
            file_ids_by_community.setdefault(cid, []).append(fp)

    hints: List[Dict[str, Any]] = []
    for path in changed_files:
        file_node_id = file_node_ids[path]
        community_id = community_by_id.get(file_node_id)
        neighbor_nodes = [node_by_id.get(node_id, {}) for node_id in neighbor_ids_by_file[path]]
        defined_symbols = sorted(
            {
                str(node.get("symbol_name"))
                for node in neighbor_nodes
                if node.get("node_type") == "symbol" and node.get("symbol_name")
            }
        )
        related_files = sorted(
            {
                str(node.get("file_path"))
                for node in neighbor_nodes
                if node.get("node_type") == "file" and node.get("file_path") and node.get("file_path") != path
            }
        )
        if community_id is not None:
            related_files.extend(
                fp
                for fp in sorted(file_ids_by_community.get(community_id, []))
                if fp != path and fp not in related_files
            )

        hints.append(
            {
                "file_path": path,
                "community_id": community_id,
                "community": community_stats.get(community_id) if community_id is not None else None,
                "direct_edge_count": edge_counts_by_file[path],
                "defined_or_adjacent_symbols": defined_symbols[:MAX_PLANNER_RELATED_ITEMS],
                "related_files": related_files[:MAX_PLANNER_RELATED_ITEMS],
            }
        )

    return {
        "changed_file_count": len(changed_files),
        "structural_node_count": len(nodes),
        "structural_edge_count": len(edges),
        "topology_algorithm": topology.algorithm if topology is not None else None,
        "topology_community_count": topology.community_count if topology is not None else None,
        "changed_file_hints": hints,
    }


def _render_planner_prompt(state: GraphState) -> str:
    files = _target_files(state)
    preflight_summary = state.get("preflight_summary")
    insights = state.get("global_insights", []) or []

    return render_reviewer_prompt(
        "planner.md",
        {
            "Changed Files": str(files),
            "Preflight Summary": str(preflight_summary.model_dump() if preflight_summary else {}),
            "Structural Routing Hints": str(_structural_routing_hints(state, files)),
            "Global Insights": str(insights),
            "Git Diff Excerpt": (state.get("git_diff", "") or "")[:MAX_PLANNER_DIFF_CHARS],
        },
    )


def make_review_planner_node(model_key: str | None = None, use_llm: bool = True):
    def review_planner_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        tasks = _default_tasks(state)
        summary = "Default parallel review plan."
        warnings: List[str] = []

        if use_llm:
            selected_model = model_key or getattr(get_settings(), "reviewer_planner_model_key", None)
            try:
                llm = Models.planner(ReviewPlanOutput, model_key=selected_model)
                response = llm.invoke(_render_planner_prompt(state))
                tasks = _normalize_tasks(response.tasks, state)
                summary = response.summary or summary
            except Exception as exc:  # noqa: BLE001 - planner fallback keeps review runs alive
                warnings.append(f"planner_llm_fallback:{exc.__class__.__name__}: {exc}")
                logger.warning(
                    "review_planner falling back to deterministic plan run_id=%s reason=%s: %s",
                    run_id,
                    exc.__class__.__name__,
                    exc,
                )

        root_task = ReviewTask(
            id="review-root",
            title="Parallel code review",
            description=summary,
            target_files=_target_files(state),
            specialty="general",
            depth=0,
            subtasks=tasks,
        )
        task_registry = {root_task.id: root_task}
        task_registry.update({task.id: task for task in tasks})

        metadata = dict(state.get("metadata", {}))
        metadata["review_planner"] = {
            "summary": summary,
            "task_count": len(tasks),
            "specialties": sorted({task.specialty for task in tasks}),
            "tasks": [task.model_dump() for task in tasks],
            "warnings": warnings,
        }

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE planner run_id=%s summary=%r task_count=%s",
                run_id,
                summary,
                len(tasks),
            )
            for task in tasks:
                trace_logger.info(
                    "TRACE plan_task run_id=%s task_id=%s specialty=%s files=%s title=%r",
                    run_id,
                    task.id,
                    task.specialty,
                    task.target_files,
                    task.title,
                )

        return {
            "root_task_id": root_task.id,
            "task_registry": task_registry,
            "task_status_by_id": {task.id: "pending" for task in tasks},
            "metadata": metadata,
            "node_history": ["review_planner"],
            "next_step": "review",
        }

    return review_planner_node
