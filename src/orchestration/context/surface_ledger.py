"""Typed changed-surface ledger helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any, Iterable, List, Mapping, Sequence

from src.domain.schemas import CodeEntity, ContractQuestion, ReviewSurface, ReviewTask, SurfaceInvariant
from src.domain.state import GraphState
from src.orchestration.context.contract_vocabulary import (
    DATA_SHAPE_CONTRACT_TERMS,
    has_any_contract_term,
)

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


def _coerce_path_list(raw: Any) -> List[str]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in raw:
        path = normalize_repo_path(str(item or ""))
        if path and path != "/dev/null" and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _metadata_mapping(state: GraphState) -> Mapping[str, Any]:
    metadata = state.get("metadata", {}) or {}
    return metadata if isinstance(metadata, Mapping) else {}


def _mapping_child(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = parent.get(key)
    return value if isinstance(value, Mapping) else {}


def changed_file_sources_from_state(state: GraphState) -> dict[str, List[str]]:
    """Collect changed-file lists from trusted run metadata and overlays."""

    sources: dict[str, List[str]] = {
        "git_diff": changed_files_from_diff(str(state.get("git_diff") or "")),
    }
    for key in (
        "changed_files",
        "changed_file_paths",
        "benchmark_changed_files",
        "pr_changed_files",
        "review_changed_files",
    ):
        values = _coerce_path_list(state.get(key))
        if values:
            sources[f"state.{key}"] = values

    metadata = _metadata_mapping(state)
    for key in (
        "changed_files",
        "changed_file_paths",
        "benchmark_changed_files",
        "pr_changed_files",
        "review_changed_files",
    ):
        values = _coerce_path_list(metadata.get(key))
        if values:
            sources[f"metadata.{key}"] = values

    repository_kb = _mapping_child(metadata, "repository_kb")
    semantic_phase2 = _mapping_child(metadata, "semantic_phase2")
    review_kb = _mapping_child(semantic_phase2, "review_kb")
    nested_sources = (
        ("metadata.review_history_context", metadata.get("review_history_context")),
        ("metadata.docs_prebrief", metadata.get("docs_prebrief")),
        ("metadata.repository_kb.review_overlay", repository_kb.get("review_overlay")),
        ("metadata.semantic_phase2.review_kb.review_overlay", review_kb.get("review_overlay")),
    )
    for source_name, raw_mapping in nested_sources:
        if not isinstance(raw_mapping, Mapping):
            continue
        values = _coerce_path_list(raw_mapping.get("changed_files") or raw_mapping.get("changed_file_paths"))
        if values:
            sources[source_name] = values
    return sources


def changed_file_integrity_diagnostics(state: GraphState) -> dict[str, Any]:
    sources = changed_file_sources_from_state(state)
    diff_files = set(sources.get("git_diff", []))
    trusted_sources = {name: paths for name, paths in sources.items() if name != "git_diff"}
    trusted_union = sorted({path for paths in trusted_sources.values() for path in paths})
    missing_from_diff_by_source = {
        name: sorted(path for path in paths if path not in diff_files)
        for name, paths in trusted_sources.items()
        if any(path not in diff_files for path in paths)
    }
    extra_in_diff_by_source = {
        name: sorted(path for path in diff_files if path not in set(paths))
        for name, paths in trusted_sources.items()
        if diff_files and any(path not in set(paths) for path in diff_files)
    }
    status = "degraded" if missing_from_diff_by_source else "ok"
    return {
        "status": status,
        "sources": sources,
        "trusted_union": trusted_union,
        "missing_from_diff_by_source": missing_from_diff_by_source,
        "extra_in_diff_by_source": extra_in_diff_by_source,
        "missing_from_diff_union": sorted(path for path in trusted_union if path not in diff_files),
    }


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
    current_added_class: str | None = None
    first_hunk_line_by_file: dict[str, int] = {}
    changed_lines_by_file: dict[str, set[int]] = {}

    for raw in git_diff.splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split()
            current_file = ""
            current_added_class = None
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
                if class_match:
                    current_added_class = name
                elif raw.startswith("+def ") or raw.startswith("+async def "):
                    current_added_class = None
                elif current_added_class:
                    kind = "method"
                    name = f"{current_added_class}.{name}"
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
                if class_match:
                    current_added_class = name
                elif raw.startswith("+def ") or raw.startswith("+async def "):
                    current_added_class = None
                elif current_added_class:
                    kind = "method"
                    name = f"{current_added_class}.{name}"
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
    diagnostics = changed_file_integrity_diagnostics(state)
    existing_files = {surface.file_path for surface in enriched}
    for file_path in diagnostics.get("missing_from_diff_union", []):
        if file_path in existing_files:
            continue
        enriched.append(
            _surface_from_parts(
                file_path=file_path,
                name=PurePosixPath(file_path).name or file_path,
                kind="file",
                line=None,
                line_end=None,
                source="changed_file_integrity_guard",
                confidence=0.65,
            )
        )
        existing_files.add(file_path)
    if isinstance(raw, list) and raw:
        out: List[ReviewSurface] = []
        for item in raw:
            try:
                out.append(item if isinstance(item, ReviewSurface) else ReviewSurface.model_validate(item))
            except Exception:
                continue
        if out:
            return _merge_surface_records([*out, *enriched])
    return _merge_surface_records(enriched)


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
    normalized_text = _normalized_surface_mention(text)
    for surface in ledger:
        if surface.surface_id in text:
            found.append(surface.surface_id)
            continue
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(surface.name)}(?![A-Za-z0-9_])"
        matches = list(re.finditer(pattern, text))
        if matches:
            if any(not _surface_mention_is_negated(text, match.start()) for match in matches):
                found.append(surface.surface_id)
            continue
        normalized_name = _normalized_surface_mention(surface.name)
        if normalized_name and normalized_name in normalized_text:
            found.append(surface.surface_id)
    return _dedupe(found)


def _normalized_surface_mention(text: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text or "")
    return " ".join(re.sub(r"[^A-Za-z0-9]+", " ", spaced).lower().split())


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
        if len(file_ids) == 1 or task.specialty != "logic" or "focused contract" in blob:
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
    for surface in _primary_contract_surfaces(ledger):
        base_evidence = [
            f"changed implementation for {surface.name}",
            "repository contract or local caller evidence when the local code is insufficient",
        ]
        signal_blob = f"{surface.name} {surface.file_path} {risk}".lower()
        if has_any_contract_term(signal_blob, DATA_SHAPE_CONTRACT_TERMS) or "tensor" in signal_blob:
            dimension = "data shape consistency"
            expected_behavior = (
                f"{surface.name} preserves structured values, element ordering, relevant fields, "
                "cardinality/completeness, and expected container/tensor shapes across the changed path."
            )
            required_evidence = [
                *base_evidence,
                "producer and consumer expectations for structured values, fields, and cardinality at this surface",
            ]
            out_of_scope = "Do not invent shape requirements without local code or caller evidence."
        elif any(token in signal_blob for token in ("state", "cache", "resource", "memory", "vram", "checkpoint", "load", "save", "move", "train")):
            dimension = "state/resource lifecycle"
            expected_behavior = (
                f"{surface.name} preserves state transitions, ownership, cleanup, and resource "
                "lifecycle order expected by callers."
            )
            required_evidence = [
                *base_evidence,
                "state/resource ownership and lifecycle ordering around this changed surface",
            ]
            out_of_scope = "Do not report lifecycle concerns without concrete changed ordering or ownership evidence."
        elif any(token in signal_blob for token in ("path", "file", "permission", "auth", "security", "folder")):
            dimension = "security boundary"
            expected_behavior = (
                f"{surface.name} preserves repository security boundaries for user-controlled inputs "
                "and filesystem or permission-sensitive operations."
            )
            required_evidence = [
                *base_evidence,
                "source of external input and boundary validation for this surface",
            ]
            out_of_scope = "Do not report generic hardening without a reachable changed-code boundary."
        else:
            dimension = "changed-surface behavior"
            expected_behavior = (
                f"{surface.name} in {surface.file_path} preserves its externally visible contract "
                "unless the PR explicitly changes it."
            )
            required_evidence = base_evidence
            out_of_scope = (
                "Do not infer defects outside this surface without direct changed-code or "
                "repository-contract evidence."
            )
        invariants.append(
            SurfaceInvariant(
                surface_id=surface.surface_id,
                dimension=dimension,
                expected_behavior=expected_behavior,
                risk_hypothesis=risk or "Changed implementation may affect local behavior or contracts.",
                required_evidence=required_evidence,
                out_of_scope=out_of_scope,
            )
        )
    return invariants


def build_contract_questions_from_ledger(
    ledger: Sequence[ReviewSurface],
    *,
    risk_hypotheses: str = "",
    existing_questions: Sequence[ContractQuestion] = (),
) -> List[ContractQuestion]:
    """Generate compact owner-scoped fallback questions from structural surfaces."""

    questions: List[ContractQuestion] = []
    risk = risk_hypotheses.lower()
    existing_families = {
        (question.owner.strip().lower(), question.dimension)
        for question in existing_questions
        if question.owner.strip() and question.breach_question.strip()
    }

    def should_add(owner: str, dimension: str) -> bool:
        return (owner.strip().lower(), dimension) not in existing_families

    for surface in _primary_contract_surfaces(ledger):
        if surface.kind == "file":
            continue
        owner = surface.name
        base_evidence = f"changed implementation and declared contract for {owner}"
        if should_add(owner, "return_output_totality"):
            questions.append(
                ContractQuestion(
                    owner=owner,
                    surface_id=surface.surface_id,
                    dimension="return_output_totality",
                    expected_behavior=f"{owner} returns the declared output shape on every reachable execution path.",
                    contract_evidence=base_evidence,
                    trigger_variant="each declared branch, fallback/default path, and error path",
                    operation="execute return contract",
                    breach_question=(
                        "Can any reachable branch, fallback/default path, or handled-error path fail to return "
                        "the declared output shape?"
                    ),
                    direct_suppressor=(
                        "Concrete evidence shows every reachable branch and fallback/default path returns the declared output shape."
                    ),
                    required_evidence=[
                        base_evidence,
                        "declared output shape",
                        "branch/fallback/default behavior",
                    ],
                    source_confidence=0.35,
                )
            )
        owner_blob = f"{owner} {surface.file_path}".lower()
        risk_blob = risk if owner.lower() in risk else ""
        if (
            any(
                token in f"{owner_blob} {risk_blob}"
                for token in (
                    "mode",
                    "variant",
                    "option",
                    "input_types",
                    "combo",
                    "dispatch",
                    "case",
                    "compare",
                    "convert",
                    "regex",
                    "extract",
                )
            )
            and should_add(owner, "variant_completeness")
        ):
            questions.append(
                ContractQuestion(
                    owner=owner,
                    surface_id=surface.surface_id,
                    dimension="variant_completeness",
                    expected_behavior=f"{owner} handles declared variants distinctly and handles unsupported/default variants explicitly.",
                    contract_evidence=base_evidence,
                    trigger_variant="declared options plus unsupported/default variant",
                    operation="variant dispatch",
                    breach_question=(
                        "Are declared variants and unsupported/default variants handled without falling through, "
                        "silently reusing an unrelated behavior, or returning the wrong output shape?"
                    ),
                    direct_suppressor=(
                        "Concrete evidence shows declared variants and unsupported/default variants are explicitly handled."
                    ),
                    required_evidence=[
                        base_evidence,
                        "declared variant/options contract",
                        "variant dispatch implementation",
                    ],
                    source_confidence=0.35,
                )
            )
        if any(
            token in f"{owner_blob} {risk_blob}"
            for token in (
                "extract",
                "parse",
                "serialize",
                "format",
                "join",
                "group",
                "record",
                "field",
                "batch",
                "collection",
                "match",
            )
        ):
            data_operation = "collection producer and element projection/index selection"
            if should_add(owner, "data_preservation_cardinality"):
                questions.append(
                    ContractQuestion(
                        owner=owner,
                        surface_id=surface.surface_id,
                        dimension="data_preservation_cardinality",
                        expected_behavior=(
                            f"{owner} preserves produced collection payloads and selected elements according "
                            "to the operation contract."
                        ),
                        contract_evidence=base_evidence,
                        trigger_variant="multi-item, nested, grouped, optional, empty, and single-item values",
                        operation=data_operation,
                        breach_question=(
                            "Can the changed producer/projection path select the wrong element, drop payload data, "
                            "or lose part of a structured value without an explicit contract narrowing?"
                        ),
                        direct_suppressor=(
                            "Concrete evidence shows producer cardinality and projection/index selection preserve the intended payload."
                        ),
                        required_evidence=[
                            base_evidence,
                            "producer cardinality and element payload contract",
                            "projection/index/aggregation path",
                        ],
                        source_confidence=0.35,
                    )
                )
            if should_add(owner, "serialization_type_closure"):
                questions.append(
                    ContractQuestion(
                        owner=owner,
                        surface_id=surface.surface_id,
                        dimension="serialization_type_closure",
                        expected_behavior=f"{owner} only sends values compatible with its declared output type into output assembly.",
                        contract_evidence=base_evidence,
                        trigger_variant="optional, absent, nested, non-string, or empty values",
                        operation="output serialization",
                        breach_question=(
                            "Can optional, absent, nested, or non-output-compatible values reach output assembly and violate "
                            "the declared output type?"
                        ),
                        direct_suppressor=(
                            "Concrete evidence shows output assembly normalizes or rejects optional, absent, nested, and non-compatible values."
                        ),
                        required_evidence=[
                            base_evidence,
                            "declared output type",
                            "output assembly and normalization path",
                        ],
                        source_confidence=0.35,
                    )
                )
        if len(questions) >= 40:
            break
    return questions[:40]


def _primary_contract_surfaces(ledger: Sequence[ReviewSurface]) -> List[ReviewSurface]:
    grouped: dict[tuple[str, str], List[ReviewSurface]] = {}
    standalone: List[ReviewSurface] = []
    for surface in ledger:
        if surface.kind == "file":
            standalone.append(surface)
            continue
        base = surface.name.split(".", 1)[0]
        grouped.setdefault((surface.file_path, base), []).append(surface)

    selected: List[ReviewSurface] = []
    for _key, surfaces in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        execute = [
            surface for surface in surfaces
            if surface.name.rsplit(".", 1)[-1].lower() in {"execute", "run", "handle", "process", "call", "__call__"}
        ]
        if execute:
            selected.append(sorted(execute, key=lambda item: item.line_start or 10**9)[0])
            continue
        non_helper = [
            surface for surface in surfaces
            if "input_types" not in surface.name.lower()
        ]
        selected.append(sorted(non_helper or surfaces, key=lambda item: (item.line_start or 10**9, item.name))[0])
    return sorted([*selected, *standalone], key=lambda surface: (surface.file_path, surface.line_start or 10**9, surface.name))


def build_migration_invariants_from_diff(
    ledger: Sequence[ReviewSurface],
    git_diff: str,
    *,
    intent_summary: str = "",
    pr_context: str = "",
    risk_hypotheses: str = "",
) -> List[SurfaceInvariant]:
    """Synthesize caller-reliance checks when a PR appears to migrate behavior."""

    removed_symbols = _symbols_from_removed_lines(git_diff)
    if not removed_symbols:
        return []
    blob = f"{intent_summary}\n{pr_context}\n{git_diff}".lower()
    if not any(marker in blob for marker in _MIGRATION_MARKERS):
        return []

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
