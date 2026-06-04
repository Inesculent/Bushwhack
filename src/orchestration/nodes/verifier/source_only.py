"""Cheap verifier checks that do not import repository packages."""

from __future__ import annotations

import ast
import re
from typing import Any, Dict, Iterable, Tuple

from src.domain.schemas import SourceFact
from src.domain.verifier_schemas import VerifierAttemptRecord
from src.orchestration.context.task_evidence import task_evidence_slot_from_state

_SOURCE_FACT_SUMMARY_LIMIT = 700
_SOURCE_FACT_EVIDENCE_LIMIT = 1000


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("/")


def _clamp_text(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    suffix = "\n... [truncated]"
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def _target_source_from_state(state: Dict[str, Any], candidate: Dict[str, Any]) -> tuple[str, bool]:
    tid = str(candidate.get("patch_task_id") or "")
    fp = _norm(str(candidate.get("file_path") or ""))
    if not tid or not fp:
        return "", False
    slot = task_evidence_slot_from_state(state, tid)
    files = slot.get("file_contents") if isinstance(slot.get("file_contents"), dict) else {}
    complete = slot.get("files_complete") if isinstance(slot.get("files_complete"), dict) else {}
    for raw_path, body in files.items():
        path = _norm(str(raw_path))
        if path == fp or path.endswith("/" + fp) or fp.endswith("/" + path):
            return str(body or ""), bool(complete.get(path) or complete.get(fp) or complete.get(str(raw_path)))
    return "", False


def _removed_import_names(git_diff: str, file_path: str) -> set[str]:
    target = _norm(file_path)
    current = ""
    names: set[str] = set()
    for raw in (git_diff or "").splitlines():
        if raw.startswith("+++ b/"):
            current = _norm(raw[6:].strip())
            continue
        if current != target or not raw.startswith("-") or raw.startswith("---"):
            continue
        line = raw[1:].strip()
        if line.startswith("import "):
            rest = line.removeprefix("import ").split("#", 1)[0]
            for part in rest.split(","):
                name = part.strip().split(" as ", 1)[0].split(".", 1)[0]
                if name:
                    names.add(name)
        elif line.startswith("from "):
            try:
                mod, imports = line.removeprefix("from ").split(" import ", 1)
            except ValueError:
                continue
            if imports.strip() == "*":
                names.add(mod.strip().split(".", 1)[0])
                continue
            for part in imports.split(","):
                name = part.strip().split(" as ", 1)[-1].strip()
                if name:
                    names.add(name)
    return names


def _defined_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_names_bound_by_target(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_names_bound_by_target(node.target))
    return names


def _names_bound_by_target(target: ast.AST) -> Iterable[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            yield from _names_bound_by_target(item)


def _used_names(tree: ast.AST) -> set[str]:
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _candidate_blob(candidate: Dict[str, Any]) -> str:
    return " ".join(
        str(candidate.get(key) or "")
        for key in ("content", "failure_mode", "evidence_summary", "recommendation")
    ).lower()


def _node_span(node: ast.AST) -> tuple[int, int]:
    start = int(getattr(node, "lineno", 1) or 1)
    end = int(getattr(node, "end_lineno", start) or start)
    return start, end


def _target_functions(tree: ast.AST, candidate: Dict[str, Any]) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    line_start = int(candidate.get("line_start") or 0)
    blob = _candidate_blob(candidate)
    funcs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    class_funcs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        start, end = _node_span(node)
        class_matches_line = bool(line_start and start <= line_start <= end)
        class_matches_text = bool(
            re.search(rf"(?<![A-Za-z0-9_]){re.escape(node.name.lower())}(?![A-Za-z0-9_])", blob)
        )
        if not class_matches_line and not class_matches_text:
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                class_funcs.append(item)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start, end = _node_span(node)
        if line_start and start <= line_start <= end:
            funcs.append(node)
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(node.name.lower())}(?![A-Za-z0-9_])", blob):
            funcs.append(node)
    return list(dict.fromkeys([*funcs, *class_funcs]))


def _stmt_guarantees_exit(stmt: ast.stmt) -> bool:
    if isinstance(stmt, (ast.Return, ast.Raise)):
        return True
    if isinstance(stmt, ast.If):
        return bool(stmt.body and stmt.orelse) and _block_guarantees_exit(stmt.body) and _block_guarantees_exit(stmt.orelse)
    if isinstance(stmt, ast.Try):
        return bool(stmt.body) and _block_guarantees_exit(stmt.body) and all(
            _block_guarantees_exit(handler.body) for handler in stmt.handlers
        )
    return False


def _block_guarantees_exit(stmts: Iterable[ast.stmt]) -> bool:
    for stmt in stmts:
        if _stmt_guarantees_exit(stmt):
            return True
    return False


def _source_segment(source: str, node: ast.AST) -> str:
    lines = source.splitlines()
    start, end = _node_span(node)
    return "\n".join(lines[start - 1 : end])


def _contains_zero_subscript(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    idx = node.slice
    if isinstance(idx, ast.Constant):
        return idx.value == 0
    return False


def _returns_or_projects_first_slot(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and node.value is not None and any(
            _contains_zero_subscript(child) for child in ast.walk(node.value)
        ):
            return True
        if isinstance(node, ast.ListComp) and any(
            _contains_zero_subscript(child) for child in ast.walk(node.elt)
        ):
            return True
    return False


def _fact(
    candidate: Dict[str, Any],
    *,
    kind: str,
    file_path: str,
    summary: str,
    evidence: str = "",
    line_start: int | None = None,
    line_end: int | None = None,
) -> SourceFact:
    return SourceFact(
        candidate_id=str(candidate.get("candidate_id") or ""),
        fact_kind=kind,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        summary=_clamp_text(summary, _SOURCE_FACT_SUMMARY_LIMIT),
        evidence=_clamp_text(evidence, _SOURCE_FACT_EVIDENCE_LIMIT),
    )


def extract_source_facts_for_candidate(
    state: Dict[str, Any],
    candidate: Dict[str, Any],
) -> list[SourceFact]:
    """Return deterministic source facts without deciding whether the candidate is valid."""
    source, source_complete = _target_source_from_state(state, candidate)
    if not source.strip():
        return []
    fp = _norm(str(candidate.get("file_path") or ""))
    facts: list[SourceFact] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        if source_complete:
            facts.append(
                _fact(
                    candidate,
                    kind="syntax_error",
                    file_path=fp,
                    line_start=exc.lineno,
                    line_end=exc.lineno,
                    summary=f"{fp} has SyntaxError during source parse: {exc.msg}",
                    evidence=str(exc),
                )
            )
        return facts

    removed = _removed_import_names(str(state.get("git_diff") or ""), fp)
    if removed:
        defined = _defined_names(tree)
        still_used = sorted((removed & _used_names(tree)) - defined)
        if still_used:
            facts.append(
                _fact(
                    candidate,
                    kind="removed_import_still_used",
                    file_path=fp,
                    summary=f"{fp} removed import(s) still used by name: {', '.join(still_used[:8])}",
                    evidence=", ".join(still_used[:8]),
                )
            )

    for func in _target_functions(tree, candidate):
        start, end = _node_span(func)
        segment = _source_segment(source, func)
        if not _block_guarantees_exit(func.body):
            facts.append(
                _fact(
                    candidate,
                    kind="reachable_fallthrough",
                    file_path=fp,
                    line_start=start,
                    line_end=end,
                    summary=f"{func.name} has a reachable path without an explicit return or raise.",
                    evidence=segment,
                )
            )
        if _returns_or_projects_first_slot(func):
            facts.append(
                _fact(
                    candidate,
                    kind="first_slot_projection",
                    file_path=fp,
                    line_start=start,
                    line_end=end,
                    summary=f"{func.name} returns or constructs a value by selecting element 0.",
                    evidence=segment,
                )
            )
        lowered = segment.lower()
        if ".join(" in lowered:
            facts.append(
                _fact(
                    candidate,
                    kind="join_aggregation",
                    file_path=fp,
                    line_start=start,
                    line_end=end,
                    summary=f"{func.name} aggregates values with join.",
                    evidence=segment,
                )
            )
    return facts


def source_only_verify_candidate(
    state: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Tuple[str, str, VerifierAttemptRecord | None]:
    """Backward-compatible wrapper: source-only facts no longer prove claims."""
    facts = extract_source_facts_for_candidate(state, candidate)
    if facts:
        return "", "; ".join(f.summary for f in facts[:3]), None
    return "", "", None
