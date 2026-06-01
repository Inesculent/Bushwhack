"""Typed changed-surface ledger helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any, Iterable, List, Mapping, Sequence

from src.domain.schemas import CodeEntity, ReviewSurface, ReviewTask, SurfaceInvariant
from src.domain.state import GraphState

_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_CLASS_RE = re.compile(r"^\+\s*class\s+([A-Za-z_][A-Za-z0-9_]*)")
_DEF_RE = re.compile(r"^\+\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)")
_REMOVED_DEF_RE = re.compile(r"^-\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
_CALL_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_MIGRATION_MARKERS = (
    "merge",
    "merged",
    "migrate",
    "migrated",
    "migration",
    "replace",
    "replaced",
    "remove",
    "removed",
    "rename",
    "renamed",
    "call site",
    "callsite",
)


def normalize_repo_path(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("/")


def changed_files_from_diff(git_diff: str) -> List[str]:
    files: List[str] = []
    seen: set[str] = set()
    for line in git_diff.splitlines():
        path = ""
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                path = parts[3].removeprefix("b/")
        elif line.startswith("+++ b/"):
            path = line.removeprefix("+++ b/")
        path = normalize_repo_path(path)
        if path and path != "/dev/null" and path not in seen:
            seen.add(path)
            files.append(path)
    return files


def make_surface_id(file_path: str, name: str, kind: str) -> str:
    norm = f"{normalize_repo_path(file_path)}::{kind}::{name}"
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9_]+", "-", name).strip("-").lower()[:32] or "surface"
    return f"surface:{digest}:{slug}"


def _surface_from_parts(
    *,
    file_path: str,
    name: str,
    kind: str,
    line: int | None,
    line_end: int | None = None,
    source: str,
    confidence: float,
) -> ReviewSurface:
    evidence_refs = [file_path]
    if line is not None:
        evidence_refs = [f"{file_path}:{line}"]
    return ReviewSurface(
        surface_id=make_surface_id(file_path, name, kind),
        name=name,
        kind=kind,  # type: ignore[arg-type]
        file_path=normalize_repo_path(file_path),
        line_start=line,
        line_end=line_end or line,
        source=source,
        confidence=confidence,
        evidence_refs=evidence_refs,
    )


def _coerce_entity(raw: Any) -> CodeEntity | None:
    try:
        return raw if isinstance(raw, CodeEntity) else CodeEntity.model_validate(raw)
    except Exception:
        return None


def _entity_span(entity: CodeEntity) -> tuple[int, int] | None:
    if entity.definition_line is None:
        return None
    start = int(entity.definition_line)
    end = int(entity.definition_end_line or start)
    if end < start:
        end = start
    return start, end


def _entity_kind(entity: CodeEntity) -> str:
    lowered = entity.type.lower()
    if "class" in lowered:
        return "class"
    if "method" in lowered:
        return "method"
    if "function" in lowered:
        return "function"
    return "symbol"


def _line_sets_intersect(start: int, end: int, changed_lines: set[int]) -> bool:
    return any(start <= line <= end for line in changed_lines)


def _merge_surface_records(records: Iterable[ReviewSurface]) -> List[ReviewSurface]:
    merged: dict[tuple[str, str, str], ReviewSurface] = {}
    for surface in records:
        key = (surface.file_path, surface.kind, surface.name)
        existing = merged.get(key)
        if existing is None:
            merged[key] = surface
            continue
        merged[key] = existing.model_copy(
            update={
                "line_start": existing.line_start or surface.line_start,
                "line_end": existing.line_end or surface.line_end,
                "source": existing.source if existing.confidence >= surface.confidence else surface.source,
                "confidence": max(existing.confidence, surface.confidence),
                "evidence_refs": _dedupe([*existing.evidence_refs, *surface.evidence_refs]),
            }
        )
    return sorted(merged.values(), key=lambda s: (s.file_path, s.line_start or 10**9, s.name))


def build_surface_ledger_from_diff(
    git_diff: str,
    *,
    inventory: Sequence[str] | None = None,
    entities_by_file: Mapping[str, Sequence[Any]] | None = None,
) -> List[ReviewSurface]:
    """Build a best-effort typed ledger from diff hunks and optional legacy names."""

    records: dict[tuple[str, str, str], ReviewSurface] = {}
    current_file = ""
    new_line: int | None = None
    first_hunk_line_by_file: dict[str, int] = {}
    changed_lines_by_file: dict[str, set[int]] = {}

    for raw in git_diff.splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split()
            current_file = ""
            if len(parts) >= 4 and parts[3].startswith("b/"):
                current_file = normalize_repo_path(parts[3].removeprefix("b/"))
            new_line = None
            continue
        if raw.startswith("+++ b/"):
            current_file = normalize_repo_path(raw.removeprefix("+++ b/"))
            continue
        if raw.startswith("@@"):
            match = _HUNK_RE.search(raw)
            new_line = int(match.group(1)) if match else None
            if current_file and new_line is not None:
                first_hunk_line_by_file.setdefault(current_file, new_line)
            continue
        if current_file and new_line is None and raw.startswith("+") and not raw.startswith("+++"):
            class_match = _CLASS_RE.match(raw)
            def_match = _DEF_RE.match(raw)
            if class_match or def_match:
                kind = "class" if class_match else "function"
                name = (class_match or def_match).group(1)  # type: ignore[union-attr]
                key = (current_file, kind, name)
                records.setdefault(
                    key,
                    _surface_from_parts(
                        file_path=current_file,
                        name=name,
                        kind=kind,
                        line=first_hunk_line_by_file.get(current_file),
                        line_end=first_hunk_line_by_file.get(current_file),
                        source="diff",
                        confidence=0.85,
                    ),
                )
            continue
        if not current_file or new_line is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            class_match = _CLASS_RE.match(raw)
            def_match = _DEF_RE.match(raw)
            if class_match or def_match:
                kind = "class" if class_match else "function"
                name = (class_match or def_match).group(1)  # type: ignore[union-attr]
                key = (current_file, kind, name)
                records.setdefault(
                    key,
                    _surface_from_parts(
                        file_path=current_file,
                        name=name,
                        kind=kind,
                        line=new_line,
                        line_end=new_line,
                        source="diff",
                        confidence=0.95,
                    ),
                )
            changed_lines_by_file.setdefault(current_file, set()).add(new_line)
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        else:
            new_line += 1

    changed_files = changed_files_from_diff(git_diff)
    if entities_by_file:
        for file_path, raw_entities in entities_by_file.items():
            norm_file = normalize_repo_path(str(file_path))
            changed_lines = changed_lines_by_file.get(norm_file)
            if not changed_lines:
                continue
            for raw_entity in raw_entities:
                entity = _coerce_entity(raw_entity)
                if entity is None or not entity.name:
                    continue
                span = _entity_span(entity)
                if span is None:
                    continue
                start, end = span
                if not _line_sets_intersect(start, end, changed_lines):
                    continue
                kind = _entity_kind(entity)
                key = (norm_file, kind, entity.name)
                surface = _surface_from_parts(
                    file_path=norm_file,
                    name=entity.name,
                    kind=kind,
                    line=start,
                    line_end=end,
                    source="ast_enclosing_diff_hunk",
                    confidence=0.95,
                )
                existing = records.get(key)
                if existing is None or existing.line_end == existing.line_start:
                    records[key] = surface

    inventory_names = [str(item).strip() for item in (inventory or []) if str(item).strip()]
    existing_names = {surface.name for surface in records.values()}
    fallback_file = changed_files[0] if len(changed_files) == 1 else ""
    for name in inventory_names:
        if name in existing_names:
            continue
        file_path = fallback_file
        if not file_path:
            continue
        records.setdefault(
            (file_path, "symbol", name),
            _surface_from_parts(
                file_path=file_path,
                name=name,
                kind="symbol",
                line=first_hunk_line_by_file.get(file_path),
                line_end=first_hunk_line_by_file.get(file_path),
                source="metadata_inventory",
                confidence=0.8,
            ),
        )

    files_with_symbols = {surface.file_path for surface in records.values()}
    for file_path in changed_files:
        if file_path in files_with_symbols:
            continue
        name = PurePosixPath(file_path).name or file_path
        records.setdefault(
            (file_path, "file", name),
            _surface_from_parts(
                file_path=file_path,
                name=name,
                kind="file",
                line=first_hunk_line_by_file.get(file_path),
                line_end=first_hunk_line_by_file.get(file_path),
                source="diff_file",
                confidence=0.7,
            ),
        )

    return sorted(records.values(), key=lambda s: (s.file_path, s.line_start or 10**9, s.name))


def _entities_by_file_from_structural_graph(state: GraphState) -> dict[str, List[CodeEntity]]:
    graph_payload = state.get("structural_graph_node_link") or {}
    if not isinstance(graph_payload, Mapping):
        return {}
    nodes = graph_payload.get("nodes")
    if not isinstance(nodes, list):
        return {}
    out: dict[str, List[CodeEntity]] = {}
    for node in nodes:
        if not isinstance(node, Mapping) or node.get("node_type") != "symbol":
            continue
        file_path = node.get("file_path")
        name = node.get("symbol_name")
        if not isinstance(file_path, str) or not isinstance(name, str) or not name.strip():
            continue
        entity = _coerce_entity(
            {
                "name": name,
                "type": str(node.get("symbol_type") or "symbol"),
                "signature": str(node.get("signature") or ""),
                "body": "",
                "dependencies": [],
                "definition_line": node.get("definition_line"),
                "definition_end_line": node.get("definition_end_line"),
            }
        )
        if entity is not None:
            out.setdefault(normalize_repo_path(file_path), []).append(entity)
    return out


def surface_ledger_from_state(state: GraphState) -> List[ReviewSurface]:
    metadata = state.get("metadata", {}) or {}
    slot = metadata.get("mental_model", {}) if isinstance(metadata, Mapping) else {}
    raw = slot.get("surface_ledger") if isinstance(slot, Mapping) else None
    inventory = slot.get("diff_surface_inventory") if isinstance(slot, Mapping) else None
    inv = [str(item) for item in inventory] if isinstance(inventory, list) else []
    enriched = build_surface_ledger_from_diff(
        str(state.get("git_diff") or ""),
        inventory=inv,
        entities_by_file=_entities_by_file_from_structural_graph(state),
    )
    if isinstance(raw, list) and raw:
        out: List[ReviewSurface] = []
        for item in raw:
            try:
                out.append(item if isinstance(item, ReviewSurface) else ReviewSurface.model_validate(item))
            except Exception:
                continue
        if out:
            return _merge_surface_records([*out, *enriched])
    return enriched


def surface_inventory_names(ledger: Iterable[ReviewSurface]) -> List[str]:
    seen: set[str] = set()
    names: List[str] = []
    for surface in ledger:
        if surface.name and surface.name not in seen:
            seen.add(surface.name)
            names.append(surface.name)
    return names


def compact_surface_ledger_json(ledger: Sequence[ReviewSurface], *, max_records: int = 40) -> str:
    rows = []
    for surface in ledger[:max_records]:
        rows.append(
            {
                "surface_id": surface.surface_id,
                "name": surface.name,
                "kind": surface.kind,
                "file_path": surface.file_path,
                "line_start": surface.line_start,
                "line_end": surface.line_end,
                "confidence": surface.confidence,
                "source": surface.source,
            }
        )
    suffix = [] if len(ledger) <= max_records else [{"truncated_count": len(ledger) - max_records}]
    return json.dumps(rows + suffix, indent=2)


def surface_by_id(ledger: Iterable[ReviewSurface]) -> dict[str, ReviewSurface]:
    return {surface.surface_id: surface for surface in ledger if surface.surface_id}


def surface_ids_for_text(text: str, ledger: Sequence[ReviewSurface]) -> List[str]:
    found: List[str] = []
    for surface in ledger:
        if surface.surface_id in text:
            found.append(surface.surface_id)
            continue
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(surface.name)}(?![A-Za-z0-9_])"
        if any(not _surface_mention_is_negated(text, match.start()) for match in re.finditer(pattern, text)):
            found.append(surface.surface_id)
    return _dedupe(found)


def _surface_mention_is_negated(text: str, start_index: int) -> bool:
    window = text[max(0, start_index - 90) : start_index].lower()
    markers = (
        "without reviewing",
        "without auditing",
        "do not review",
        "don't review",
        "not review",
        "excluding",
        "exclude",
        "except",
        "out of scope",
    )
    return any(marker in window for marker in markers)


def surface_ids_for_task(task: ReviewTask, ledger: Sequence[ReviewSurface]) -> List[str]:
    by_id = surface_by_id(ledger)
    ids = [sid for sid in task.surface_ids if sid in by_id]
    ids.extend(surface_ids_for_text(f"{task.title} {task.description}", ledger))
    if not ids:
        target_files = [normalize_repo_path(path) for path in task.target_files]
        file_ids = [
            surface.surface_id
            for surface in ledger
            if surface.file_path and surface.file_path in target_files
        ]
        blob = f"{task.id} {task.title} {task.description}".lower()
        if len(file_ids) == 1 or task.specialty != "logic" or "structured extraction" in blob:
            ids.extend(file_ids)
    return _dedupe(ids)


def surface_names_for_ids(surface_ids: Iterable[str], ledger: Sequence[ReviewSurface]) -> List[str]:
    by_id = surface_by_id(ledger)
    return [by_id[sid].name for sid in _dedupe(surface_ids) if sid in by_id]


def build_surface_invariants_from_ledger(
    ledger: Sequence[ReviewSurface],
    *,
    risk_hypotheses: str = "",
) -> List[SurfaceInvariant]:
    invariants: List[SurfaceInvariant] = []
    risk = risk_hypotheses.strip()[:300]
    for surface in ledger:
        invariants.append(
            SurfaceInvariant(
                surface_id=surface.surface_id,
                dimension="changed-surface behavior",
                expected_behavior=(
                    f"{surface.name} in {surface.file_path} preserves its existing observable "
                    "contract unless the PR explicitly changes it."
                ),
                risk_hypothesis=risk or "Changed implementation may affect local behavior or contracts.",
                required_evidence=[
                    f"changed implementation for {surface.name}",
                    "repository contract or local caller evidence when the local code is insufficient",
                ],
                out_of_scope=(
                    "Do not infer defects outside this surface without direct changed-code or "
                    "repository-contract evidence."
                ),
            )
        )
    return invariants


def build_migration_invariants_from_diff(
    ledger: Sequence[ReviewSurface],
    git_diff: str,
    *,
    intent_summary: str = "",
    pr_context: str = "",
    risk_hypotheses: str = "",
) -> List[SurfaceInvariant]:
    """Synthesize caller-reliance checks when a PR appears to migrate behavior."""

    blob = f"{intent_summary}\n{pr_context}\n{git_diff}".lower()
    if not any(marker in blob for marker in _MIGRATION_MARKERS):
        return []

    removed_symbols = _symbols_from_removed_lines(git_diff)
    added_calls = _symbols_from_added_call_lines(git_diff)
    old_contract = ", ".join(removed_symbols[:5]) or "the removed or changed implementation"
    new_contract = ", ".join(added_calls[:5]) or "the replacement implementation"
    risk = risk_hypotheses.strip()[:300] or (
        "A migrated call site may drop preconditions, state inputs, lifecycle ordering, "
        "or exception behavior that callers previously relied on."
    )

    invariants: List[SurfaceInvariant] = []
    for surface in ledger:
        if surface.kind == "file":
            continue
        invariants.append(
            SurfaceInvariant(
                surface_id=surface.surface_id,
                dimension="migration caller-reliance contract",
                expected_behavior=(
                    f"{surface.name} preserves caller-visible behavior while migrating from "
                    f"{old_contract} to {new_contract}."
                ),
                risk_hypothesis=risk,
                required_evidence=[
                    f"deleted or old-path diff evidence for {old_contract}",
                    f"changed implementation or call-site evidence for {surface.name}",
                    "new callee signature and required arguments/state inputs",
                    "caller reliance on preconditions, computed state, exception behavior, and lifecycle order",
                ],
                out_of_scope=(
                    "Do not report a migration issue unless exact changed-code and caller/contract "
                    "evidence show a reachable behavior difference."
                ),
            )
        )
        lower_name = surface.name.lower()
        if any(token in lower_name for token in ("cache", "block", "slot", "state", "queue", "resource")):
            invariants.append(
                SurfaceInvariant(
                    surface_id=surface.surface_id,
                    dimension="state/cache lifecycle migration contract",
                    expected_behavior=(
                        f"{surface.name} preserves state/resource lifecycle ordering across the migration."
                    ),
                    risk_hypothesis=risk,
                    required_evidence=[
                        f"changed state/cache lifecycle code for {surface.name}",
                        "old-path lifecycle ordering from deleted diff or repository precedent",
                        "caller evidence for which state objects are passed, released, reused, or invalidated",
                    ],
                    out_of_scope=(
                        "Do not infer lifecycle regressions without concrete ordering, reuse, or invalidation evidence."
                    ),
                )
            )
    return invariants[:12]


def _symbols_from_removed_lines(git_diff: str) -> List[str]:
    symbols: List[str] = []
    for line in git_diff.splitlines():
        if not line.startswith("-") or line.startswith("---"):
            continue
        def_match = _REMOVED_DEF_RE.match(line)
        if def_match:
            symbols.append(def_match.group(1))
            continue
        symbols.extend(match.group(1) for match in _CALL_RE.finditer(line))
    return _dedupe(symbols)


def _symbols_from_added_call_lines(git_diff: str) -> List[str]:
    symbols: List[str] = []
    for line in git_diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        symbols.extend(match.group(1) for match in _CALL_RE.finditer(line))
    return _dedupe(symbols)


def _dedupe(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
