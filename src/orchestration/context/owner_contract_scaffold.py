"""Owner-local factual scaffold for proactive mental-model synthesis."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from src.domain.schemas import ReviewSurface
from src.domain.state import GraphState
from src.orchestration.context.mandate_loop_context import build_repository_contract_context
from src.orchestration.context.surface_ledger import (
    normalize_repo_path,
    surface_ledger_from_state,
)

_DEFAULT_MAX_CHARS = 4_800
_OWNER_SOFT_CHARS = 1_200
_OWNER_HARD_CHARS = 2_800
_MAX_PRIMARY_OWNERS = 12
_MAX_COMPANIONS_PER_OWNER = 3
_WINDOW_CONTEXT = 2
_FILE_READ_MAX_CHARS = 160_000


def changed_lines_by_file_from_diff(git_diff: str) -> Dict[str, set[int]]:
    changed: Dict[str, set[int]] = {}
    current_file = ""
    new_line: int | None = None
    for raw in git_diff.splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split()
            current_file = (
                normalize_repo_path(parts[3].removeprefix("b/"))
                if len(parts) >= 4 and parts[3].startswith("b/")
                else ""
            )
            new_line = None
            continue
        if raw.startswith("+++ b/"):
            current_file = normalize_repo_path(raw.removeprefix("+++ b/"))
            continue
        if raw.startswith("@@"):
            marker = raw.split("@@")[1].strip() if "@@" in raw else ""
            new_part = next((part for part in marker.split() if part.startswith("+")), "")
            if new_part:
                start = new_part[1:].split(",", maxsplit=1)[0]
                try:
                    new_line = int(start)
                except ValueError:
                    new_line = None
            continue
        if not current_file or new_line is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            changed.setdefault(current_file, set()).add(new_line)
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        else:
            new_line += 1
    return changed


def build_owner_contract_scaffold(
    state: GraphState,
    *,
    context_provider: Any | None = None,
    max_chars: int = _DEFAULT_MAX_CHARS,
    owner_soft_chars: int = _OWNER_SOFT_CHARS,
    owner_hard_chars: int = _OWNER_HARD_CHARS,
    max_primary_owners: int = _MAX_PRIMARY_OWNERS,
) -> tuple[str, Dict[str, Any]]:
    """Return compact owner facts and diagnostics without inferring bug meaning."""

    ledger = surface_ledger_from_state(state)
    if not ledger:
        return "[]", {"status": "empty", "primary_owner_count": 0}

    repo_path = _local_repo_path(str(state.get("repo_path") or ""))
    changed_lines = changed_lines_by_file_from_diff(str(state.get("git_diff") or ""))
    file_cache: Dict[str, tuple[List[str], str]] = {}
    primaries, omitted = _primary_surfaces(ledger, max_primary_owners=max_primary_owners)
    companions_by_id = _companion_surfaces_by_primary(primaries, ledger)
    graph_hints = _structural_hints_by_owner(state)

    owner_rows: List[Dict[str, Any]] = []
    diagnostics: Dict[str, Any] = {
        "status": "ok",
        "primary_owner_count": len(primaries),
        "omitted_primary_owners": [surface.name for surface in omitted],
        "owner_snippets": [],
        "repo_convention_status": "omitted",
    }

    for surface in primaries:
        row, row_diag = _owner_row(
            surface,
            state=state,
            repo_path=repo_path,
            context_provider=context_provider,
            file_cache=file_cache,
            changed_lines=changed_lines.get(surface.file_path, set()),
            companions=companions_by_id.get(surface.surface_id, []),
            graph_hints=graph_hints.get(surface.name, []),
            owner_soft_chars=owner_soft_chars,
            owner_hard_chars=owner_hard_chars,
        )
        owner_rows.append(row)
        diagnostics["owner_snippets"].append(row_diag)

    repo_context = build_repository_contract_context(state, max_chars=900)
    if repo_context:
        diagnostics["repo_convention_status"] = "included"
    payload: Dict[str, Any] = {
        "owners": owner_rows,
        "repo_convention_hints": repo_context,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(text) > max_chars:
        payload["repo_convention_hints"] = ""
        diagnostics["repo_convention_status"] = "dropped_for_budget"
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(text) > max_chars:
        stripped = _strip_companion_snippets(payload["owners"])
        if stripped:
            diagnostics["companion_snippets_dropped_for_budget"] = stripped
            text = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(text) > max_chars:
        removed = _drop_companion_rows(payload["owners"])
        if removed:
            diagnostics["companion_rows_dropped_for_budget"] = removed
            text = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(text) > max_chars:
        stripped = _strip_structural_hints(payload["owners"])
        if stripped:
            diagnostics["structural_hints_dropped_for_budget"] = stripped
            text = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(text) > max_chars:
        stripped = _strip_declaration_facts(payload["owners"])
        if stripped:
            diagnostics["declaration_facts_dropped_for_budget"] = stripped
            text = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(text) > max_chars:
        compacted = _compact_primary_snippets(payload["owners"])
        if compacted:
            diagnostics["owners_compacted_for_budget"] = compacted
            text = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(text) > max_chars:
        omitted = _omit_primary_snippets(payload["owners"])
        if omitted:
            diagnostics["owner_snippets_omitted_for_budget"] = omitted
            text = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(text) > max_chars:
        compacted = _compact_owner_rows(payload["owners"])
        if compacted:
            diagnostics["owner_rows_compacted_for_budget"] = compacted
            text = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(text) > max_chars:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) > max_chars:
        stringified = _stringify_compact_owner_rows(payload["owners"])
        if stringified:
            diagnostics["owner_rows_stringified_for_budget"] = stringified
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) > max_chars:
        diagnostics["budget_overflow_chars"] = len(text) - max_chars
    diagnostics["char_len"] = len(text)
    diagnostics["max_chars"] = max_chars
    diagnostics["owner_soft_chars"] = owner_soft_chars
    diagnostics["owner_hard_chars"] = owner_hard_chars
    return text if payload["owners"] else "[]", diagnostics


def _strip_companion_snippets(owners: Sequence[Dict[str, Any]]) -> List[str]:
    stripped: List[str] = []
    for owner in reversed(owners):
        companions = owner.get("companion_surfaces")
        if not isinstance(companions, list):
            continue
        for companion in reversed(companions):
            if not isinstance(companion, dict) or not companion.get("snippet"):
                continue
            companion["snippet"] = ""
            companion["snippet_status"] = "omitted_for_budget"
            stripped.append(str(companion.get("owner") or "unknown"))
    return stripped


def _drop_companion_rows(owners: Sequence[Dict[str, Any]]) -> List[str]:
    dropped: List[str] = []
    for owner in reversed(owners):
        companions = owner.get("companion_surfaces")
        if not isinstance(companions, list) or not companions:
            continue
        for companion in companions:
            if isinstance(companion, dict):
                dropped.append(str(companion.get("owner") or "unknown"))
        owner["companion_surfaces"] = []
    return dropped


def _strip_structural_hints(owners: Sequence[Dict[str, Any]]) -> List[str]:
    stripped: List[str] = []
    for owner in reversed(owners):
        if not isinstance(owner, dict) or not owner.get("structural_hints"):
            continue
        owner["structural_hints"] = []
        stripped.append(str(owner.get("owner") or "unknown"))
    return stripped


def _strip_declaration_facts(owners: Sequence[Dict[str, Any]]) -> List[str]:
    stripped: List[str] = []
    for owner in reversed(owners):
        if not isinstance(owner, dict) or not owner.get("declaration_facts"):
            continue
        owner["declaration_facts"] = []
        stripped.append(str(owner.get("owner") or "unknown"))
    return stripped


def _compact_primary_snippets(owners: Sequence[Dict[str, Any]]) -> List[str]:
    compacted: List[str] = []
    for owner in reversed(owners):
        if not isinstance(owner, dict) or not owner.get("owner_snippet"):
            continue
        lines = str(owner.get("owner_snippet") or "").splitlines()
        if len(lines) <= 6:
            continue
        owner["owner_snippet"] = "\n".join([*lines[:3], "...", *lines[-3:]])
        owner["snippet_status"] = f"compacted_from_{owner.get('snippet_status') or 'unknown'}"
        compacted.append(str(owner.get("owner") or "unknown"))
    return compacted


def _omit_primary_snippets(owners: Sequence[Dict[str, Any]]) -> List[str]:
    omitted: List[str] = []
    for owner in reversed(owners):
        if not isinstance(owner, dict) or not owner.get("owner_snippet"):
            continue
        owner["owner_snippet"] = ""
        owner["snippet_status"] = f"omitted_for_budget_from_{owner.get('snippet_status') or 'unknown'}"
        omitted.append(str(owner.get("owner") or "unknown"))
    return omitted


def _compact_owner_rows(owners: Sequence[Dict[str, Any]]) -> List[str]:
    compacted: List[str] = []
    for index in range(len(owners) - 1, -1, -1):
        owner = owners[index]
        if not isinstance(owner, dict):
            continue
        compacted_owner = {
            "owner": owner.get("owner") or "",
            "file": owner.get("file_path") or "",
            "lines": [owner.get("line_start") or 1, owner.get("line_end") or owner.get("line_start") or 1],
            "source": owner.get("source_status") or "",
            "span": owner.get("span_status") or "",
            "snippet_status": f"compact_row_from_{owner.get('snippet_status') or 'unknown'}",
        }
        owners[index].clear()
        owners[index].update(compacted_owner)
        compacted.append(str(compacted_owner["owner"] or "unknown"))
    return compacted


def _stringify_compact_owner_rows(owners: Sequence[Dict[str, Any]]) -> List[str]:
    stringified: List[str] = []
    for index, owner in enumerate(list(owners)):
        if not isinstance(owner, dict):
            continue
        line_start, line_end = 1, 1
        lines = owner.get("lines")
        if isinstance(lines, list) and len(lines) >= 2:
            line_start, line_end = int(lines[0] or 1), int(lines[1] or lines[0] or 1)
        source = str(owner.get("source") or "")
        if source == "sandbox_provider":
            source = "sandbox"
        span = str(owner.get("span") or "")
        if span == "expanded_ast":
            span = "ast"
        snippet = "compact" if str(owner.get("snippet_status") or "") else ""
        row = (
            f"{owner.get('owner') or ''} "
            f"{owner.get('file') or ''}:{line_start}-{line_end} "
            f"source={source} "
            f"span={span} "
            f"snippet={snippet}"
        ).strip()
        owners[index] = row  # type: ignore[index]
        stringified.append(str(owner.get("owner") or "unknown"))
    return stringified


def _primary_surfaces(
    ledger: Sequence[ReviewSurface],
    *,
    max_primary_owners: int,
) -> tuple[List[ReviewSurface], List[ReviewSurface]]:
    executable = [
        surface
        for surface in ledger
        if surface.kind in {"method", "function", "symbol"} and not _is_schema_helper(surface)
    ]
    primaries = executable or [
        surface for surface in ledger if surface.kind != "file" and not _is_schema_helper(surface)
    ]
    if not primaries:
        primaries = [surface for surface in ledger if surface.kind != "file"]
    ordered = sorted(
        primaries,
        key=lambda s: (
            s.file_path,
            0 if s.kind in {"method", "function"} else 1,
            s.line_start or 10**9,
            s.name,
        ),
    )
    return ordered[:max_primary_owners], ordered[max_primary_owners:]


def _is_schema_helper(surface: ReviewSurface) -> bool:
    name = surface.name.rsplit(".", maxsplit=1)[-1].lower()
    return name in {"input_types", "return_types"} or surface.kind == "class"


def _class_prefix(name: str) -> str:
    return name.split(".", maxsplit=1)[0] if "." in name else ""


def _companion_surfaces_by_primary(
    primaries: Sequence[ReviewSurface],
    ledger: Sequence[ReviewSurface],
) -> Dict[str, List[ReviewSurface]]:
    out: Dict[str, List[ReviewSurface]] = {}
    primary_ids = {surface.surface_id for surface in primaries}
    for primary in primaries:
        prefix = _class_prefix(primary.name)
        companions: List[ReviewSurface] = []
        for surface in ledger:
            if surface.surface_id in primary_ids or surface.file_path != primary.file_path:
                continue
            same_class = prefix and (surface.name == prefix or surface.name.startswith(f"{prefix}."))
            if same_class:
                companions.append(surface)
        out[primary.surface_id] = sorted(
            companions,
            key=lambda s: (0 if _is_schema_helper(s) else 1, s.line_start or 10**9, s.name),
        )[:_MAX_COMPANIONS_PER_OWNER]
    return out


def _owner_row(
    surface: ReviewSurface,
    *,
    state: GraphState,
    repo_path: Path | None,
    context_provider: Any | None,
    file_cache: Dict[str, tuple[List[str], str]],
    changed_lines: set[int],
    companions: Sequence[ReviewSurface],
    graph_hints: Sequence[str],
    owner_soft_chars: int,
    owner_hard_chars: int,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    lines, source_status = _read_file_lines(
        state,
        repo_path,
        surface.file_path,
        file_cache,
        context_provider=context_provider,
    )
    effective_surface, span_status = _expand_surface_span(surface, lines)
    snippet, status = _surface_snippet(
        effective_surface,
        lines,
        changed_lines=changed_lines,
        owner_soft_chars=owner_soft_chars,
        owner_hard_chars=owner_hard_chars,
    )
    companion_rows = []
    for companion in companions:
        companion_lines, companion_source_status = _read_file_lines(
            state,
            repo_path,
            companion.file_path,
            file_cache,
            context_provider=context_provider,
        )
        effective_companion, companion_span_status = _expand_surface_span(companion, companion_lines)
        companion_snippet, companion_status = _surface_snippet(
            effective_companion,
            companion_lines,
            changed_lines=changed_lines,
            owner_soft_chars=max(400, owner_soft_chars // 2),
            owner_hard_chars=max(800, owner_hard_chars // 2),
        )
        companion_rows.append(
            {
                "owner": companion.name,
                "kind": companion.kind,
                "line_start": effective_companion.line_start,
                "line_end": effective_companion.line_end,
                "source_status": companion_source_status,
                "span_status": companion_span_status,
                "snippet_status": companion_status,
                "snippet": companion_snippet,
            }
        )
    row = {
        "owner": surface.name,
        "surface_id": surface.surface_id,
        "kind": surface.kind,
        "file_path": surface.file_path,
        "line_start": effective_surface.line_start,
        "line_end": effective_surface.line_end,
        "source_status": source_status,
        "span_status": span_status,
        "changed_lines": sorted(line for line in changed_lines if _line_in_surface(line, effective_surface))[:40],
        "evidence_refs": list(surface.evidence_refs[:6]),
        "snippet_status": status,
        "owner_snippet": snippet,
        "declaration_facts": _declaration_facts(lines, effective_surface),
        "companion_surfaces": companion_rows,
        "structural_hints": list(graph_hints[:6]),
    }
    diag = {
        "owner": surface.name,
        "file_path": surface.file_path,
        "source_status": source_status,
        "span_status": span_status,
        "line_start": effective_surface.line_start,
        "line_end": effective_surface.line_end,
        "snippet_status": status,
        "snippet_chars": len(snippet),
        "companion_count": len(companion_rows),
    }
    return row, diag


def _local_repo_path(repo_path: str) -> Path | None:
    if not repo_path:
        return None
    try:
        path = Path(repo_path).resolve()
    except Exception:
        return None
    return path if path.is_dir() else None


def _read_file_lines(
    state: GraphState,
    repo_path: Path | None,
    file_path: str,
    cache: Dict[str, tuple[List[str], str]],
    *,
    context_provider: Any | None,
) -> tuple[List[str], str]:
    norm = normalize_repo_path(file_path)
    if norm in cache:
        return cache[norm]
    text = ""
    source_status = "unavailable"
    if repo_path is not None:
        try:
            target = (repo_path / norm).resolve()
            target.relative_to(repo_path)
            text = target.read_text(encoding="utf-8", errors="replace")
            source_status = "local_repo"
        except Exception:
            text = ""
    if not text and context_provider is not None:
        try:
            get_sandbox = getattr(context_provider, "get_sandbox", None)
            if callable(get_sandbox):
                get_sandbox(state)
            read_full = getattr(context_provider, "read_full_file", None)
            if callable(read_full):
                text = str(read_full(norm, max_chars=_FILE_READ_MAX_CHARS) or "")
                if text:
                    source_status = "sandbox_provider"
        except Exception:
            text = ""
    if not text:
        text = _state_file_text(state, norm)
        if text:
            source_status = "state_fallback"
    cache[norm] = (text.splitlines(), source_status)
    return cache[norm]


def _state_file_text(state: GraphState, file_path: str) -> str:
    for raw in (state.get("focused_context_results", {}) or {}).values():
        if not isinstance(raw, Mapping):
            continue
        for key in ("file_contents_full", "file_snippets"):
            values = raw.get(key)
            if not isinstance(values, Mapping):
                continue
            text = values.get(file_path) or values.get(file_path.replace("/", "\\"))
            if isinstance(text, str) and text.strip():
                return text
    metadata = state.get("metadata", {}) or {}
    review_checks = metadata.get("review_checks") if isinstance(metadata, Mapping) else None
    by_task = review_checks.get("by_task") if isinstance(review_checks, Mapping) else None
    if isinstance(by_task, Mapping):
        for task_meta in by_task.values():
            if not isinstance(task_meta, Mapping):
                continue
            evidence = task_meta.get("task_evidence")
            if not isinstance(evidence, Mapping):
                continue
            files = evidence.get("file_contents")
            if not isinstance(files, Mapping):
                continue
            text = files.get(file_path) or files.get(file_path.replace("/", "\\"))
            if isinstance(text, str) and text.strip():
                return text
    return _file_text_from_added_diff(str(state.get("git_diff") or ""), file_path)


def _file_text_from_added_diff(git_diff: str, file_path: str) -> str:
    current_file = ""
    lines: List[str] = []
    for raw in git_diff.splitlines():
        if raw.startswith("diff --git "):
            current_file = ""
            parts = raw.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                current_file = normalize_repo_path(parts[3].removeprefix("b/"))
            continue
        if raw.startswith("+++ b/"):
            current_file = normalize_repo_path(raw.removeprefix("+++ b/"))
            continue
        if current_file != file_path:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            lines.append(raw[1:])
        elif raw.startswith(" ") and lines:
            lines.append(raw[1:])
    return "\n".join(lines)


def _expand_surface_span(surface: ReviewSurface, lines: Sequence[str]) -> tuple[ReviewSurface, str]:
    if not lines or not surface.file_path.endswith(".py") or not surface.line_start:
        return surface, "unavailable" if not lines else "unchanged"
    current_end = surface.line_end or surface.line_start
    if current_end > surface.line_start:
        return surface, "provided"
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:
        return surface, "parse_failed"
    target_name = surface.name.rsplit(".", maxsplit=1)[-1]
    class_name = _class_prefix(surface.name)
    matches: List[tuple[int, int, int, ast.AST]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if not (start <= surface.line_start <= end):
            continue
        name = getattr(node, "name", "")
        if surface.kind in {"method", "function", "symbol"} and name != target_name:
            if not (surface.kind == "symbol" and name == class_name):
                continue
        if surface.kind == "class" and name != surface.name:
            continue
        matches.append((end - start, start, end, node))
    if not matches:
        return surface, "no_enclosing_ast"
    _, start, end, _node = sorted(matches, key=lambda item: (item[0], item[1]))[0]
    return surface.model_copy(update={"line_start": start, "line_end": end}), "expanded_ast"


def _surface_snippet(
    surface: ReviewSurface,
    lines: Sequence[str],
    *,
    changed_lines: set[int],
    owner_soft_chars: int,
    owner_hard_chars: int,
) -> tuple[str, str]:
    if not lines:
        return "", "unavailable"
    if surface.line_start and surface.line_end:
        full = _numbered_slice(lines, surface.line_start, surface.line_end)
        if len(full) <= owner_soft_chars:
            return full, "full_ast_span"
        if len(full) <= owner_hard_chars and any(_line_in_surface(line, surface) for line in changed_lines):
            return full, "full_ast_span_over_soft"
        child = _changed_child_blocks(lines, surface, changed_lines, max_chars=owner_hard_chars)
        if child:
            return child, "degraded_complete_child_blocks"
    hunk = _changed_windows(lines, changed_lines, max_chars=owner_hard_chars)
    if hunk:
        return hunk, "degraded_changed_hunks"
    if surface.line_start:
        start = max(1, surface.line_start - _WINDOW_CONTEXT)
        end = min(len(lines), surface.line_start + _WINDOW_CONTEXT)
        return _numbered_slice(lines, start, end), "degraded_signature_context"
    return "", "unavailable"


def _line_in_surface(line: int, surface: ReviewSurface) -> bool:
    if surface.line_start is None:
        return False
    end = surface.line_end or surface.line_start
    return surface.line_start <= line <= end


def _numbered_slice(lines: Sequence[str], start: int, end: int) -> str:
    start = max(1, start)
    end = min(len(lines), max(start, end))
    return "\n".join(f"{line_no}: {lines[line_no - 1]}" for line_no in range(start, end + 1))


def _changed_windows(lines: Sequence[str], changed_lines: Iterable[int], *, max_chars: int) -> str:
    parts: List[str] = []
    used = 0
    for line in sorted(set(changed_lines)):
        start = max(1, line - _WINDOW_CONTEXT)
        end = min(len(lines), line + _WINDOW_CONTEXT)
        block = _numbered_slice(lines, start, end)
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n---\n".join(parts)


def _changed_child_blocks(
    lines: Sequence[str],
    surface: ReviewSurface,
    changed_lines: set[int],
    *,
    max_chars: int,
) -> str:
    if not surface.file_path.endswith(".py") or not changed_lines:
        return ""
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:
        return ""
    blocks: List[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if not _line_in_surface(start, surface) or not _line_in_surface(end, surface):
            continue
        if any(start <= line <= end for line in changed_lines):
            blocks.append((start, end))
    merged: List[tuple[int, int]] = []
    for start, end in sorted(blocks, key=lambda item: (item[0], item[1] - item[0])):
        if merged and start <= merged[-1][1]:
            continue
        merged.append((start, end))
    parts: List[str] = []
    used = 0
    for start, end in merged:
        block = _numbered_slice(lines, start, end)
        if len(block) > max_chars:
            continue
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n---\n".join(parts)


def _declaration_facts(lines: Sequence[str], surface: ReviewSurface) -> List[str]:
    if not lines or not surface.line_start:
        return []
    start = max(1, surface.line_start - 8)
    end = min(len(lines), (surface.line_end or surface.line_start) + 8)
    facts: List[str] = []
    for line_no in range(start, end + 1):
        text = lines[line_no - 1].strip()
        if not text:
            continue
        if text.startswith("@") or text.startswith(("def ", "async def ", "class ")):
            facts.append(f"{line_no}: {text[:240]}")
        elif "=" in text and text.split("=", maxsplit=1)[0].strip().isupper():
            facts.append(f"{line_no}: {text[:240]}")
        if len(facts) >= 12:
            break
    return facts


def _structural_hints_by_owner(state: GraphState) -> Dict[str, List[str]]:
    graph = state.get("structural_graph_node_link") or {}
    if not isinstance(graph, Mapping):
        return {}
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return {}
    node_by_id = {
        str(node.get("id")): node
        for node in nodes
        if isinstance(node, Mapping) and node.get("id") is not None
    }
    symbol_id_by_name = {
        str(node.get("symbol_name")): str(node.get("id"))
        for node in nodes
        if isinstance(node, Mapping) and node.get("symbol_name")
    }
    hints: Dict[str, List[str]] = {}
    for name, node_id in symbol_id_by_name.items():
        rows: List[str] = []
        for edge in edges:
            if not isinstance(edge, Mapping):
                continue
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if source != node_id and target != node_id:
                continue
            other = target if source == node_id else source
            node = node_by_id.get(other)
            if not isinstance(node, Mapping):
                continue
            label = str(node.get("symbol_name") or node.get("file_path") or other)
            if label and label not in rows:
                rows.append(label)
            if len(rows) >= 6:
                break
        if rows:
            hints[name] = rows
    return hints
