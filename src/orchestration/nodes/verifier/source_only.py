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

    return "", "", None
