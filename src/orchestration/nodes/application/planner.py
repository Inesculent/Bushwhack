from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List

from pydantic import BaseModel, Field

from src.config import get_settings
from src.domain.schemas import ReviewSurface, ReviewTask
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import append_trace, trace_from_exception, trace_llm_call
from src.orchestration.context.context_packets import (
    build_draft_planner_packet,
    classes_introduced_in_diff,
    packet_to_prompt_sections,
    surface_inventory_from_state,
)
from src.orchestration.context.surface_ledger import (
    changed_file_integrity_diagnostics,
    changed_file_sources_from_state,
    changed_files_from_diff,
    surface_by_id,
    surface_ids_for_task,
    surface_ledger_from_state,
    surface_names_for_ids,
)
from src.orchestration.prompts.renderer import render_reviewer_prompt

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

WORKER_SPECIALTIES = ("security", "logic", "performance", "general")
_TASK_DEDUPE_SIMILARITY = 0.85
_DIFF_LOCAL_CORRECTNESS_PHRASES = (
    "diff-local",
    "diff local",
    "general correctness",
    "without requiring off-diff",
    "without off-diff",
    "within the changed",
    "changed hunks",
)
_CLASS_CHUNK_MIN_INVENTORY = 4
_SURFACE_SCOPE_ISOLATION_PHRASE = "do not review any other surface"
_CLASS_SCOPE_ISOLATION_PHRASE = _SURFACE_SCOPE_ISOLATION_PHRASE
_TASK_SURFACE_NAME_RE = re.compile(r"\b([A-Z][a-zA-Z0-9_]{2,})\b")
_TASK_SURFACE_NOISE = frozenset(
    {
        "Diff",
        "Local",
        "Correctness",
        "Audit",
        "Review",
        "Structured",
        "Extraction",
        "Aggregation",
        "Branch",
        "Exhaustiveness",
        "Mandatory",
        "Per",
        "For",
        "The",
        "And",
        "All",
        "Matches",
        "First",
        "Group",
        "Groups",
        "Equal",
        "Ends",
        "With",
        "Starts",
    }
)
_DIFF_CONTEXT_TERMS = re.compile(
    r"\b(diff|hunk|excerpt|snippet|visible|shown|displayed|truncated|partial)\b",
    re.IGNORECASE,
)
_SCOPE_LIMITING_TERMS = re.compile(
    r"\b(only|visible|shown|displayed|do\s+not|don't|cannot|can't|avoid|exclude|ignore|limit(?:ed)?|"
    r"restrict(?:ed)?|truncated|partial|unshown|unseen|unexposed|not\s+shown|not\s+visible|not\s+included)\b",
    re.IGNORECASE,
)
_HIDDEN_TARGET_TERMS = re.compile(
    r"\b(unshown|unseen|unexposed|not\s+shown|not\s+visible|not\s+included|hidden|omitted)\b",
    re.IGNORECASE,
)


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


def _target_files(state: GraphState) -> List[str]:
    diff_files = changed_files_from_diff(state.get("git_diff", "") or "")
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
            "Diff-local correctness",
            "Diff-local correctness: review control flow, return paths, None/empty inputs, off-by-one bounds, "
            "and type/API consistency in the changed repository file(s)—without requiring off-diff callers or middleware.",
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
    tasks = [
        ReviewTask(
            id=f"review-{specialty}",
            title=title,
            description=description,
            target_files=files,
            specialty=specialty,  # type: ignore[arg-type]
            review_dimension={
                "security": "security_boundary",
                "logic": "diff_local_correctness",
                "performance": "resource_lifecycle",
                "general": "api_contract",
            }.get(specialty, "general"),
            depth=1,
        )
        for specialty, title, description in task_specs
    ]
    return [_attach_task_surface_ids(task, state) for task in tasks]


def _task_is_diff_local_correctness(task: ReviewTask) -> bool:
    if task.specialty != "logic":
        return False
    blob = f"{task.title} {task.description}".lower()
    return any(phrase in blob for phrase in _DIFF_LOCAL_CORRECTNESS_PHRASES)


_MULTI_SURFACE_CHECKLIST_MIN_CLASSES = 4


def _surface_chunking_enabled(state: GraphState) -> bool:
    inventory = surface_inventory_from_state(state)
    if len(inventory) < _CLASS_CHUNK_MIN_INVENTORY:
        return False
    return len(_target_files(state)) == 1


def _multi_surface_correctness_suffix(state: GraphState) -> str:
    """Prefer mandate surface inventory over a truncated diff scan."""
    if _surface_chunking_enabled(state):
        return ""
    inventory = surface_inventory_from_state(state)
    if len(inventory) < _MULTI_SURFACE_CHECKLIST_MIN_CLASSES:
        classes = classes_introduced_in_diff(state.get("git_diff", "") or "")
        if len(classes) < _MULTI_SURFACE_CHECKLIST_MIN_CLASSES:
            return ""
        names = ", ".join(classes)
    else:
        names = ", ".join(inventory)
    return (
        f" Audit every changed entry point in: {names}. Keep the review scoped to changed behavior, "
        "local contracts, and directly visible interactions for each listed surface."
    )


def _diff_text_for_signals(state: GraphState) -> str:
    parts: List[str] = []
    git_diff = state.get("git_diff", "") or ""
    if git_diff.strip():
        parts.append(git_diff)
    metadata = state.get("metadata", {}) or {}
    mm = metadata.get("mental_model", {}) if isinstance(metadata, dict) else {}
    if isinstance(mm, dict):
        digest = mm.get("bootstrap_digest") or mm.get("diff_digest")
        if isinstance(digest, str) and digest.strip():
            parts.append(digest)
    return "\n".join(parts)


def _diff_signals_structured_extraction(state: GraphState) -> bool:
    return False


def _task_covers_structured_extraction(
    task: ReviewTask,
    inventory: List[str] | None = None,
) -> bool:
    return False


def _surfaces_mentioned_in_text(text: str, inventory: List[str]) -> List[str]:
    return [name for name in inventory if name in text]


def _attach_task_surface_ids(task: ReviewTask, state: GraphState) -> ReviewTask:
    ledger = surface_ledger_from_state(state)
    if not ledger:
        return task
    surface_ids = surface_ids_for_task(task, ledger)
    if surface_ids == task.surface_ids:
        return task
    return task.model_copy(update={"surface_ids": surface_ids})


def _surface_names_for_task(task: ReviewTask, state: GraphState) -> List[str]:
    ledger = surface_ledger_from_state(state)
    if not ledger:
        return []
    return surface_names_for_ids(surface_ids_for_task(task, ledger), ledger)


def _required_surfaces_for_plan(ledger: List[ReviewSurface]) -> List[ReviewSurface]:
    """Surfaces that must have executable work before emit."""
    high_confidence = [surface for surface in ledger if surface.confidence >= 0.75]
    symbol_surfaces = [surface for surface in high_confidence if surface.kind != "file"]
    symbol_files = {surface.file_path for surface in symbol_surfaces}
    file_fallbacks = [
        surface
        for surface in high_confidence
        if surface.kind == "file" and surface.file_path not in symbol_files
    ]
    return sorted(symbol_surfaces + file_fallbacks, key=lambda s: (s.file_path, s.line_start or 10**9, s.name))


def _explicit_surface_ids(task: ReviewTask, by_id: dict[str, ReviewSurface]) -> List[str]:
    return _dedupe_preserve_order(sid for sid in task.surface_ids if sid in by_id)


def _task_is_concrete_cross_surface(task: ReviewTask) -> bool:
    blob = f"{task.id} {task.title} {task.description}".lower()
    return any(
        marker in blob
        for marker in (
            "call path",
            "call-path",
            "caller-reliance",
            "caller reliance",
            "migration",
            "integration contract",
            "concrete integration",
        )
    )


def _primary_surface_ids_for_task(task: ReviewTask, by_id: dict[str, ReviewSurface]) -> List[str]:
    surface_ids = _explicit_surface_ids(task, by_id)
    if not surface_ids:
        return []
    if _task_is_explicit_cross_surface(task) and not _task_is_concrete_cross_surface(task):
        return []
    return surface_ids


def _logic_covered_surface_ids(tasks: List[ReviewTask], by_id: dict[str, ReviewSurface]) -> set[str]:
    covered: set[str] = set()
    for task in tasks:
        if task.specialty != "logic":
            continue
        covered.update(_primary_surface_ids_for_task(task, by_id))
    return covered


def surface_work_fill_tasks(tasks: List[ReviewTask], state: GraphState) -> tuple[List[ReviewTask], Dict[str, Any]]:
    """Report uncovered surfaces without inventing semantic task ownership."""
    ledger = surface_ledger_from_state(state)
    if not ledger:
        return tasks, {
            "surface_fill_uncovered_before": [],
            "surface_fill_added_tasks": [],
        }
    by_id = surface_by_id(ledger)
    required = _required_surfaces_for_plan(ledger)
    logic_covered = _logic_covered_surface_ids(tasks, by_id)
    uncovered = [surface for surface in required if surface.surface_id not in logic_covered]
    return tasks, {
        "surface_fill_uncovered_before": [
            {
                "surface_id": surface.surface_id,
                "name": surface.name,
                "kind": surface.kind,
                "file_path": surface.file_path,
            }
            for surface in uncovered
        ],
        "surface_fill_added_tasks": [],
        "surface_fill_requires_plan_repair": bool(uncovered),
    }


def _task_surface_names(task: ReviewTask) -> frozenset[str]:
    names: set[str] = set()
    for match in _TASK_SURFACE_NAME_RE.finditer(f"{task.title} {task.description}"):
        token = match.group(1)
        if token not in _TASK_SURFACE_NOISE:
            names.add(token)
    return frozenset(names)


def _is_surface_scoped_logic_task(task: ReviewTask, inventory: List[str]) -> bool:
    if task.specialty != "logic":
        return False
    mentioned = _surfaces_mentioned_in_text(f"{task.title} {task.description}", inventory)
    if not mentioned or len(mentioned) > 2:
        return False
    blob = f"{task.title} {task.description}".lower()
    return _CLASS_SCOPE_ISOLATION_PHRASE in blob or " only " in f" {blob} " or blob.endswith(" only.")


def _surfaces_covered_by_scoped_logic_tasks(
    tasks: List[ReviewTask],
    inventory: List[str],
    state: GraphState,
) -> set[str]:
    covered: set[str] = set()
    id_to_name = {surface.surface_id: surface.name for surface in surface_ledger_from_state(state)}
    for task in tasks:
        if task.specialty != "logic" or not _is_surface_scoped_logic_task(task, inventory):
            continue
        covered.update(id_to_name[sid] for sid in task.surface_ids if sid in id_to_name)
        covered.update(_surfaces_mentioned_in_text(f"{task.title} {task.description}", inventory))
    return covered


def _strip_mega_audit_suffix(description: str) -> str:
    lowered = description.lower()
    marker = "audit every changed entry point in:"
    idx = lowered.find(marker)
    if idx < 0:
        return description
    return description[:idx].rstrip()


def _ensure_structured_extraction_logic_task(
    tasks: List[ReviewTask],
    state: GraphState,
) -> List[ReviewTask]:
    return tasks


def _baseline_diff_local_correctness_task(files: List[str], state: GraphState) -> ReviewTask:
    description = (
        "Diff-local correctness: review control flow, return paths, None/empty inputs, off-by-one bounds, "
        "and type/API consistency in the changed repository file(s)—without requiring off-diff callers or middleware."
    )
    description += _multi_surface_correctness_suffix(state)
    task = ReviewTask(
        id="review-logic-diff-local",
        title="Diff-local correctness",
        description=description,
        target_files=files,
        specialty="logic",
        review_dimension="diff_local_correctness",
        depth=1,
    )
    return _attach_task_surface_ids(task, state)


def _strip_diff_narrowing_scope(text: str) -> str:
    if not _is_diff_narrowing_scope(text):
        return text
    parts = re.split(r"(?<=[.!?;])\s+", text.strip())
    kept = [
        s
        for s in parts
        if s and not _is_diff_narrowing_scope(s)
    ]
    cleaned = " ".join(kept).strip()
    return cleaned if cleaned else ""


def _is_diff_narrowing_scope(text: str) -> bool:
    if not text.strip():
        return False
    limiting = bool(_SCOPE_LIMITING_TERMS.search(text))
    return limiting and bool(_DIFF_CONTEXT_TERMS.search(text) or _HIDDEN_TARGET_TERMS.search(text))


def _amend_diff_narrowed_tasks(tasks: List[ReviewTask], state: GraphState) -> List[ReviewTask]:
    """After bootstrap, prevent plans that shrink scope to a truncated diff view."""
    from src.orchestration.context.mandate_loop_context import mm_meta

    _, slot = mm_meta(state)
    if not slot.get("bootstrap_completed"):
        return tasks
    suffix = _multi_surface_correctness_suffix(state)
    inventory = surface_inventory_from_state(state)
    amended: List[ReviewTask] = []
    for task in tasks:
        blob = f"{task.title} {task.description}"
        narrowed = _is_diff_narrowing_scope(blob)
        desc = _strip_mega_audit_suffix(task.description)
        surface_scoped = bool(inventory) and _is_surface_scoped_logic_task(task, inventory)
        dedicated_structured = _task_covers_structured_extraction(task, inventory or [])
        append_global_suffix = (
            bool(suffix)
            and suffix not in desc
            and task.specialty == "logic"
            and not surface_scoped
            and not dedicated_structured
            and not task.surface_ids
        )
        if task.specialty == "logic" and (narrowed or append_global_suffix):
            desc = _strip_diff_narrowing_scope(desc)
            if not desc:
                desc = (
                    "Diff-local correctness: audit every changed entry point in the target file(s)."
                )
            if append_global_suffix:
                desc = (desc.rstrip() + suffix).strip()
            amended.append(task.model_copy(update={"description": desc}))
        elif narrowed:
            desc = _strip_diff_narrowing_scope(desc)
            amended.append(task.model_copy(update={"description": desc}))
        elif desc != task.description:
            amended.append(task.model_copy(update={"description": desc}))
        else:
            amended.append(task)
    return amended


def _ensure_diff_local_correctness_task(tasks: List[ReviewTask], state: GraphState) -> List[ReviewTask]:
    """Guarantee a non-context-dependent logic pass when the LLM plan omits one."""
    tasks = _amend_diff_narrowed_tasks(tasks, state)
    inventory = surface_inventory_from_state(state)
    if _surface_chunking_enabled(state):
        scoped = [t for t in tasks if _is_surface_scoped_logic_task(t, inventory)]
        if scoped and len(_surfaces_covered_by_scoped_logic_tasks(tasks, inventory, state)) >= len(
            inventory
        ):
            return tasks
    suffix = _multi_surface_correctness_suffix(state)
    if any(_task_is_diff_local_correctness(task) for task in tasks):
        if suffix:
            enriched: List[ReviewTask] = []
            for task in tasks:
                if (
                    _task_is_diff_local_correctness(task)
                    and suffix not in task.description
                    and not task.surface_ids
                ):
                    desc = _strip_diff_narrowing_scope(task.description) + suffix
                    enriched.append(task.model_copy(update={"description": desc.strip()}))
                else:
                    enriched.append(task)
            return enriched
        return tasks
    files = _target_files(state)
    baseline = _baseline_diff_local_correctness_task(files, state)
    if _is_duplicate_task(baseline, tasks):
        return tasks
    if len(tasks) >= 6:
        for index in range(len(tasks) - 1, -1, -1):
            if tasks[index].specialty == "general":
                trimmed = tasks[:index] + tasks[index + 1 :]
                return trimmed + [baseline]
        return tasks[:-1] + [baseline]
    return tasks + [baseline]


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
        candidate = task.model_copy(
            update={
                "id": task_id,
                "specialty": specialty,
                "target_files": _dedupe_preserve_order(task.target_files or fallback_files),
                "subtasks": [],
            }
        )
        candidate = _attach_task_surface_ids(candidate, state)
        if _is_duplicate_task(candidate, normalized):
            continue
        normalized.append(candidate)

    normalized = normalized or _default_tasks(state)
    return _ensure_structured_extraction_logic_task(normalized, state)


_CROSS_SURFACE_AUDIT_RE = re.compile(
    r"\s*Audit every changed entry point in:.*?(?:listed surface\.?)?",
    re.IGNORECASE | re.DOTALL,
)


def _sanitize_batched_logic_task_description(
    task: ReviewTask,
    state: GraphState,
    inventory: List[str],
) -> str:
    """Remove cross-surface audit boilerplate when the title already scopes a small surface batch."""
    desc = (task.description or "").strip()
    if task.specialty != "logic" or not inventory:
        return desc
    suffix = _multi_surface_correctness_suffix(state)
    if suffix and suffix in desc:
        desc = desc.replace(suffix, "").strip()
    else:
        desc = _CROSS_SURFACE_AUDIT_RE.sub("", desc).strip()
    title_head = task.title.split(":", 1)[0]
    mentioned = [name for name in inventory if name in title_head] or _surface_names_for_task(task, state)
    if 1 <= len(mentioned) <= 5 and _CLASS_SCOPE_ISOLATION_PHRASE not in desc.lower():
        desc = f"{desc} {_CLASS_SCOPE_ISOLATION_PHRASE.capitalize()} in the target file.".strip()
    return desc


def finalize_emitted_tasks(tasks: List[ReviewTask], state: GraphState) -> List[ReviewTask]:
    """Normalize mechanical task fields without rewriting AI-authored scope."""
    if not tasks:
        return tasks
    fallback_files = _target_files(state)
    prepared: List[ReviewTask] = []
    for task in tasks:
        task = _attach_task_surface_ids(task, state)
        prepared.append(
            task.model_copy(
                update={
                    "target_files": _dedupe_preserve_order(task.target_files or fallback_files),
                    "subtasks": [],
                }
            )
        )
    prepared = _ensure_structured_extraction_logic_task(prepared, state)
    return [_attach_task_surface_ids(task, state) for task in prepared]


def _changed_code_files_from_state(state: GraphState) -> set[str]:
    changed_sources = changed_file_sources_from_state(state)
    changed_files = {
        path
        for paths in changed_sources.values()
        for path in paths
    } or set(changed_files_from_diff(state.get("git_diff", "") or ""))
    return {path.strip().replace("\\", "/") for path in changed_files if path and path.strip()}


def _changed_code_files_for_task_targets(state: GraphState) -> set[str]:
    return _changed_code_files_from_state(state)


def _prune_non_changed_task_targets(
    tasks: List[ReviewTask],
    state: GraphState,
) -> tuple[List[ReviewTask], Dict[str, Any]]:
    changed_files = _changed_code_files_for_task_targets(state)
    if not changed_files:
        return tasks, {"task_target_files_pruned": [], "task_target_files_pruned_empty": []}
    pruned: List[ReviewTask] = []
    pruned_rows: List[Dict[str, Any]] = []
    empty_rows: List[Dict[str, Any]] = []
    for task in tasks:
        original = [path.strip().replace("\\", "/") for path in task.target_files if path and path.strip()]
        kept = [path for path in original if path in changed_files]
        dropped = [path for path in original if path not in changed_files]
        if dropped:
            pruned_rows.append({"task_id": task.id, "dropped": dropped, "kept": kept})
        if not kept:
            empty_rows.append({"task_id": task.id, "dropped": dropped})
            pruned.append(task)
            continue
        pruned.append(task.model_copy(update={"target_files": kept}))
    return pruned, {
        "task_target_files_pruned": pruned_rows,
        "task_target_files_pruned_empty": empty_rows,
    }


def prepare_surface_first_tasks(
    tasks: List[ReviewTask],
    state: GraphState,
) -> tuple[List[ReviewTask], Dict[str, Any]]:
    """Finalize planner output, report coverage gaps, and dedupe exact task duplicates."""
    finalized = finalize_emitted_tasks(tasks, state)
    finalized, prune_meta = _prune_non_changed_task_targets(finalized, state)
    diagnosed, fill_meta = surface_work_fill_tasks(finalized, state)
    deduped, dedupe_meta = dedupe_tasks_by_surface_dimension(diagnosed, state)
    return [_attach_task_surface_ids(task, state) for task in deduped], {**prune_meta, **fill_meta, **dedupe_meta}


def _task_is_explicit_cross_surface(task: ReviewTask) -> bool:
    blob = f"{task.id} {task.title} {task.description}".lower()
    return (
        task.id == "review-logic-structured-extraction"
        or "cross-surface" in blob
        or "integration" in blob
        or "call path" in blob
    )


def validate_surface_bound_plan(tasks: List[ReviewTask], state: GraphState) -> Dict[str, Any]:
    """Pre-execution plan gate for changed-surface scope integrity."""

    ledger = surface_ledger_from_state(state)
    inventory_diagnostics = changed_file_integrity_diagnostics(state)
    changed_files = _changed_code_files_from_state(state)
    by_id = surface_by_id(ledger)
    normalized_tasks = list(tasks)

    covered: set[str] = set()
    missing_surface_ids: List[str] = []
    invalid_target_files: List[Dict[str, Any]] = []
    logic_owners: Dict[str, List[str]] = {}

    for task in normalized_tasks:
        surface_ids = _explicit_surface_ids(task, by_id)
        if ledger and not surface_ids:
            missing_surface_ids.append(task.id)
        covered.update(_primary_surface_ids_for_task(task, by_id))
        for path in task.target_files:
            norm = path.strip().replace("\\", "/")
            if changed_files and norm and norm not in changed_files:
                invalid_target_files.append({"task_id": task.id, "file_path": norm})
        if task.specialty == "logic" and not _task_is_explicit_cross_surface(task):
            target_files = {path.strip().replace("\\", "/") for path in task.target_files if path.strip()}
            for sid in surface_ids:
                surface = by_id[sid]
                if surface.kind == "file":
                    continue
                if target_files and surface.file_path and surface.file_path not in target_files:
                    continue
                logic_owners.setdefault(sid, []).append(task.id)

    required_surfaces = _required_surfaces_for_plan(ledger)
    uncovered = [surface.surface_id for surface in required_surfaces if surface.surface_id not in covered]
    overlapping = [
        {"surface_id": sid, "task_ids": task_ids}
        for sid, task_ids in sorted(logic_owners.items())
        if len(set(task_ids)) > 1
    ]
    diagnostics = {
        "ok": not invalid_target_files,
        "coverage_complete": not uncovered,
        "surface_count": len(ledger),
        "required_surface_count": len(required_surfaces),
        "covered_surface_ids": sorted(covered),
        "uncovered_surfaces": [
            {
                "surface_id": sid,
                "name": by_id[sid].name,
                "file_path": by_id[sid].file_path,
            }
            for sid in uncovered
            if sid in by_id
        ],
        "overlapping_tasks": overlapping,
        "invalid_target_files": invalid_target_files,
        "tasks_missing_surface_ids": missing_surface_ids,
        "changed_file_inventory_diagnostics": inventory_diagnostics,
    }
    return diagnostics


def _task_tokens(task: ReviewTask) -> set[str]:
    text = f"{task.title} {task.description}".lower()
    parts = re.split(r"[^a-z0-9]+", text)
    return {p for p in parts if p}


def _task_similarity(left: ReviewTask, right: ReviewTask) -> float:
    lt = _task_tokens(left)
    rt = _task_tokens(right)
    if not lt or not rt:
        return 0.0
    intersection = lt & rt
    union = lt | rt
    return len(intersection) / max(1, len(union))


def _review_dimension_for_task(task: ReviewTask) -> str:
    explicit = str(getattr(task, "review_dimension", "") or "").strip()
    if explicit and explicit != "general":
        return explicit
    return task.specialty


def _specificity_score(task: ReviewTask) -> tuple[int, int, int]:
    surface_count = len(task.surface_ids)
    single_surface = 1 if surface_count == 1 else 0
    return (single_surface, -surface_count, len(task.description or ""))


def _merge_duplicate_task(keeper: ReviewTask, challenger: ReviewTask) -> ReviewTask:
    if _specificity_score(challenger) > _specificity_score(keeper):
        keeper, challenger = challenger, keeper
    return keeper.model_copy(
        update={
            "target_files": _dedupe_preserve_order([*keeper.target_files, *challenger.target_files]),
            "surface_ids": _dedupe_preserve_order([*keeper.surface_ids, *challenger.surface_ids]),
        }
    )


def dedupe_tasks_by_surface_dimension(
    tasks: List[ReviewTask],
    state: GraphState,
) -> tuple[List[ReviewTask], Dict[str, Any]]:
    """Collapse exact single-surface task duplicates while preserving distinct dimensions."""
    by_id = surface_by_id(surface_ledger_from_state(state))
    keyed: dict[tuple[str, str, str], ReviewTask] = {}
    key_order: List[tuple[str, str, str]] = []
    out: List[ReviewTask] = []
    dropped: List[Dict[str, Any]] = []

    for task in tasks:
        surface_ids = _explicit_surface_ids(task, by_id)
        if len(surface_ids) != 1:
            out.append(task)
            continue
        key = (surface_ids[0], task.specialty, _review_dimension_for_task(task))
        existing = keyed.get(key)
        if existing is None:
            keyed[key] = task
            key_order.append(key)
            continue
        merged = _merge_duplicate_task(existing, task)
        dropped_id = existing.id if merged.id == task.id else task.id
        kept_id = merged.id
        keyed[key] = merged
        dropped.append(
            {
                "key": {"surface_id": key[0], "specialty": key[1], "dimension": key[2]},
                "kept_task_id": kept_id,
                "dropped_task_id": dropped_id,
            }
        )

    out.extend(keyed[key] for key in key_order)
    seen_ids: dict[str, int] = {}
    renamed: List[ReviewTask] = []
    for task in out:
        count = seen_ids.get(task.id, 0)
        seen_ids[task.id] = count + 1
        if count == 0:
            renamed.append(task)
        else:
            renamed.append(task.model_copy(update={"id": f"{task.id}-{count + 1}"}))
    return renamed, {"task_dedupe_dropped": dropped}


def _is_duplicate_task(candidate: ReviewTask, existing: List[ReviewTask]) -> bool:
    for prior in existing:
        if prior.specialty != candidate.specialty:
            continue
        if _dedupe_preserve_order(prior.target_files) != _dedupe_preserve_order(candidate.target_files):
            continue
        left_surfaces = _task_surface_names(prior)
        right_surfaces = _task_surface_names(candidate)
        if (
            left_surfaces
            and right_surfaces
            and left_surfaces.isdisjoint(right_surfaces)
            and max(len(left_surfaces), len(right_surfaces)) <= 3
        ):
            continue
        if _task_similarity(prior, candidate) >= _TASK_DEDUPE_SIMILARITY:
            return True
    return False


def _render_planner_prompt(state: GraphState, *, max_diff_chars: int | None = None) -> str:
    packet = build_draft_planner_packet(state, max_diff_chars=max_diff_chars)
    return render_reviewer_prompt("planner.md", packet_to_prompt_sections(packet))


def run_planner_generation(
    state: GraphState,
    *,
    model_key: str | None = None,
    use_llm: bool = True,
) -> tuple[List[ReviewTask], str, List[str], int, List[Dict[str, Any]]]:
    """Run LLM planner (or deterministic fallback) and return normalized leaf tasks."""
    run_id = state.get("run_id", "unknown")
    tasks = _default_tasks(state)
    summary = "Default parallel review plan."
    warnings: List[str] = []
    llm_tokens = 0
    llm_trace: List[Dict[str, Any]] = []

    if use_llm:
        selected_model = model_key or getattr(get_settings(), "reviewer_planner_model_key", None)
        try:
            prompt = _render_planner_prompt(state)
            if _trace_enabled(state):
                trace_logger.info(
                    "TRACE planner_prompt run_id=%s model=%s prompt_chars=%s",
                    run_id,
                    selected_model,
                    len(prompt),
            )
            llm = Models.planner(ReviewPlanOutput, model_key=selected_model)
            traced = trace_llm_call(
                llm,
                prompt,
                state=state,
                node_name="review_planner",
                model_key=selected_model,
                schema_name="ReviewPlanOutput",
                request_label="primary",
                input_summary={"target_files": _target_files(state)},
            )
            invoke_result = traced.result
            response = parse_structured_output(invoke_result, ReviewPlanOutput)
            llm_tokens = traced.tokens
            llm_trace = append_trace(llm_trace, traced)
            tasks = _normalize_tasks(response.tasks, state)
            summary = response.summary or summary
        except Exception as exc:  # noqa: BLE001 - planner fallback keeps review runs alive
            llm_trace.extend(trace_from_exception(exc))
            warnings.append(f"planner_llm_failed:{exc.__class__.__name__}: {exc}")
            logger.warning(
                "review_planner failed run_id=%s reason=%s: %s",
                run_id,
                exc.__class__.__name__,
                exc,
            )
            try:
                fallback_prompt = _render_planner_prompt(state, max_diff_chars=8000)
                llm = Models.planner(ReviewPlanOutput, model_key=selected_model)
                traced = trace_llm_call(
                    llm,
                    fallback_prompt,
                    state=state,
                    node_name="review_planner",
                    model_key=selected_model,
                    schema_name="ReviewPlanOutput",
                    request_label="fallback_short_prompt",
                    input_summary={"target_files": _target_files(state)},
                )
                invoke_result = traced.result
                response = parse_structured_output(invoke_result, ReviewPlanOutput)
                llm_tokens = traced.tokens
                llm_trace = append_trace(llm_trace, traced)
                tasks = _normalize_tasks(response.tasks, state)
                summary = response.summary or summary
                warnings.append("planner_llm_retry:shorter_prompt")
            except Exception as exc2:  # noqa: BLE001 - planner fallback keeps review runs alive
                llm_trace.extend(trace_from_exception(exc2))
                warnings.append(f"planner_llm_fallback:{exc2.__class__.__name__}: {exc2}")
                logger.warning(
                    "review_planner fallback to deterministic plan run_id=%s reason=%s: %s",
                    run_id,
                    exc2.__class__.__name__,
                    exc2,
                )

    return tasks, summary, warnings, llm_tokens, llm_trace


def build_planner_state_update(
    state: GraphState,
    tasks: List[ReviewTask],
    summary: str,
    warnings: List[str],
    llm_tokens: int,
    llm_trace: List[Dict[str, Any]] | None = None,
    *,
    node_history_name: str = "review_planner",
    metadata_extra: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Canonical task_registry / task_status_by_id / metadata block for planner outputs."""
    run_id = state.get("run_id", "unknown")
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
    planner_block: Dict[str, Any] = {
        "summary": summary,
        "task_count": len(tasks),
        "specialties": sorted({task.specialty for task in tasks}),
        "tasks": [task.model_dump() for task in tasks],
        "warnings": warnings,
    }
    if metadata_extra:
        planner_block.update(metadata_extra)
    metadata["review_planner"] = planner_block

    if _trace_enabled(state):
        trace_logger.info(
            "TRACE planner run_id=%s summary=%r task_count=%s node=%s",
            run_id,
            summary,
            len(tasks),
            node_history_name,
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
        "node_history": [node_history_name],
        "next_step": "review",
        "token_usage": llm_tokens,
        "llm_trace": list(llm_trace or []),
    }


def make_review_planner_node(model_key: str | None = None, use_llm: bool = True):
    def review_planner_node(state: GraphState) -> Dict[str, Any]:
        tasks, summary, warnings, llm_tokens, llm_trace = run_planner_generation(
            state, model_key=model_key, use_llm=use_llm
        )
        return build_planner_state_update(
            state,
            tasks,
            summary,
            warnings,
            llm_tokens,
            llm_trace,
            node_history_name="review_planner",
        )

    return review_planner_node
