"""Cheap verifier checks that do not import repository packages."""

from __future__ import annotations

import ast
import re
from typing import Any, Dict, Iterable, Tuple

from src.domain.verifier_schemas import VerificationStatus, VerifierAttemptRecord
from src.orchestration.context.task_evidence import task_evidence_slot_from_state


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("/")


def _target_source_from_state(state: Dict[str, Any], candidate: Dict[str, Any]) -> str:
    tid = str(candidate.get("patch_task_id") or "")
    fp = _norm(str(candidate.get("file_path") or ""))
    if not tid or not fp:
        return ""
    slot = task_evidence_slot_from_state(state, tid)
    files = slot.get("file_contents") if isinstance(slot.get("file_contents"), dict) else {}
    for raw_path, body in files.items():
        path = _norm(str(raw_path))
        if path == fp or path.endswith("/" + fp) or fp.endswith("/" + path):
            return str(body or "")
    return ""


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


def _attempt(reason: str, *, failure_class: str = "wrong_output") -> VerifierAttemptRecord:
    return VerifierAttemptRecord(
        attempt_number=0,
        test_code="# source-only verifier",
        exit_code=1,
        stdout=f"STATUS: MISMATCH | source-only static proof: {reason}",
        stderr="",
        status=VerificationStatus.COMPLETED,
        sandbox_mode="source_only_static",
        failure_class=failure_class,
    )


_MISSING_RETURN_MARKERS = (
    "missing return",
    "missing a return",
    "no return",
    "does not return",
    "doesn't return",
    "implicit none",
    "falls through",
    "fall through",
)

_STRUCTURED_EXTRACTION_MARKERS = (
    "regex",
    "extract",
    "all matches",
    "all groups",
    "first match",
    "first group",
    "data loss",
    "structured",
)


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
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start, end = _node_span(node)
        if line_start and start <= line_start <= end:
            funcs.append(node)
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(node.name.lower())}(?![A-Za-z0-9_])", blob):
            funcs.append(node)
    return funcs


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


def _source_only_missing_return(
    source: str,
    tree: ast.AST,
    candidate: Dict[str, Any],
) -> tuple[str, VerifierAttemptRecord | None]:
    blob = _candidate_blob(candidate)
    if not any(marker in blob for marker in _MISSING_RETURN_MARKERS):
        return "", None
    for func in _target_functions(tree, candidate):
        if not _block_guarantees_exit(func.body):
            reason = f"{func.name} has a reachable path that can fall through without an explicit return"
            return reason, _attempt(reason, failure_class="missing_return")
    return "", None


def _source_only_structured_extraction(
    source: str,
    tree: ast.AST,
    candidate: Dict[str, Any],
) -> tuple[str, VerifierAttemptRecord | None]:
    blob = _candidate_blob(candidate)
    if not ("regex" in blob and any(marker in blob for marker in _STRUCTURED_EXTRACTION_MARKERS)):
        return "", None
    for func in _target_functions(tree, candidate):
        text = _source_segment(source, func).lower()
        all_matches_claim = "all matches" in blob or "findall" in blob or "finditer" in blob
        all_groups_claim = "all groups" in blob or "group tuple" in blob or "groups" in blob
        tuple_first_slot_claim = any(
            marker in blob
            for marker in ("m[0]", "first element", "first group", "only the first")
        )
        group_index_claim = "group_index" in blob or "group index" in blob
        if all_matches_claim and "re.search" in text and "re.findall" not in text and "re.finditer" not in text:
            reason = f"{func.name} uses re.search for an all-matches regex extraction claim"
            return reason, _attempt(reason)
        if (
            all_matches_claim
            and "re.findall" in text
            and "[m[0] for m in matches]" in text
            and (tuple_first_slot_claim or group_index_claim)
        ):
            reason = f"{func.name} keeps only tuple element 0 for an all-matches regex extraction claim"
            return reason, _attempt(reason)
        if all_groups_claim and ".group(1)" in text and ".groups(" not in text:
            reason = f"{func.name} extracts only group(1) for an all-groups regex extraction claim"
            return reason, _attempt(reason)
        if (
            all_groups_claim
            and ".group(" in text
            and ".append(" in text
            and ".join(" in text
            and "is not none" not in text
            and " or \"\"" not in text
        ):
            reason = f"{func.name} joins regex group results without filtering possible None values"
            return reason, _attempt(reason)
    return "", None


def source_only_verify_candidate(
    state: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Tuple[str, str, VerifierAttemptRecord | None]:
    """Return (verdict, rationale, attempt) when static source evidence proves a claim."""
    source = _target_source_from_state(state, candidate)
    if not source.strip():
        return "", "", None
    fp = _norm(str(candidate.get("file_path") or ""))
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        reason = f"{fp} has SyntaxError during source-only parse: {exc.msg}"
        return "verified", reason, _attempt(reason, failure_class="syntax_error")

    removed = _removed_import_names(str(state.get("git_diff") or ""), fp)
    if removed:
        defined = _defined_names(tree)
        still_used = sorted((removed & _used_names(tree)) - defined)
        if still_used:
            reason = (
                f"{fp} removed import(s) still used by name: {', '.join(still_used[:8])}"
            )
            return "verified", reason, _attempt(reason)

    reason, attempt = _source_only_missing_return(source, tree, candidate)
    if attempt is not None:
        return "verified", reason, attempt

    reason, attempt = _source_only_structured_extraction(source, tree, candidate)
    if attempt is not None:
        return "verified", reason, attempt

    return "", "", None
