"""Repository-agnostic review obligation tracking.

These helpers do not predict project-specific bugs. They derive abstract review
obligations from code shape, then record whether the critiquer addressed them
with a candidate, an audit note, or a context gap.
"""

from __future__ import annotations

import re
from typing import Any, Collection, Mapping, Sequence

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
    "api/signature compatibility",
    "dependency/import availability",
    "nullability/panic safety",
    "state/cache lifecycle",
    "protocol/output fidelity",
    "concurrency/shared-state safety",
    "security/input boundary",
    "repository convention contract",
    "public/user contract",
    "maintainability contract",
)

_CLASS_OR_DEF_RE = re.compile(r"^\s*(class|def)\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_CAMEL_SURFACE_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]{2,}\b")
_API_INTENT_RE = re.compile(
    r"\b(api|signature|call-?site|caller|type|contract|integration contract|framework|"
    r"public method|interface|include|import)\b",
    re.IGNORECASE,
)
_API_EVIDENCE_RE = re.compile(
    r"(\b(class|def|func|function|public|private|protected|interface|struct|enum)\b|"
    r"#include\b|\bimport\b|\bfrom\s+\S+\s+import\b|\brequire\s*\(|->|::|=>|:\s*[A-Z]\w+)",
    re.IGNORECASE,
)
_DEPENDENCY_INTENT_RE = re.compile(
    r"\b(import|include|dependency|module|package|symbol|undefined|not defined)\b",
    re.IGNORECASE,
)
_DEPENDENCY_EVIDENCE_RE = re.compile(
    r"^\s*(#include|import|from\s+\S+\s+import|using\s+|require\s*\()",
    re.IGNORECASE | re.MULTILINE,
)
_NULL_INTENT_RE = re.compile(
    r"\b(null|none|nil|panic|crash|empty|missing|optional|guard|safety|correctness)\b",
    re.IGNORECASE,
)
_NULL_EVIDENCE_RE = re.compile(
    r"\b(None|nullptr|null|nil|panic|unwrap\s*\(|expect\s*\(|Optional|Option<|"
    r"undefined|TypeError|NullPointerException|is None|==\s*nil|!=\s*nil)\b",
    re.IGNORECASE,
)
_STATE_INTENT_RE = re.compile(
    r"\b(state|cache|lifecycle|invalidate|reset|cleanup|resource|performance|concurrency)\b",
    re.IGNORECASE,
)
_STATE_EVIDENCE_RE = re.compile(
    r"\b(cache|cached|state|reset|cleanup|init|initialize|invalidate|persist|mutable|global)\b",
    re.IGNORECASE,
)
_PROTOCOL_INTENT_RE = re.compile(
    r"\b(protocol|output|format|serialize|serialization|cli|api response|message|user-visible|docs?|tooltip)\b",
    re.IGNORECASE,
)
_PROTOCOL_EVIDENCE_RE = re.compile(
    r"\b(json|serialize|deserialize|format|fprintf|println|print|response|status|header|"
    r"message|tooltip|error text|stdout|stderr|cli)\b",
    re.IGNORECASE,
)
_CONCURRENCY_INTENT_RE = re.compile(
    r"\b(concurrency|concurrent|thread|async|await|lock|race|parallel|shared-state|shared state)\b",
    re.IGNORECASE,
)
_CONCURRENCY_EVIDENCE_RE = re.compile(
    r"\b(thread|async|await|lock|mutex|atomic|race|goroutine|synchronized|concurrent)\b",
    re.IGNORECASE,
)
_SECURITY_INTENT_RE = re.compile(
    r"\b(security|auth|authorization|permission|escape|sanitize|validation|input|"
    r"path|file|network|deseriali[sz]ation|injection|secret)\b",
    re.IGNORECASE,
)
_SECURITY_EVIDENCE_RE = re.compile(
    r"\b(auth|permission|escape|sanitize|validate|user\s*input|request|path|open\s*\(|"
    r"readfile|writefile|network|deserialize|pickle|eval|exec|secret|token|sql)\b",
    re.IGNORECASE,
)
_CONVENTION_INTENT_RE = re.compile(
    r"\b(convention|framework syntax|preferred|documented|repo policy|project-local|local api)\b",
    re.IGNORECASE,
)
_CONVENTION_EVIDENCE_RE = re.compile(
    r"\b(INPUT_TYPES|RETURN_TYPES|node_typing|convention|preferred|documented|framework)\b",
    re.IGNORECASE,
)
_PUBLIC_CONTRACT_INTENT_RE = re.compile(
    r"\b(user-visible|public|docs?|tooltip|message|cli|api|protocol|output)\b",
    re.IGNORECASE,
)
_PUBLIC_CONTRACT_EVIDENCE_RE = re.compile(
    r"\b(tooltip|docstring|readme|message|error|warning|print|response|status|public)\b",
    re.IGNORECASE,
)
_MAINTAINABILITY_INTENT_RE = re.compile(
    r"\b(maintainability|unused|dead code|duplicate|duplication|integration|repo policy)\b",
    re.IGNORECASE,
)
_MAINTAINABILITY_EVIDENCE_RE = re.compile(
    r"\b(unused|dead code|todo|duplicate|duplicated|copy-paste|deprecated)\b",
    re.IGNORECASE,
)
_TASK_CONDITIONED_OBLIGATION_SPECS: tuple[
    tuple[str, str, re.Pattern[str], re.Pattern[str], frozenset[str]],
    ...,
] = (
    (
        "api/signature compatibility",
        "task targets API, signature, caller, type, or integration compatibility",
        _API_INTENT_RE,
        _API_EVIDENCE_RE,
        frozenset(),
    ),
    (
        "dependency/import availability",
        "task targets dependency/import availability and code declares imports/includes",
        _DEPENDENCY_INTENT_RE,
        _DEPENDENCY_EVIDENCE_RE,
        frozenset(),
    ),
    (
        "nullability/panic safety",
        "task targets crash/null safety and code contains nullable or panic-prone paths",
        _NULL_INTENT_RE,
        _NULL_EVIDENCE_RE,
        frozenset(),
    ),
    (
        "state/cache lifecycle",
        "task targets state, cache, lifecycle, or resource behavior",
        _STATE_INTENT_RE,
        _STATE_EVIDENCE_RE,
        frozenset(),
    ),
    (
        "protocol/output fidelity",
        "task targets protocol or output behavior and code formats emitted data",
        _PROTOCOL_INTENT_RE,
        _PROTOCOL_EVIDENCE_RE,
        frozenset(),
    ),
    (
        "concurrency/shared-state safety",
        "task targets concurrent or shared-state behavior",
        _CONCURRENCY_INTENT_RE,
        _CONCURRENCY_EVIDENCE_RE,
        frozenset(),
    ),
    (
        "security/input boundary",
        "task targets security or input boundary behavior",
        _SECURITY_INTENT_RE,
        _SECURITY_EVIDENCE_RE,
        frozenset({"security"}),
    ),
    (
        "repository convention contract",
        "task targets an explicit repository or framework convention",
        _CONVENTION_INTENT_RE,
        _CONVENTION_EVIDENCE_RE,
        frozenset(),
    ),
    (
        "public/user contract",
        "task targets user-visible or public contract text/output",
        _PUBLIC_CONTRACT_INTENT_RE,
        _PUBLIC_CONTRACT_EVIDENCE_RE,
        frozenset(),
    ),
    (
        "maintainability contract",
        "task targets maintainability only where code shows an explicit contract risk",
        _MAINTAINABILITY_INTENT_RE,
        _MAINTAINABILITY_EVIDENCE_RE,
        frozenset(),
    ),
)


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


def _task_text(task: ReviewTask) -> str:
    return f"{task.title} {task.description}".lower()


def _task_has_intent(
    task: ReviewTask,
    pattern: re.Pattern[str],
    *,
    specialties: Collection[str] | None = None,
) -> bool:
    if specialties and task.specialty in specialties:
        return True
    return bool(pattern.search(_task_text(task)))


def _add_task_conditioned_obligations(
    obligations: list[dict[str, Any]],
    *,
    task: ReviewTask,
    file_path: str,
    surface: str,
    files_complete: Mapping[str, bool],
    body: str,
) -> None:
    for dimension, evidence, intent, code, specialties in _TASK_CONDITIONED_OBLIGATION_SPECS:
        if not _task_has_intent(task, intent, specialties=specialties):
            continue
        if not code.search(body):
            continue
        _add_obligation(
            obligations,
            task_id=task.id,
            file_path=file_path,
            surface=surface,
            dimension=dimension,
            evidence=evidence,
            files_complete=files_complete,
        )


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
        _add_task_conditioned_obligations(
            obligations,
            task=task,
            file_path=file_path,
            surface=surface,
            files_complete=files_complete,
            body=body,
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
        dims.add("api/signature compatibility")
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
    if any(marker in blob for marker in ("signature", "call site", "call-site", "type mismatch", "api", "interface")):
        dims.add("api/signature compatibility")
    if any(marker in blob for marker in ("import", "include", "undefined", "not defined", "missing dependency")):
        dims.add("dependency/import availability")
    if normalized.behavioral_symptom == "crash" or any(
        marker in blob for marker in ("null", "none", "nil", "panic", "nil pointer", "null pointer")
    ):
        dims.add("nullability/panic safety")
    if any(marker in blob for marker in ("cache", "state", "lifecycle", "invalidate", "reset")):
        dims.add("state/cache lifecycle")
    if any(marker in blob for marker in ("protocol", "output", "format", "status", "header", "message")):
        dims.add("protocol/output fidelity")
    if any(marker in blob for marker in ("thread", "lock", "mutex", "race", "async", "concurrent")):
        dims.add("concurrency/shared-state safety")
    if normalized.claim_type == "security_risk" or any(
        marker in blob for marker in ("auth", "permission", "sanitize", "escape", "injection", "path traversal")
    ):
        dims.add("security/input boundary")
    if any(marker in blob for marker in ("convention", "framework syntax", "preferred syntax", "repo policy")):
        dims.add("repository convention contract")
    if any(marker in blob for marker in ("user-visible", "tooltip", "public contract", "cli output", "docs")):
        dims.add("public/user contract")
    if any(marker in blob for marker in ("unused", "dead code", "duplicate", "duplicated")):
        dims.add("maintainability contract")
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
