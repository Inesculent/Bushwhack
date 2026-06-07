"""Shared source-scope helpers for review-check pipeline phases."""

from __future__ import annotations

import re
from typing import Mapping, Sequence

from src.domain.schemas import ReviewCheck, ReviewTask
from src.domain.state import GraphState
from src.orchestration.context.surface_ledger import (
    changed_file_sources_from_state,
    changed_files_from_diff,
)

_CODE_FILE_EXTENSIONS = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".cpp",
    ".cc",
    ".cxx",
    ".c",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".scala",
}

EXTERNAL_EVIDENCE_MARKERS = (
    "caller",
    "call site",
    "entrypoint",
    "entry point",
    "upstream",
    "downstream",
    "contract",
    "framework",
    "repository convention",
    "repo convention",
    "project convention",
    "public api",
    "integration",
    "permission",
    "authorization",
)


def looks_like_code_file(path: str) -> bool:
    lower = path.strip().lower()
    return any(lower.endswith(ext) for ext in _CODE_FILE_EXTENSIONS)


def changed_task_files(state: GraphState, task: ReviewTask) -> list[str]:
    changed_files = {
        path
        for paths in changed_file_sources_from_state(state).values()
        for path in paths
    } or set(changed_files_from_diff(str(state.get("git_diff") or "")))
    targets = [path.strip().replace("\\", "/") for path in task.target_files if path and path.strip()]
    if not targets:
        targets = sorted(changed_files)
    if changed_files:
        targets = [path for path in targets if path in changed_files]
    return [path for path in dict.fromkeys(targets) if looks_like_code_file(path)]


def task_evidence_text(slot: Mapping[str, object]) -> str:
    parts: list[str] = [
        str(slot.get("direct_context") or ""),
        str(slot.get("review_kb_excerpt") or ""),
        str(slot.get("mental_model_excerpt") or ""),
    ]
    te = slot.get("task_evidence") if isinstance(slot.get("task_evidence"), dict) else {}
    if isinstance(te, dict):
        rendered = str(te.get("rendered") or "")
        if rendered:
            parts.append(rendered)
        files = te.get("file_contents") if isinstance(te.get("file_contents"), dict) else {}
        if isinstance(files, dict):
            parts.extend(str(body or "") for body in files.values())
    return "\n\n".join(part for part in parts if part.strip())


def meaningful_tokens(text: str) -> set[str]:
    stop = {
        "the",
        "and",
        "that",
        "this",
        "with",
        "from",
        "code",
        "evidence",
        "changed",
        "behavior",
        "repository",
    }
    return {
        tok
        for tok in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower())
        if tok not in stop
    }


def tokens_overlap(left: str, right: str) -> bool:
    left_tokens = meaningful_tokens(left)
    right_tokens = meaningful_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    return bool(left_tokens & right_tokens)


def evidence_covers_requirement(requirement: str, evidence_blob: str) -> bool:
    tokens = meaningful_tokens(requirement)
    if not tokens:
        return False
    blob = evidence_blob.lower()
    hits = sum(1 for token in tokens if token in blob)
    return hits >= max(1, min(2, len(tokens)))


def requires_external_evidence(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in EXTERNAL_EVIDENCE_MARKERS)


def evidence_requirements_for_check(check: ReviewCheck) -> list[str]:
    items: list[str] = []
    items.extend(str(item).strip() for item in check.required_evidence if str(item).strip())
    for item in list(check.suppress_criteria) + list(check.report_criteria):
        text = str(item).strip()
        if text and requires_external_evidence(text):
            items.append(text)
    if requires_external_evidence(check.affected_invariant):
        items.append(check.affected_invariant)
    return list(dict.fromkeys(items))


def coverage_meta_relevance(meta: Mapping[str, object] | None) -> int:
    if not meta:
        return 0
    try:
        return int(meta.get("relevance_score") or 0)
    except (TypeError, ValueError):
        return 0


def compiled_check_is_source_local(
    check: ReviewCheck,
    meta: Mapping[str, object] | None,
    evidence_blob: str | None,
    task_files: set[str],
    evidence_requirements: Sequence[str],
) -> bool:
    path = check.file_path.strip().replace("\\", "/")
    if meta:
        kind = str(meta.get("_floor_kind") or meta.get("origin_kind") or "")
        meta_path = str(meta.get("file_path") or path).strip().replace("\\", "/")
        meta_blob = " ".join(
            str(meta.get(key) or "")
            for key in ("surface", "dimension", "evidence")
        )
        if kind == "coverage_obligation":
            if bool(meta.get("files_complete")):
                return True
            if meta_path in task_files and not requires_external_evidence(meta_blob):
                return True

    if any(requires_external_evidence(requirement) for requirement in evidence_requirements):
        return False
    if path in task_files:
        return True
    if evidence_blob is None:
        return bool(check.surface_ids)
    return bool(evidence_requirements) and all(
        evidence_covers_requirement(requirement, evidence_blob)
        for requirement in evidence_requirements
    )
