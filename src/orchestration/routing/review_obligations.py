"""Repository-agnostic review obligation tracking.

These helpers do not predict project-specific bugs. They derive abstract review
obligations from code shape, then record whether the critiquer addressed them
with a candidate, an audit note, or a context gap.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from src.domain.schemas import AuditCoverageRecord, CandidateFinding, ReviewFinding, ReviewTask
from src.orchestration.routing.finding_dedupe import candidate_with_behavioral_metadata

COVERAGE_DIMENSIONS: tuple[str, ...] = (
    "contract completeness",
    "branch exhaustiveness",
    "boundary/index handling",
    "structured data preservation",
    "aggregation/serialization safety",
    "exception/control-flow scope",
    "resource-amplification risk",
)

_CLASS_OR_DEF_RE = re.compile(r"^\s*(class|def)\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_CAMEL_SURFACE_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]{2,}\b")


def _norm_path(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("/")


def _task_target_files(task: ReviewTask) -> set[str]:
    return {_norm_path(path) for path in task.target_files if path}


def _path_in_task_scope(file_path: str, target_files: set[str]) -> bool:
    if not target_files:
        return True
    norm = _norm_path(file_path)
    return norm in target_files or any(norm.endswith("/" + target) for target in target_files)


def _surface_name(file_path: str, body: str) -> str:
    names: list[str] = []
    for line in body.splitlines():
        match = _CLASS_OR_DEF_RE.match(line)
        if match:
            if match.group(1) == "class":
                return match.group(2)
            names.append(match.group(2))
        if len(names) >= 3:
            break
    if names:
        return ", ".join(names)
    return _norm_path(file_path)


def _task_surface_names(task: ReviewTask) -> set[str]:
    text = f"{task.title} {task.description}"
    ignored = {
        "Audit",
        "Review",
        "Structured",
        "Regex",
        "String",
        "Code",
        "Handler",
        "Task",
    }
    return {name for name in _CAMEL_SURFACE_RE.findall(text) if name not in ignored}


def _class_blocks(body: str) -> dict[str, str]:
    lines = body.splitlines()
    starts: list[tuple[str, int, int]] = []
    for index, raw in enumerate(lines):
        match = re.match(r"^(\s*)class\s+([A-Za-z_][A-Za-z0-9_]*)\b", raw)
        if match:
            starts.append((match.group(2), index, len(match.group(1))))
    blocks: dict[str, str] = {}
    for pos, (name, start, indent) in enumerate(starts):
        end = len(lines)
        for _, next_start, next_indent in starts[pos + 1 :]:
            if next_indent <= indent:
                end = next_start
                break
        blocks[name] = "\n".join(lines[start:end])
    return blocks


def _body_for_task_surface(task: ReviewTask, body: str) -> str:
    names = _task_surface_names(task)
    if not names:
        return body
    blocks = _class_blocks(body)
    selected = [blocks[name] for name in sorted(names) if name in blocks]
    if selected:
        return "\n\n".join(selected)
    return body


def _add_obligation(
    out: list[dict[str, Any]],
    *,
    task_id: str,
    file_path: str,
    surface: str,
    dimension: str,
    evidence: str,
    files_complete: Mapping[str, bool],
) -> None:
    index = len(out) + 1
    norm_file = _norm_path(file_path)
    out.append(
        {
            "obligation_id": f"{task_id}:{index:03d}",
            "task_id": task_id,
            "file_path": norm_file,
            "surface": surface,
            "dimension": dimension,
            "status": "pending",
            "evidence": evidence[:240],
            "files_complete": bool(files_complete.get(norm_file, files_complete.get(file_path, False))),
        }
    )


def derive_review_obligations(
    task: ReviewTask,
    task_evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Derive abstract review obligations from task-scoped code evidence."""
    files = task_evidence.get("file_contents")
    if not isinstance(files, Mapping):
        files = {}
    files_complete = task_evidence.get("files_complete")
    if not isinstance(files_complete, Mapping):
        files_complete = {}

    obligations: list[dict[str, Any]] = []
    target_files = _task_target_files(task)
    for raw_path, raw_body in files.items():
        file_path = _norm_path(str(raw_path))
        if not _path_in_task_scope(file_path, target_files):
            continue
        body = _body_for_task_surface(task, str(raw_body or ""))
        if not body.strip():
            continue
        surface = _surface_name(file_path, body)
        blob = body.lower()

        if "return_types" in blob or re.search(r"\bdef\s+\w+\([^)]*\):", body):
            _add_obligation(
                obligations,
                task_id=task.id,
                file_path=file_path,
                surface=surface,
                dimension="contract completeness",
                evidence="entry point declares or implies a return contract",
                files_complete=files_complete,
            )
        if "elif " in blob or re.search(r"\b(if|elif)\s+\w+\s*==", blob):
            _add_obligation(
                obligations,
                task_id=task.id,
                file_path=file_path,
                surface=surface,
                dimension="branch exhaustiveness",
                evidence="conditional/discriminant branch chain present",
                files_complete=files_complete,
            )
        if re.search(r"\[[0-9]+\]", body) or "index" in blob or "len(" in blob:
            _add_obligation(
                obligations,
                task_id=task.id,
                file_path=file_path,
                surface=surface,
                dimension="boundary/index handling",
                evidence="indexing, length checks, or explicit index parameter present",
                files_complete=files_complete,
            )
        if any(marker in blob for marker in ("tuple", "row", "record", "findall", "groups()", "structured")):
            _add_obligation(
                obligations,
                task_id=task.id,
                file_path=file_path,
                surface=surface,
                dimension="structured data preservation",
                evidence="structured or multi-slot result handling present",
                files_complete=files_complete,
            )
        if any(marker in blob for marker in (".join(", "json.", "serialize", "format(")):
            _add_obligation(
                obligations,
                task_id=task.id,
                file_path=file_path,
                surface=surface,
                dimension="aggregation/serialization safety",
                evidence="aggregation, formatting, serialization, or join path present",
                files_complete=files_complete,
            )
        if "try:" in blob or "except " in blob:
            _add_obligation(
                obligations,
                task_id=task.id,
                file_path=file_path,
                surface=surface,
                dimension="exception/control-flow scope",
                evidence="try/except or exception scope present",
                files_complete=files_complete,
            )
        if any(marker in blob for marker in ("regex", "re.", "while ", "for ")) and any(
            marker in blob for marker in ("user", "pattern", "external", "unbounded", "loop")
        ):
            _add_obligation(
                obligations,
                task_id=task.id,
                file_path=file_path,
                surface=surface,
                dimension="resource-amplification risk",
                evidence="potentially amplifying operation with variable input present",
                files_complete=files_complete,
            )

    return obligations


def _candidate_dimensions(candidate: CandidateFinding) -> set[str]:
    normalized = candidate_with_behavioral_metadata(candidate)
    dims: set[str] = set()
    blob = " ".join(
        [
            normalized.content,
            normalized.failure_mode,
            normalized.evidence_summary,
            normalized.recommendation or "",
        ]
    ).lower()
    if normalized.behavioral_symptom in {"missing_return", "contract_mismatch"}:
        dims.add("contract completeness")
    if normalized.root_operation == "dispatch" or "branch" in blob or "fall-through" in blob:
        dims.add("branch exhaustiveness")
    if normalized.root_operation == "indexing":
        dims.add("boundary/index handling")
    if normalized.behavioral_symptom == "data_loss" or any(
        marker in blob for marker in ("structured", "tuple", "row", "slot", "first element")
    ):
        dims.add("structured data preservation")
    if normalized.root_operation in {"aggregation", "serialization"} or any(
        marker in blob for marker in ("join", "serialize", "format")
    ):
        dims.add("aggregation/serialization safety")
    if normalized.root_operation == "exception_scope" or "uncaught" in blob:
        dims.add("exception/control-flow scope")
    if normalized.root_operation == "resource_use" or normalized.behavioral_symptom == "unbounded_work":
        dims.add("resource-amplification risk")
    return dims


def _audit_dimensions(rows: Sequence[AuditCoverageRecord | Mapping[str, Any]]) -> set[str]:
    dims: set[str] = set()
    for raw in rows:
        if isinstance(raw, AuditCoverageRecord):
            values = raw.dimensions
        elif isinstance(raw, Mapping):
            values = raw.get("dimensions") or []
        else:
            values = []
        for value in values:
            text = str(value).strip().lower()
            if text:
                dims.add(text)
    return dims


def evaluate_review_obligations(
    obligations: Sequence[Mapping[str, Any]],
    candidates: Sequence[CandidateFinding],
    audit_coverage: Sequence[AuditCoverageRecord | Mapping[str, Any]],
) -> dict[str, Any]:
    """Mark obligation statuses after a critiquer pass."""
    candidate_dims_by_file: dict[str, set[str]] = {}
    candidate_ids_by_file_dim: dict[tuple[str, str], list[str]] = {}
    for candidate in candidates:
        norm_file = _norm_path(candidate.file_path)
        for dim in _candidate_dimensions(candidate):
            candidate_dims_by_file.setdefault(norm_file, set()).add(dim)
            candidate_ids_by_file_dim.setdefault((norm_file, dim), []).append(candidate.candidate_id)

    audit_dims = _audit_dimensions(audit_coverage)
    rows: list[dict[str, Any]] = []
    counts = {
        "candidate": 0,
        "cleared_with_evidence": 0,
        "needs_context": 0,
        "unchecked": 0,
    }
    for raw in obligations:
        row = dict(raw)
        dim = str(row.get("dimension") or "").strip().lower()
        norm_file = _norm_path(str(row.get("file_path") or ""))
        file_dims = candidate_dims_by_file.get(norm_file, set())
        if dim in file_dims:
            row["status"] = "candidate"
            row["candidate_ids"] = sorted(set(candidate_ids_by_file_dim.get((norm_file, dim), [])))
        elif dim in audit_dims:
            row["status"] = "cleared_with_evidence"
        elif not row.get("files_complete"):
            row["status"] = "needs_context"
        else:
            row["status"] = "unchecked"
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        rows.append(row)

    warnings = [
        f"coverage_obligation_unchecked:{row.get('obligation_id')}:{row.get('dimension')}"
        for row in rows
        if row.get("status") == "unchecked"
    ]
    return {
        "obligations": rows,
        "counts": counts,
        "warnings": warnings,
    }


def recall_audit_for_final_findings(
    *,
    obligations_by_task: Mapping[str, Any],
    candidates: Sequence[CandidateFinding],
    final_findings: Sequence[ReviewFinding],
    duplicate_map: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Build a compact final recall audit matrix."""
    final_ids = {finding.id for finding in final_findings}
    duplicate_equivalents: dict[str, str] = {}
    for keeper, dropped in (duplicate_map or {}).items():
        for item in dropped:
            if str(item) != str(keeper):
                duplicate_equivalents[str(item)] = str(keeper)

    candidate_ids: set[str] = set()
    for candidate in candidates:
        if isinstance(candidate, CandidateFinding):
            candidate_ids.add(candidate.candidate_id)
        elif isinstance(candidate, Mapping):
            cid = str(candidate.get("candidate_id") or "")
            if cid:
                candidate_ids.add(cid)
    orphaned_duplicates = sorted(
        cid
        for cid, keeper in duplicate_equivalents.items()
        if cid in candidate_ids and keeper not in final_ids
    )
    by_task: dict[str, Any] = {}
    for task_id, block in obligations_by_task.items():
        rows = block.get("obligations") if isinstance(block, Mapping) else None
        if not isinstance(rows, list):
            continue
        by_task[str(task_id)] = {
            "unchecked": [
                row for row in rows if isinstance(row, Mapping) and row.get("status") == "unchecked"
            ],
            "needs_context": [
                row for row in rows if isinstance(row, Mapping) and row.get("status") == "needs_context"
            ],
        }

    return {
        "candidate_count": len(candidate_ids),
        "final_finding_count": len(final_ids),
        "duplicate_equivalents": duplicate_equivalents,
        "orphaned_duplicate_candidate_ids": orphaned_duplicates,
        "obligations_by_task": by_task,
    }
