"""
Task-scoped evidence bundles for critique (symbol/file-complete units, no mid-method chops).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.config import Settings, get_settings
from src.domain.schemas import CodeEntity, ReviewTask
from src.domain.state import GraphState
from src.orchestration.context.context_packets import _extract_files_from_diff
from src.orchestration.context.review_context import LazyReviewContextProvider
from src.orchestration.nodes.application.worker import ReviewTaskContext
from src.orchestration.context.context_packets import surface_inventory_from_state
from src.orchestration.routing.candidate_line_anchor import (
    class_line_range_in_file,
    class_line_range_with_tail,
)
from src.orchestration.routing.finding_dedupe import extract_subject_class

_SYMBOL_NAME_RE = re.compile(r"\b([A-Z][a-zA-Z0-9_]{2,})\b")
_CLASS_SCOPE_ISOLATION_PHRASE = "do not review any other class"
_CLASS_SLICE_TAIL_LINES = 3
_SIMPLE_CLASS_DEF_RE = re.compile(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:\(]", re.MULTILINE)


@dataclass(frozen=True)
class EvidenceUnit:
    priority: int
    label: str
    content: str
    kind: str  # file | symbol | diff


@dataclass
class TaskEvidenceBundle:
    task_id: str
    file_contents: Dict[str, str] = field(default_factory=dict)
    files_complete: Dict[str, bool] = field(default_factory=dict)
    symbols_included: List[str] = field(default_factory=list)
    diff_hunks: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    char_total: int = 0
    byte_chop: bool = False
    rendered: str = ""
    rendered_units: Dict[str, str] = field(default_factory=dict)

    def to_storage_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "file_contents": dict(self.file_contents),
            "files_complete": dict(self.files_complete),
            "symbols_included": list(self.symbols_included),
            "rendered_units": dict(self.rendered_units),
            "rendered": self.rendered,
            "diff_hunks": dict(self.diff_hunks),
            "warnings": list(self.warnings),
            "char_total": self.char_total,
            "byte_chop": self.byte_chop,
        }


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def _task_symbol_names(task: ReviewTask) -> List[str]:
    text = f"{task.title} {task.description}"
    seen: set[str] = set()
    out: List[str] = []
    for match in _SYMBOL_NAME_RE.finditer(text):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def changed_lines_for_file(git_diff: str, file_path: str) -> set[int]:
    """1-based line numbers on the post-image (+) side touched by the diff hunk."""
    hunk_text = diff_hunk_for_file(git_diff, file_path, max_chars=500_000)
    if not hunk_text.strip():
        return set()
    changed: set[int] = set()
    for line in hunk_text.splitlines():
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? ", line)
        if match:
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            for ln in range(start, start + count):
                changed.add(ln)
    return changed


def diff_hunk_for_file(git_diff: str, file_path: str, *, max_chars: int) -> str:
    normalized = _normalize_path(file_path)
    if not git_diff.strip() or not normalized:
        return ""
    blocks: List[str] = []
    current: List[str] = []
    active = False
    for line in git_diff.splitlines():
        if line.startswith("diff --git "):
            if active and current:
                blocks.append("\n".join(current))
            current = []
            active = normalized in line.replace("\\", "/")
        elif line.startswith("+++ b/"):
            path = _normalize_path(line[6:].strip())
            active = path == normalized
            if active:
                current = [line]
        elif active:
            current.append(line)
    if active and current:
        blocks.append("\n".join(current))
    text = "\n\n".join(blocks)
    if len(text) > max_chars:
        return text[:max_chars]
    return text


def _line_slice_inclusive(file_text: str, line_start: int, line_end: int) -> str:
    """Extract ``line_start``..``line_end`` (1-based, both inclusive)."""
    if not file_text or line_start < 1 or line_end < line_start:
        return ""
    lines = file_text.splitlines()
    start = line_start - 1
    end = min(len(lines), line_end)
    return "\n".join(lines[start:end])


def _line_slice(file_text: str, line_start: int, line_end: int) -> str:
    return _line_slice_inclusive(file_text, line_start, line_end)


def _fallback_class_line_range_with_tail(
    file_text: str,
    class_name: str,
    *,
    tail_lines: int = _CLASS_SLICE_TAIL_LINES,
) -> Tuple[int, int] | None:
    if not file_text.strip() or not class_name:
        return None
    lines = file_text.splitlines()
    start: int | None = None
    for idx, line in enumerate(lines, start=1):
        match = _SIMPLE_CLASS_DEF_RE.match(line.strip())
        if match and match.group(1) == class_name:
            start = idx
            break
    if start is None:
        return None
    end = len(lines)
    for idx in range(start + 1, len(lines) + 1):
        if _SIMPLE_CLASS_DEF_RE.match(lines[idx - 1].strip()):
            end = idx - 1
            break
    if tail_lines > 0:
        for line_no in range(end + 1, min(len(lines), end + tail_lines) + 1):
            if _SIMPLE_CLASS_DEF_RE.match(lines[line_no - 1].strip()):
                break
            end = line_no
    return start, end


def _is_class_scoped_task(task: ReviewTask) -> bool:
    blob = f"{task.title} {task.description}".lower()
    return _CLASS_SCOPE_ISOLATION_PHRASE in blob or ".execute" in task.title.lower()


def _task_focus_classes(task: ReviewTask, inventory: List[str]) -> List[str]:
    mentioned = _task_symbol_names(task)
    if inventory:
        mentioned = [name for name in mentioned if name in inventory]
    if not mentioned:
        return []
    if _is_class_scoped_task(task):
        return mentioned[:2]
    return mentioned[:2] if len(mentioned) <= 2 else []


def _entity_end_line(entity: CodeEntity, file_text: str) -> int:
    if entity.definition_line is not None and entity.definition_line >= 1:
        start = entity.definition_line
        if entity.type == "class" and file_text.strip():
            ranged = class_line_range_with_tail(
                file_text, entity.name, tail_lines=_CLASS_SLICE_TAIL_LINES
            )
            if ranged is not None:
                return ranged[1]
        if entity.body.strip():
            return start + max(0, entity.body.count("\n"))
        lines = file_text.splitlines()
        for idx in range(start, len(lines)):
            line = lines[idx]
            if idx > start and line.startswith(("class ", "def ")):
                return idx
        return len(lines)
    return 1


def _entity_unit(
    file_path: str,
    entity: CodeEntity,
    file_text: str,
    *,
    priority: int,
) -> EvidenceUnit | None:
    start = entity.definition_line or 1
    if entity.type == "class" and file_text.strip():
        ranged = class_line_range_with_tail(
            file_text, entity.name, tail_lines=_CLASS_SLICE_TAIL_LINES
        )
        if ranged is not None:
            start, end = ranged
            content = _line_slice_inclusive(file_text, start, end)
        elif entity.body.strip():
            content = entity.body.strip()
            end = _entity_end_line(entity, file_text)
        else:
            return None
    elif entity.body.strip() and entity.type != "class":
        content = entity.body.strip()
        end = _entity_end_line(entity, file_text)
    elif entity.definition_line and file_text:
        end = _entity_end_line(entity, file_text)
        content = _line_slice_inclusive(file_text, entity.definition_line, end)
        start = entity.definition_line
    else:
        return None
    label = f"{entity.name}" if entity.type != "function" else f"{entity.name}"
    header = f"{entity.type} {entity.name} (L{start}-L{end})"
    return EvidenceUnit(
        priority=priority,
        label=f"{file_path}: {header}",
        content=content,
        kind="symbol",
    )


def _pack_units(units: Sequence[EvidenceUnit], budget: int) -> Tuple[List[EvidenceUnit], bool]:
    """Include whole units until budget; never cut inside unit content."""
    ordered = sorted(units, key=lambda u: (u.priority, len(u.content)))
    included: List[EvidenceUnit] = []
    used = 0
    for unit in ordered:
        block = f"--- {unit.label} ---\n{unit.content}"
        need = len(block) + (2 if included else 0)
        if used + need <= budget:
            included.append(unit)
            used += need
    byte_chop = len(included) < len(units)
    return included, byte_chop


def _entity_intersects_changed(
    entity: CodeEntity,
    file_text: str,
    changed_lines: set[int],
) -> bool:
    if not changed_lines:
        return False
    if entity.definition_line and entity.definition_line in changed_lines:
        return True
    start = entity.definition_line or 1
    end = _entity_end_line(entity, file_text)
    return any(ln in changed_lines for ln in range(start, end + 1))


def _render_units(units: Sequence[EvidenceUnit]) -> str:
    parts: List[str] = []
    for unit in units:
        parts.append(f"--- {unit.label} ---\n{unit.content}")
    return "\n\n".join(parts)


def _units_by_file(units: Sequence[EvidenceUnit]) -> Dict[str, str]:
    out: Dict[str, List[str]] = {}
    for unit in units:
        file_path = unit.label.split(":", 1)[0].strip()
        if not file_path:
            continue
        out.setdefault(_normalize_path(file_path), []).append(
            f"--- {unit.label} ---\n{unit.content}"
        )
    return {path: "\n\n".join(parts) for path, parts in out.items()}


def build_task_evidence(
    state: GraphState,
    task: ReviewTask,
    provider: LazyReviewContextProvider,
    ctx: ReviewTaskContext,
    *,
    settings: Settings | None = None,
) -> TaskEvidenceBundle:
    settings = settings or get_settings()
    git_diff = state.get("git_diff", "") or ""
    target_files = [_normalize_path(p) for p in task.target_files[:12] if isinstance(p, str) and p.strip()]
    if not target_files:
        target_files = [_normalize_path(p) for p in _extract_files_from_diff(git_diff)[:3]]

    principles_reserve = 1200
    budget = max(4000, int(settings.reviewer_critique_packet_max_chars) - principles_reserve)
    single_file = len(target_files) == 1
    per_file_cap = (
        min(int(settings.reviewer_critiquer_single_file_max_chars), int(settings.review_full_file_max_chars))
        if single_file
        else min(8000, max(2000, budget // max(1, len(target_files))))
    )

    named_symbols = set(_task_symbol_names(task))
    inventory = surface_inventory_from_state(state)
    focus_classes = _task_focus_classes(task, inventory)
    units: List[EvidenceUnit] = []
    file_contents: Dict[str, str] = {}
    files_complete: Dict[str, bool] = {}
    symbols_included: List[str] = []
    diff_hunks: Dict[str, str] = {}
    warnings: List[str] = list(ctx.warnings)

    for fp in target_files:
        snippet = (ctx.file_snippets.get(fp) or ctx.file_snippets.get(fp.replace("/", "\\")) or "").strip()
        file_text = ""
        if provider is not None:
            file_text = provider.read_full_file(fp, max_chars=per_file_cap)
        if not file_text.strip() and snippet:
            file_text = snippet

        if file_text:
            file_contents[fp] = file_text
            if len(file_text) >= per_file_cap:
                warnings.append(f"evidence_file_content_capped:{fp}")

        entities = ctx.entities_by_file.get(fp) or ctx.entities_by_file.get(fp.replace("/", "\\")) or []

        use_class_slices = bool(single_file and file_text and focus_classes)
        if use_class_slices:
            files_complete[fp] = False
            class_units_added = 0
            for class_name in focus_classes:
                ranged = class_line_range_with_tail(
                    file_text, class_name, tail_lines=_CLASS_SLICE_TAIL_LINES
                )
                if ranged is None:
                    ranged = _fallback_class_line_range_with_tail(
                        file_text,
                        class_name,
                        tail_lines=_CLASS_SLICE_TAIL_LINES,
                    )
                    if ranged is None:
                        warnings.append(f"evidence_class_range_missing:{fp}:{class_name}")
                        continue
                    warnings.append(f"evidence_class_range_recovered:{fp}:{class_name}")
                start, end = ranged
                content = _line_slice_inclusive(file_text, start, end)
                if not content.strip():
                    continue
                units.append(
                    EvidenceUnit(
                        priority=0,
                        label=f"{fp}: class {class_name} (L{start}-L{end})",
                        content=content,
                        kind="symbol",
                    )
                )
                symbols_included.append(f"{fp}: class {class_name} (L{start}-L{end})")
                class_units_added += 1
            if class_units_added == 0:
                use_class_slices = False
                warnings.append(f"evidence_class_slice_fallback:{fp}")
        elif single_file and file_text and len(file_text) <= budget:
            units.append(
                EvidenceUnit(
                    priority=0,
                    label=f"{fp} (complete file)",
                    content=file_text,
                    kind="file",
                )
            )
            files_complete[fp] = True
        elif single_file and file_text:
            files_complete[fp] = False
            warnings.append(f"evidence_file_exceeds_budget:{fp}")

        changed_lines = changed_lines_for_file(git_diff, fp)

        if not use_class_slices and not (single_file and file_text and len(file_text) <= budget):
            for entity in entities:
                in_task = entity.name in named_symbols
                in_diff = _entity_intersects_changed(entity, file_text, changed_lines)
                changed_file = bool(changed_lines)
                execute_on_changed = (
                    changed_file
                    and entity.type == "function"
                    and entity.name == "execute"
                    and in_diff
                )
                class_on_changed = (
                    changed_file and entity.type == "class" and in_diff
                )
                if not (in_task or in_diff or execute_on_changed or class_on_changed):
                    continue
                if execute_on_changed or class_on_changed:
                    pri = 0
                elif in_task:
                    pri = 1
                else:
                    pri = 2
                unit = _entity_unit(fp, entity, file_text, priority=pri)
                if unit is not None:
                    units.append(unit)
                    symbols_included.append(unit.label)

        if not single_file and file_text and not entities:
            units.append(
                EvidenceUnit(
                    priority=2,
                    label=f"{fp} (file excerpt)",
                    content=file_text[: min(len(file_text), per_file_cap)],
                    kind="file",
                )
            )
            files_complete[fp] = len(file_text) <= per_file_cap

        hunk = diff_hunk_for_file(git_diff, fp, max_chars=min(8000, max(4000, budget // 3)))
        if hunk:
            diff_hunks[fp] = hunk

    if not units and ctx.file_snippets:
        for fp, snippet in ctx.file_snippets.items():
            norm = _normalize_path(fp)
            if snippet.strip():
                existing = file_contents.get(norm, "")
                if len(snippet) > len(existing):
                    file_contents[norm] = snippet
                units.append(
                    EvidenceUnit(
                        priority=2,
                        label=f"{norm} (snippet fallback)",
                        content=snippet[:per_file_cap],
                        kind="file",
                    )
                )

    included, byte_chop = _pack_units(units, budget)
    if not included and units:
        warnings.append("evidence_all_units_dropped_for_budget")
    rendered = _render_units(included)
    rendered_units = _units_by_file(included)
    for fp, body in rendered_units.items():
        existing = file_contents.get(fp, "")
        if body.strip() and existing.strip() and len(body) > len(existing):
            warnings.append(f"evidence_rendered_units_preferred:{fp}")

    bundle = TaskEvidenceBundle(
        task_id=task.id,
        file_contents=file_contents,
        files_complete=files_complete,
        symbols_included=symbols_included,
        diff_hunks=diff_hunks,
        warnings=warnings,
        char_total=len(rendered),
        byte_chop=byte_chop,
        rendered=rendered,
        rendered_units=rendered_units,
    )
    return bundle


def task_evidence_slot_from_state(state: GraphState, task_id: str) -> Dict[str, Any]:
    meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    pipe = meta.get("critique_pipeline") if isinstance(meta.get("critique_pipeline"), dict) else {}
    by_task = pipe.get("by_task") if isinstance(pipe.get("by_task"), dict) else {}
    slot = by_task.get(task_id) if isinstance(by_task.get(task_id), dict) else {}
    raw = slot.get("task_evidence")
    return raw if isinstance(raw, dict) else {}


def code_slice_from_task_evidence(
    stored: Mapping[str, Any],
    file_path: str,
    line_start: int,
    line_end: int,
    *,
    padding: int = 40,
) -> str:
    fp = _normalize_path(file_path)
    files = stored.get("file_contents") if isinstance(stored.get("file_contents"), dict) else {}
    text = str(files.get(fp) or files.get(file_path) or "")
    if not text.strip():
        return ""
    lo = max(1, line_start - padding)
    hi = line_end + padding
    return _line_slice(text, lo, hi)


def cited_class_slices_for_candidates(
    state: GraphState,
    candidates: Sequence[Any],
    *,
    max_chars_per_slice: int = 3500,
) -> str:
    """Whole cited-class bodies from task evidence (preferred over truncated diff)."""
    chunks: List[str] = []
    for cand in candidates:
        cid = getattr(cand, "candidate_id", None) or (cand.get("candidate_id") if isinstance(cand, dict) else "")
        tid = getattr(cand, "patch_task_id", None) or (cand.get("patch_task_id") if isinstance(cand, dict) else "")
        fp = getattr(cand, "file_path", None) or (cand.get("file_path") if isinstance(cand, dict) else "")
        if not tid or not fp:
            continue
        stored = task_evidence_slot_from_state(state, str(tid))
        files = stored.get("file_contents") if isinstance(stored.get("file_contents"), dict) else {}
        text = str(files.get(str(fp).replace("\\", "/").lstrip("/")) or files.get(fp) or "")
        if not text.strip():
            continue
        subject = extract_subject_class(
            getattr(cand, "content", "") or (cand.get("content") if isinstance(cand, dict) else ""),
            getattr(cand, "failure_mode", "") or (cand.get("failure_mode") if isinstance(cand, dict) else ""),
            getattr(cand, "evidence_summary", "") or (cand.get("evidence_summary") if isinstance(cand, dict) else ""),
            getattr(cand, "recommendation", "") or (cand.get("recommendation") if isinstance(cand, dict) else "") or "",
        )
        if not subject:
            continue
        class_range = class_line_range_in_file(text, subject)
        if class_range is None:
            continue
        body = _line_slice(text, class_range[0], class_range[1])
        if len(body) > max_chars_per_slice:
            body = body[: max_chars_per_slice - 24].rstrip() + "\n... [class truncated]"
        chunks.append(f"### Cited class {cid}: {subject} ({fp}:{class_range[0]}-{class_range[1]})\n{body}")
    return "\n\n".join(chunks)


def claim_slices_for_candidates(
    state: GraphState,
    candidates: Sequence[Any],
    *,
    padding: int = 40,
    max_chars_per_slice: int = 2000,
) -> str:
    chunks: List[str] = []
    for cand in candidates:
        cid = getattr(cand, "candidate_id", None) or (cand.get("candidate_id") if isinstance(cand, dict) else "")
        tid = getattr(cand, "patch_task_id", None) or (cand.get("patch_task_id") if isinstance(cand, dict) else "")
        fp = getattr(cand, "file_path", None) or (cand.get("file_path") if isinstance(cand, dict) else "")
        ls = getattr(cand, "line_start", None) or (cand.get("line_start") if isinstance(cand, dict) else 1)
        le = getattr(cand, "line_end", None) or (cand.get("line_end") if isinstance(cand, dict) else ls)
        if not tid or not fp:
            continue
        stored = task_evidence_slot_from_state(state, str(tid))
        if not stored:
            continue
        files = stored.get("file_contents") if isinstance(stored.get("file_contents"), dict) else {}
        text = str(files.get(str(fp).replace("\\", "/").lstrip("/")) or files.get(fp) or "")
        ls_i, le_i = int(ls or 1), int(le or ls or 1)
        subject = extract_subject_class(
            getattr(cand, "content", "") or (cand.get("content") if isinstance(cand, dict) else ""),
            getattr(cand, "failure_mode", "") or (cand.get("failure_mode") if isinstance(cand, dict) else ""),
            getattr(cand, "evidence_summary", "") or (cand.get("evidence_summary") if isinstance(cand, dict) else ""),
            getattr(cand, "recommendation", "") or (cand.get("recommendation") if isinstance(cand, dict) else "") or "",
        )
        if subject and text.strip():
            class_range = class_line_range_in_file(text, subject)
            if class_range is not None and class_range[0] <= ls_i and le_i <= class_range[1]:
                continue
        body = code_slice_from_task_evidence(stored, str(fp), ls_i, le_i, padding=padding)
        if len(body) > max_chars_per_slice:
            body = body[: max_chars_per_slice - 24].rstrip() + "\n... [claim slice truncated]"
        if body.strip():
            chunks.append(f"### Claim slice {cid} ({fp}:{ls}-{le})\n{body}")
    return "\n\n".join(chunks)
