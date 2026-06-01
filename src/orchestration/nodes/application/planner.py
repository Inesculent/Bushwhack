from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List

from pydantic import BaseModel, Field

from src.config import get_settings
from src.domain.schemas import ReviewTask, StructuralTopologySummary
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
MAX_PLANNER_DIFF_CHARS = 20000
MAX_PLANNER_RELATED_ITEMS = 8
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
_DEDICATED_STRUCTURED_TASK_MARKERS = (
    "structured extraction and aggregation",
    "structured extraction",
    "review-logic-structured-extraction",
)
_MAX_PLANNER_TASKS = 10
_CLASS_CHUNK_MIN_INVENTORY = 4
_CLASS_CHUNK_DEFAULT_BATCH = 2
_MULTI_SURFACE_SPLIT_MIN_INVENTORY = 8
_MULTI_SURFACE_SPLIT_MIN_MENTIONED = 6
_CLASS_SCOPE_ISOLATION_PHRASE = "do not review any other class"
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
_CLASS_INTRO_RE = re.compile(r"^\+\s*class\s+(\w+)", re.MULTILINE)
_DIFF_SIGNAL_FINDALL = re.compile(r"\bfindall\s*\(", re.IGNORECASE)
_DIFF_SIGNAL_GROUP = re.compile(r"\.group\s*\(|\.groups\s*\(", re.IGNORECASE)
_DIFF_SIGNAL_FINDITER = re.compile(r"\bfinditer\s*\(", re.IGNORECASE)
_DIFF_SIGNAL_JOIN = re.compile(r"\bjoin\s*\(", re.IGNORECASE)
_STRUCTURED_REGION_MARKERS = (
    _DIFF_SIGNAL_FINDALL,
    _DIFF_SIGNAL_GROUP,
    _DIFF_SIGNAL_FINDITER,
    _DIFF_SIGNAL_JOIN,
)
_DIFF_SIGNAL_ELIF_DISCRIMINANT = re.compile(
    r"^\+\s*elif\b.*\b(mode|op|action|kind)\b",
    re.IGNORECASE | re.MULTILINE,
)
_DIFF_NARROWING_PHRASES = (
    "visible in the diff",
    "visible nodes",
    "do not infer",
    "unexposed nodes",
    "truncated diff",
    "diff excerpt",
    "only the 5",
    "five visible",
    "322-line",
    "unexposed",
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
        f" Audit every changed entry point in: {names}. For each: branch exhaustiveness on "
        "mode/discriminant inputs, consistent return on all paths, correct indexing into "
        "structured results (e.g. regex tuples, capture groups), and safe aggregation before return."
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
    """True when diff/digest suggests structured extraction or multi-branch dispatch."""
    blob = _diff_text_for_signals(state)
    if not blob.strip():
        return False
    signals = (
        _DIFF_SIGNAL_FINDALL.search(blob),
        _DIFF_SIGNAL_GROUP.search(blob),
        _DIFF_SIGNAL_FINDITER.search(blob),
        _DIFF_SIGNAL_JOIN.search(blob),
        len(_DIFF_SIGNAL_ELIF_DISCRIMINANT.findall(blob)) >= 2,
    )
    return any(signals)


def _task_covers_structured_extraction(
    task: ReviewTask,
    inventory: List[str] | None = None,
) -> bool:
    """True only for a dedicated, class-scoped structured-extraction task."""
    if task.id == "review-logic-structured-extraction":
        return True
    if task.specialty != "logic" or not inventory:
        return False
    if not _is_class_scoped_logic_task(task, inventory):
        return False
    blob = f"{task.title} {task.description}".lower()
    if any(marker in blob for marker in _DEDICATED_STRUCTURED_TASK_MARKERS):
        return True
    return "type-tracing" in blob and "structured" in blob


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


def _task_surface_names(task: ReviewTask) -> frozenset[str]:
    names: set[str] = set()
    for match in _TASK_SURFACE_NAME_RE.finditer(f"{task.title} {task.description}"):
        token = match.group(1)
        if token not in _TASK_SURFACE_NOISE:
            names.add(token)
    return frozenset(names)


def _is_class_scoped_logic_task(task: ReviewTask, inventory: List[str]) -> bool:
    if task.specialty != "logic":
        return False
    mentioned = _surfaces_mentioned_in_text(f"{task.title} {task.description}", inventory)
    if not mentioned or len(mentioned) > 2:
        return False
    blob = f"{task.title} {task.description}".lower()
    return _CLASS_SCOPE_ISOLATION_PHRASE in blob or " only " in f" {blob} " or blob.endswith(" only.")


def _diff_region_for_class(blob: str, class_name: str) -> str:
    """Added-lines region for one class name inside a diff or digest blob."""
    if not blob.strip() or not class_name:
        return ""
    intro = re.search(rf"^\+\s*class\s+{re.escape(class_name)}\b", blob, re.MULTILINE)
    if not intro:
        return ""
    after = blob[intro.end() :]
    next_intro = _CLASS_INTRO_RE.search(after)
    end = intro.end() + next_intro.start() if next_intro else len(blob)
    return blob[intro.start() : end]


def _structured_focus_surfaces(inventory: List[str], state: GraphState) -> List[str]:
    if not _diff_signals_structured_extraction(state):
        return []
    blob = _diff_text_for_signals(state)
    focused: List[str] = []
    for name in inventory:
        region = _diff_region_for_class(blob, name)
        if region and any(marker.search(region) for marker in _STRUCTURED_REGION_MARKERS):
            focused.append(name)
    return focused


def _branch_focus_surfaces(inventory: List[str], state: GraphState) -> List[str]:
    blob = _diff_text_for_signals(state)
    if len(_DIFF_SIGNAL_ELIF_DISCRIMINANT.findall(blob)) < 2:
        return []
    focused: List[str] = []
    for name in inventory:
        region = _diff_region_for_class(blob, name)
        if region and re.search(r"^\+\s*elif\b", region, re.MULTILINE | re.IGNORECASE):
            focused.append(name)
    return focused


def _class_focus_description(surfaces: List[str], *, focus: str) -> str:
    names = ", ".join(surfaces)
    if focus == "structured":
        lead = surfaces[0] if len(surfaces) == 1 else names
        return (
            f"Mandatory type-tracing on {lead} only: structured return shapes (tuple vs scalar, "
            "row slots, parsed fields), correct index/slot selection, and join/format paths with no stray None. "
            f"{_CLASS_SCOPE_ISOLATION_PHRASE.capitalize()} in the target file."
        )
    if focus == "branch":
        lead = surfaces[0] if len(surfaces) == 1 else names
        return (
            f"Branch-exhaustiveness on {lead} only: every if/elif returns or raises; "
            "require a terminal else for invalid discriminant values. "
            f"{_CLASS_SCOPE_ISOLATION_PHRASE.capitalize()} in the target file."
        )
    return (
        f"Diff-local correctness for {names} only. Per class: branch exhaustiveness, consistent returns, "
        "correct indexing into structured results, safe aggregation before return. "
        f"{_CLASS_SCOPE_ISOLATION_PHRASE.capitalize()} in the target file."
    )


def _class_focus_title(surfaces: List[str], *, focus: str) -> str:
    if len(surfaces) == 1:
        if focus == "structured":
            return f"{surfaces[0]} — structured extraction"
        if focus == "branch":
            return f"{surfaces[0]} — branch exhaustiveness"
        return f"Diff-local correctness: {surfaces[0]}"
    return f"Diff-local correctness: {', '.join(surfaces)}"


def _logic_class_focus_task(
    files: List[str],
    surfaces: List[str],
    *,
    focus: str,
    shard_index: int,
) -> ReviewTask:
    slug = "-".join(s.lower() for s in surfaces[:2])
    if len(surfaces) == 1:
        task_id = f"review-logic-{surfaces[0]}"
    else:
        task_id = f"review-logic-batch-{shard_index + 1}-{slug}"
    title = _class_focus_title(surfaces, focus=focus)
    if len(title) > 80:
        title = title[:77] + "..."
    description = _class_focus_description(surfaces, focus=focus)
    if len(description) > 500:
        description = description[:497] + "..."
    return ReviewTask(
        id=task_id,
        title=title,
        description=description,
        target_files=files,
        specialty="logic",
        depth=1,
    )


def _batch_surface_list(surfaces: List[str], batch_size: int) -> List[List[str]]:
    if batch_size < 1:
        batch_size = 1
    return [surfaces[i : i + batch_size] for i in range(0, len(surfaces), batch_size)]


def _build_class_focus_shards(
    surfaces: List[str],
    state: GraphState,
    *,
    max_shards: int,
) -> List[ReviewTask]:
    if max_shards < 1 or not surfaces:
        return []
    files = _target_files(state)
    structured = [s for s in _structured_focus_surfaces(surfaces, state) if s in surfaces]
    branch = [s for s in _branch_focus_surfaces(surfaces, state) if s in surfaces]
    remainder = [s for s in surfaces if s not in structured and s not in branch]

    planned: List[tuple[List[str], str]] = []
    for name in structured:
        planned.append(([name], "structured"))
    for name in branch:
        if name not in structured:
            planned.append(([name], "branch"))
    batch_size = _CLASS_CHUNK_DEFAULT_BATCH
    while batch_size <= len(remainder):
        batches = _batch_surface_list(remainder, batch_size)
        if len(planned) + len(batches) <= max_shards:
            for batch in batches:
                planned.append((batch, "default"))
            break
        batch_size += 1
    else:
        if remainder and len(planned) < max_shards:
            planned.append((remainder, "default"))

    if len(planned) > max_shards:
        combined: List[tuple[List[str], str]] = []
        default_batches = [p for p in planned if p[1] == "default"]
        focused = [p for p in planned if p[1] != "default"]
        if len(focused) >= max_shards:
            planned = focused[:max_shards]
        else:
            slots = max_shards - len(focused)
            merged = _batch_surface_list(
                [s for batch, _ in default_batches for s in batch],
                max(1, (len(remainder) + slots - 1) // slots),
            )
            planned = focused + [(batch, "default") for batch in merged[:slots]]

    shards: List[ReviewTask] = []
    for index, (batch, focus) in enumerate(planned):
        shards.append(
            _attach_task_surface_ids(
                _logic_class_focus_task(files, batch, focus=focus, shard_index=index),
                state,
            )
        )
    return shards


def _is_monolithic_logic_task(task: ReviewTask, inventory: List[str]) -> bool:
    if task.specialty != "logic":
        return False
    if _is_class_scoped_logic_task(task, inventory):
        return False
    if len(task.surface_ids) >= max(3, (len(inventory) + 1) // 2):
        return True
    if _task_covers_structured_extraction(task, inventory) and len(
        _surfaces_mentioned_in_text(f"{task.title} {task.description}", inventory)
    ) <= 2:
        return False
    mentioned = _surfaces_mentioned_in_text(f"{task.title} {task.description}", inventory)
    if _task_is_diff_local_correctness(task):
        return len(mentioned) >= 3 or len(mentioned) >= max(3, len(inventory) // 2)
    return len(mentioned) >= max(3, (len(inventory) + 1) // 2)


def _surfaces_covered_by_logic_tasks(
    tasks: List[ReviewTask],
    inventory: List[str],
    state: GraphState,
) -> set[str]:
    covered: set[str] = set()
    id_to_name = {
        surface.surface_id: surface.name
        for surface in surface_ledger_from_state(state)
    }
    for task in tasks:
        if task.specialty != "logic":
            continue
        covered.update(id_to_name[sid] for sid in task.surface_ids if sid in id_to_name)
        covered.update(_surfaces_mentioned_in_text(f"{task.title} {task.description}", inventory))
    return covered


def _surfaces_covered_by_class_scoped_logic_tasks(
    tasks: List[ReviewTask],
    inventory: List[str],
    state: GraphState,
) -> set[str]:
    """Surfaces explicitly assigned to class-scoped logic shards (not mega-audit boilerplate)."""
    covered: set[str] = set()
    for task in tasks:
        if task.specialty != "logic" or not _is_class_scoped_logic_task(task, inventory):
            continue
        id_to_name = {surface.surface_id: surface.name for surface in surface_ledger_from_state(state)}
        covered.update(id_to_name[sid] for sid in task.surface_ids if sid in id_to_name)
        covered.update(_surfaces_mentioned_in_text(f"{task.title} {task.description}", inventory))
    return covered


def _chunk_logic_tasks_by_surface(tasks: List[ReviewTask], state: GraphState) -> List[ReviewTask]:
    """Replace monolithic multi-class logic tasks with class-scoped shards."""
    inventory = surface_inventory_from_state(state)
    if not _surface_chunking_enabled(state):
        return _split_oversized_logic_tasks(tasks, state)

    monolithic = [t for t in tasks if _is_monolithic_logic_task(t, inventory)]
    if not monolithic:
        scoped_count = sum(1 for t in tasks if _is_class_scoped_logic_task(t, inventory))
        if scoped_count >= 2:
            return tasks
        logic_tasks = [t for t in tasks if t.specialty == "logic"]
        if len(logic_tasks) == 1 and _is_monolithic_logic_task(logic_tasks[0], inventory):
            monolithic = logic_tasks
        else:
            return tasks

    kept = [t for t in tasks if t not in monolithic]
    covered = _surfaces_covered_by_logic_tasks(kept, inventory, state)
    to_cover = [name for name in inventory if name not in covered]
    if not to_cover:
        out = kept
        return out if len(out) <= _MAX_PLANNER_TASKS else tasks

    max_shards = _MAX_PLANNER_TASKS - len(kept)
    if max_shards < 1:
        return _split_oversized_logic_tasks(tasks, state)

    shards = _build_class_focus_shards(to_cover, state, max_shards=max_shards)
    if not shards:
        return tasks

    out = kept + shards
    if len(out) > _MAX_PLANNER_TASKS:
        return _split_oversized_logic_tasks(tasks, state)
    return out


def _should_split_monolithic_logic_task(task: ReviewTask, inventory: List[str]) -> bool:
    if task.specialty != "logic" or len(inventory) < _MULTI_SURFACE_SPLIT_MIN_INVENTORY:
        return False
    if len(task.surface_ids) >= _MULTI_SURFACE_SPLIT_MIN_MENTIONED:
        return True
    mentioned = _surfaces_mentioned_in_text(task.description, inventory)
    return len(mentioned) >= _MULTI_SURFACE_SPLIT_MIN_MENTIONED


def _logic_surface_shard_task(
    files: List[str],
    surfaces: List[str],
    *,
    shard_index: int,
    shard_count: int,
) -> ReviewTask:
    names = ", ".join(surfaces)
    return ReviewTask(
        id=f"review-logic-surfaces-{shard_index + 1}",
        title=f"Diff-local correctness ({shard_index + 1}/{shard_count})",
        description=(
            f"Diff-local correctness for: {names}. Audit every listed handler—do not defer to "
            "other tasks. Per handler: branch exhaustiveness on mode/discriminant inputs; "
            "consistent returns; correct indexing into structured results; safe aggregation before return."
        ),
        target_files=files,
        specialty="logic",
        depth=1,
    )


def _split_oversized_logic_tasks(tasks: List[ReviewTask], state: GraphState) -> List[ReviewTask]:
    """Replace one logic task that lists 6+ surfaces with parallel shards (more critiquer fan-out)."""
    inventory = surface_inventory_from_state(state)
    if len(inventory) < _MULTI_SURFACE_SPLIT_MIN_INVENTORY:
        return tasks
    logic_tasks = [t for t in tasks if t.specialty == "logic"]
    if len(logic_tasks) >= 2:
        return tasks

    files = _target_files(state)
    out: List[ReviewTask] = []
    split_applied = False
    for task in tasks:
        if task.specialty == "logic" and not split_applied and _should_split_monolithic_logic_task(
            task, inventory
        ):
            midpoint = (len(inventory) + 1) // 2
            shards = [inventory[:midpoint], inventory[midpoint:]]
            shards = [chunk for chunk in shards if chunk]
            for index, chunk in enumerate(shards):
                out.append(
                    _attach_task_surface_ids(
                        _logic_surface_shard_task(files, chunk, shard_index=index, shard_count=len(shards)),
                        state,
                    )
                )
            split_applied = True
            continue
        out.append(task)

    if len(out) > _MAX_PLANNER_TASKS:
        return tasks
    return out


def _structured_extraction_correctness_task(files: List[str]) -> ReviewTask:
    return ReviewTask(
        id="review-logic-structured-extraction",
        title="Structured extraction and aggregation",
        description=(
            "Audit structured extraction and aggregation in changed handlers: correct index/slot "
            "into tuples/lists/rows (not only the first element); truthiness on empty group-like "
            "results vs full-match semantics; and join/format paths with no stray None unless allowed. "
            f"{_CLASS_SCOPE_ISOLATION_PHRASE.capitalize()} in the target file."
        ),
        target_files=files,
        specialty="logic",
        depth=1,
    )


def _strip_mega_audit_suffix(description: str) -> str:
    """Remove appended 'audit every entry point' checklist from shard task text."""
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
    if not _diff_signals_structured_extraction(state):
        return tasks
    inventory = surface_inventory_from_state(state)
    if any(_task_covers_structured_extraction(t, inventory) for t in tasks if t.specialty == "logic"):
        return tasks
    files = _target_files(state)
    extra = _attach_task_surface_ids(_structured_extraction_correctness_task(files), state)
    if _is_duplicate_task(extra, tasks):
        return tasks
    if len(tasks) >= _MAX_PLANNER_TASKS:
        for index in range(len(tasks) - 1, -1, -1):
            if tasks[index].specialty == "general":
                trimmed = tasks[:index] + tasks[index + 1 :]
                return trimmed + [extra]
        return tasks[:-1] + [extra]
    return tasks + [extra]


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
        depth=1,
    )
    return _attach_task_surface_ids(task, state)


def _strip_diff_narrowing_scope(text: str) -> str:
    lowered = text.lower()
    if not any(phrase in lowered for phrase in _DIFF_NARROWING_PHRASES):
        return text
    parts = re.split(r"(?<=[.!?;])\s+", text.strip())
    kept = [
        s
        for s in parts
        if s and not any(phrase in s.lower() for phrase in _DIFF_NARROWING_PHRASES)
    ]
    cleaned = " ".join(kept).strip()
    return cleaned if cleaned else ""


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
        narrowed = any(phrase in blob.lower() for phrase in _DIFF_NARROWING_PHRASES)
        desc = _strip_mega_audit_suffix(task.description)
        class_scoped = bool(inventory) and _is_class_scoped_logic_task(task, inventory)
        dedicated_structured = _task_covers_structured_extraction(task, inventory or [])
        append_global_suffix = (
            bool(suffix)
            and suffix not in desc
            and task.specialty == "logic"
            and not class_scoped
            and not dedicated_structured
            and not task.surface_ids
        )
        if task.specialty == "logic" and (narrowed or append_global_suffix):
            desc = _strip_diff_narrowing_scope(desc)
            if not desc:
                desc = (
                    "Diff-local correctness: audit every changed handler in the target file(s)."
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
        scoped = [t for t in tasks if _is_class_scoped_logic_task(t, inventory)]
        if scoped and len(_surfaces_covered_by_class_scoped_logic_tasks(tasks, inventory, state)) >= len(
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
    normalized = _ensure_diff_local_correctness_task(normalized, state)
    return _ensure_structured_extraction_logic_task(normalized, state)


_CROSS_SURFACE_AUDIT_RE = re.compile(
    r"\s*Audit every changed entry point in:.*?(?:correct indexing into structured results.*?(?:safe aggregation before return\.?)?)?",
    re.IGNORECASE | re.DOTALL,
)


def _sanitize_batched_logic_task_description(
    task: ReviewTask,
    state: GraphState,
    inventory: List[str],
) -> str:
    """Remove cross-surface audit boilerplate when the title already scopes a small class batch."""
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
    """Apply baseline logic guards to actor-critic draft tasks before plan emit."""
    if not tasks:
        return tasks
    fallback_files = _target_files(state)
    inventory = surface_inventory_from_state(state)
    prepared: List[ReviewTask] = []
    for task in tasks:
        task = _attach_task_surface_ids(task, state)
        description = _sanitize_batched_logic_task_description(task, state, inventory)
        prepared.append(
            task.model_copy(
                update={
                    "target_files": _dedupe_preserve_order(task.target_files or fallback_files),
                    "subtasks": [],
                    "description": description,
                }
            )
        )
    prepared = _amend_diff_narrowed_tasks(prepared, state)
    prepared = _chunk_logic_tasks_by_surface(prepared, state)
    prepared = _ensure_diff_local_correctness_task(prepared, state)
    prepared = _ensure_structured_extraction_logic_task(prepared, state)
    return [_attach_task_surface_ids(task, state) for task in prepared]


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
    changed_files = set(changed_files_from_diff(state.get("git_diff", "") or ""))
    by_id = surface_by_id(ledger)
    normalized_tasks = [_attach_task_surface_ids(task, state) for task in tasks]

    covered: set[str] = set()
    missing_surface_ids: List[str] = []
    invalid_target_files: List[Dict[str, Any]] = []
    logic_owners: Dict[str, List[str]] = {}

    for task in normalized_tasks:
        surface_ids = [sid for sid in task.surface_ids if sid in by_id]
        if ledger and not surface_ids:
            missing_surface_ids.append(task.id)
        covered.update(surface_ids)
        for path in task.target_files:
            norm = path.strip().replace("\\", "/")
            if changed_files and norm and norm not in changed_files:
                invalid_target_files.append({"task_id": task.id, "file_path": norm})
        if task.specialty == "logic" and not _task_is_explicit_cross_surface(task):
            for sid in surface_ids:
                logic_owners.setdefault(sid, []).append(task.id)

    high_confidence = [surface for surface in ledger if surface.confidence >= 0.75]
    uncovered = [surface.surface_id for surface in high_confidence if surface.surface_id not in covered]
    overlapping = [
        {"surface_id": sid, "task_ids": task_ids}
        for sid, task_ids in sorted(logic_owners.items())
        if len(set(task_ids)) > 1
    ]
    diagnostics = {
        "ok": not (uncovered or overlapping or invalid_target_files or missing_surface_ids),
        "surface_count": len(ledger),
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


def _structural_routing_hints(state: GraphState, changed_files: List[str]) -> Dict[str, Any]:
    """Summarize only changed-file structural signals useful for task routing."""
    graph_payload = state.get("structural_graph_node_link") or {}
    topology = state.get("structural_topology")
    if topology is not None and not isinstance(topology, StructuralTopologySummary):
        topology = StructuralTopologySummary.model_validate(topology)
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
