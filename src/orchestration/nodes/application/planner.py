from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List

from pydantic import BaseModel, Field

from src.config import get_settings
from src.domain.schemas import ReviewTask
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models

logger = logging.getLogger(__name__)

WORKER_SPECIALTIES = ("security", "logic", "performance", "general")


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
    structural_files = _extract_files_from_structural_graph(state)
    if structural_files:
        return structural_files
    return _extract_files_from_diff(state.get("git_diff", "") or "")


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


def _normalize_tasks(tasks: List[ReviewTask], state: GraphState) -> List[ReviewTask]:
    fallback_files = _target_files(state)
    normalized: List[ReviewTask] = []
    used_ids: set[str] = set()

    for index, task in enumerate(tasks, start=1):
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
                }
            )
        )

    return normalized or _default_tasks(state)


def _render_planner_prompt(state: GraphState) -> str:
    files = _target_files(state)
    preflight_summary = state.get("preflight_summary")
    topology = state.get("structural_topology")
    insights = state.get("global_insights", []) or []

    return (
        "You are the planner for a parallel code-review graph. "
        "Create independent review tasks that can be dispatched to specialist workers. "
        "Use only these specialties: security, logic, performance, general. "
        "Prefer one task per specialty unless the diff clearly requires more focused task clusters. "
        "Each task must include concrete target_files and an evidence-oriented description.\n\n"
        f"Changed files: {files}\n"
        f"Preflight summary: {preflight_summary.model_dump() if preflight_summary else {}}\n"
        f"Structural topology: {topology.model_dump() if topology else {}}\n"
        f"Global insights: {insights}\n\n"
        "Git diff excerpt:\n"
        f"{(state.get('git_diff', '') or '')[:20000]}"
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
            "warnings": warnings,
        }

        return {
            "root_task_id": root_task.id,
            "task_registry": task_registry,
            "task_status_by_id": {task.id: "pending" for task in tasks},
            "metadata": metadata,
            "node_history": ["review_planner"],
            "next_step": "review",
        }

    return review_planner_node
