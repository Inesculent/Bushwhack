"""Check-first review nodes for the adversarial reviewer ablation."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Mapping

from src.config import Settings, get_settings
from src.domain.schemas import (
    CandidateFinding,
    FocusedContextRequest,
    InvalidReviewCheck,
    ReviewCheck,
    ReviewCheckCompilerOutput,
    ReviewCheckExecutorOutput,
    ReviewCheckResult,
    ReviewTask,
)
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import trace_from_exception, trace_llm_call
from src.orchestration.context.focus_request_scope import (
    allowed_review_paths,
    clamp_focused_context_request,
)
from src.orchestration.context.context_packets import focused_snippets_for_candidate
from src.orchestration.nodes.application.critiquer import _normalize_candidates
from src.orchestration.prompts.renderer import render_reviewer_prompt

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

REVIEW_CHECK_LENSES = (
    "permission_boundary",
    "api_compatibility",
    "state_transition",
    "input_validation",
    "error_propagation",
    "resource_lifecycle",
    "data_shape_consistency",
    "concurrency_ordering",
    "test_oracle_strength",
)

_VAGUE_QUESTIONS = (
    "look for security bugs",
    "look for edge cases",
    "review error handling",
    "find bugs",
    "check this code",
)
_FOCUS_MARKERS = (
    "caller",
    "call site",
    "entrypoint",
    "upstream",
    "downstream",
    "authorization",
    "permission",
    "contract",
    "repository evidence",
    "other guard",
    "config",
    "middleware",
)
_SPECULATIVE_MARKERS = (
    "hypothetical",
    "speculative",
    "no evidence",
    "not shown",
    "could be",
    "might be",
    "maybe",
)
_TERMINAL_CHECK_DECISIONS = {"candidate", "no_finding", "suppressed", "budget_exhausted"}
_AFFECTED_PATH_MARKERS = (
    "caller",
    "call path",
    "entrypoint",
    "entry point",
    "route",
    "request",
    "flow",
    "operation",
    "reachable",
    "when ",
    "if ",
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
_GENERIC_QUERY_TOKENS = {"changed", "code", "behavior", "repository", "evidence", "context"}


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _task_from_state(state: GraphState) -> ReviewTask | None:
    task_id = state.get("current_task_id")
    registry = state.get("task_registry", {}) or {}
    if not task_id or task_id not in registry:
        return None
    return registry[task_id]


def _pipeline_slot(state: GraphState, task_id: str) -> Dict[str, Any]:
    meta = state.get("metadata", {}) or {}
    pipe = meta.get("critique_pipeline", {}) if isinstance(meta, dict) else {}
    by_task = pipe.get("by_task", {}) if isinstance(pipe, dict) else {}
    slot = by_task.get(task_id, {}) if isinstance(by_task, dict) else {}
    return dict(slot) if isinstance(slot, dict) else {}


def _review_checks_meta(state: GraphState) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    metadata = dict(state.get("metadata", {}) or {})
    block = dict(metadata.get("review_checks", {}) or {})
    by_task = dict(block.get("by_task", {}) or {})
    return metadata, block, by_task


def _set_task_review_checks_meta(
    state: GraphState,
    task_id: str,
    patch: Mapping[str, Any],
) -> Dict[str, Any]:
    metadata, block, by_task = _review_checks_meta(state)
    slot = dict(by_task.get(task_id, {}) or {})
    slot.update(dict(patch))
    by_task[task_id] = slot
    block["by_task"] = by_task
    metadata["review_checks"] = block
    return metadata


def _task_evidence_text(slot: Mapping[str, Any]) -> str:
    parts: List[str] = [
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


def _json_for_prompt(value: Any, *, max_chars: int = 8000) -> str:
    try:
        text = json.dumps(value, indent=2, ensure_ascii=False)
    except TypeError:
        text = json.dumps(str(value), ensure_ascii=False)
    if len(text) > max_chars:
        return text[: max_chars - 24].rstrip() + "\n... [truncated]"
    return text


def _dimension_to_lens(dimension: str) -> str:
    dim = dimension.lower()
    if "security" in dim or "permission" in dim:
        return "permission_boundary"
    if "api" in dim or "signature" in dim or "contract" in dim:
        return "api_compatibility"
    if "state" in dim or "cache" in dim:
        return "state_transition"
    if "null" in dim or "input" in dim or "validation" in dim:
        return "input_validation"
    if "exception" in dim or "error" in dim:
        return "error_propagation"
    if "resource" in dim or "lifecycle" in dim:
        return "resource_lifecycle"
    if "structured" in dim or "index" in dim or "aggregation" in dim or "serialization" in dim:
        return "data_shape_consistency"
    if "concurrency" in dim or "ordering" in dim:
        return "concurrency_ordering"
    if "test" in dim:
        return "test_oracle_strength"
    return "other"


def _fallback_checks(task: ReviewTask, slot: Mapping[str, Any]) -> List[ReviewCheck]:
    obligations = slot.get("coverage_obligations") if isinstance(slot.get("coverage_obligations"), list) else []
    checks: List[ReviewCheck] = []
    for index, raw in enumerate(obligations[:6], start=1):
        row = raw if isinstance(raw, Mapping) else {}
        file_path = str(row.get("file_path") or (task.target_files[0] if task.target_files else ""))
        dimension = str(row.get("dimension") or "task contract")
        surface = str(row.get("surface") or file_path or "changed code")
        checks.append(
            ReviewCheck(
                check_id=f"{task.id}:check:{index}",
                patch_task_id=task.id,
                lens=_dimension_to_lens(dimension),  # type: ignore[arg-type]
                file_path=file_path,
                line_start=1,
                line_end=1,
                changed_code_anchor=surface,
                behavioral_question=f"Does the changed {surface} preserve {dimension}?",
                affected_invariant=dimension,
                required_evidence=[
                    str(row.get("evidence") or f"code evidence for {dimension}"),
                    "changed-code behavior at the anchor",
                ],
                suppress_criteria=[f"Repository evidence shows {dimension} is preserved."],
                report_criteria=[f"The changed code violates {dimension} on a reachable path."],
                allowed_retrieval=["task_evidence", "focused_context"],
                budget=2,
            )
        )
    if checks:
        return checks
    file_path = task.target_files[0] if task.target_files else ""
    return [
        ReviewCheck(
            check_id=f"{task.id}:check:1",
            patch_task_id=task.id,
            lens="other",
            file_path=file_path,
            line_start=1,
            line_end=1,
            changed_code_anchor=file_path or task.title,
            behavioral_question=f"Does the changed code satisfy the task-specific behavior: {task.title}?",
            affected_invariant=task.description[:400],
            required_evidence=["changed-code behavior at the task anchor"],
            suppress_criteria=["Task evidence shows the changed behavior is preserved."],
            report_criteria=["Changed code creates a concrete reachable regression."],
            allowed_retrieval=["task_evidence"],
            budget=1,
        )
    ]


def _looks_like_code_file(path: str) -> bool:
    lower = path.strip().lower()
    return any(lower.endswith(ext) for ext in _CODE_FILE_EXTENSIONS)


def _changed_task_files(state: GraphState, task: ReviewTask) -> List[str]:
    changed_files = _changed_files_from_diff(str(state.get("git_diff") or ""))
    targets = [path.strip().replace("\\", "/") for path in task.target_files if path and path.strip()]
    if not targets:
        targets = sorted(changed_files)
    if changed_files:
        targets = [path for path in targets if path in changed_files]
    return [path for path in dict.fromkeys(targets) if _looks_like_code_file(path)]


def _coverage_check_for_file(task: ReviewTask, file_path: str, index: int) -> ReviewCheck:
    return ReviewCheck(
        check_id=f"{task.id}:coverage:{index}",
        patch_task_id=task.id,
        lens="other",
        file_path=file_path,
        line_start=1,
        line_end=1,
        changed_code_anchor=file_path,
        behavioral_question=(
            f"Does the changed code in {file_path} preserve the task-specific behavior for {task.title}?"
        ),
        affected_invariant=task.description[:400] or task.title,
        required_evidence=[
            f"changed behavior in {file_path}",
            "caller, contract, or runtime path needed to decide the changed behavior",
        ],
        suppress_criteria=[f"Repository evidence shows the changed behavior in {file_path} is preserved."],
        report_criteria=[f"The changed behavior in {file_path} creates a concrete reachable regression."],
        allowed_retrieval=["task_evidence", "focused_context"],
        budget=2,
    )


def _ensure_compiler_coverage_floor(
    *,
    state: GraphState,
    task: ReviewTask,
    checks: List[ReviewCheck],
) -> tuple[List[ReviewCheck], Dict[str, Any]]:
    coverage_files = _changed_task_files(state, task)
    checked_files = {check.file_path.strip().replace("\\", "/") for check in checks if check.file_path.strip()}
    missing_files = [path for path in coverage_files if path not in checked_files]
    if not missing_files:
        return checks, {
            "coverage_files": coverage_files,
            "missed_files": [],
            "added_coverage_checks": [],
            "warnings": [],
        }

    next_index = len(checks) + 1
    added = [
        _coverage_check_for_file(task, file_path, next_index + offset)
        for offset, file_path in enumerate(missing_files)
    ]
    warnings = [f"compiler_coverage_floor_added:{path}" for path in missing_files]
    return checks + added, {
        "coverage_files": coverage_files,
        "missed_files": missing_files,
        "added_coverage_checks": [check.check_id for check in added],
        "warnings": warnings,
    }


def _normalize_compiled_checks(task: ReviewTask, checks: Iterable[ReviewCheck]) -> List[ReviewCheck]:
    normalized: List[ReviewCheck] = []
    seen: set[str] = set()
    fallback_path = task.target_files[0] if task.target_files else ""
    for index, check in enumerate(checks, start=1):
        cid = check.check_id.strip() or f"{task.id}:check:{index}"
        if not cid.startswith(task.id):
            cid = f"{task.id}:{cid}"
        if cid in seen:
            cid = f"{cid}:{index}"
        seen.add(cid)
        path = check.file_path.strip().replace("\\", "/") or fallback_path
        line_start = max(1, check.line_start)
        line_end = max(line_start, check.line_end)
        normalized.append(
            check.model_copy(
                update={
                    "check_id": cid,
                    "patch_task_id": task.id,
                    "file_path": path,
                    "line_start": line_start,
                    "line_end": line_end,
                }
            )
        )
    return normalized


def _render_compiler_prompt(state: GraphState, task: ReviewTask, slot: Mapping[str, Any]) -> str:
    sections = {
        "Assigned Task": (
            f"Task ID: {task.id}\n"
            f"Title: {task.title}\n"
            f"Description: {task.description}\n"
            f"Specialty: {task.specialty}\n"
            f"Target files: {task.target_files}"
        ),
        "Repository Code Evidence": str(slot.get("direct_context") or "")[:16000],
        "Mental Model Excerpt": str(slot.get("mental_model_excerpt") or ""),
        "Review KB Context": str(slot.get("review_kb_excerpt") or ""),
        "Coverage Obligations": _json_for_prompt(slot.get("coverage_obligations") or [], max_chars=6000),
        "Available Lenses": ", ".join(REVIEW_CHECK_LENSES),
    }
    return render_reviewer_prompt("review_check_compiler.md", sections)


def make_review_check_compiler_node(
    model_key: str | None = None,
    use_llm: bool = True,
    settings: Settings | None = None,
):
    node_name = "review_check_compiler"

    def review_check_compiler_node(state: GraphState) -> Dict[str, Any]:
        task = _task_from_state(state)
        if task is None:
            return {"node_history": [f"{node_name}:skipped"]}

        resolved = settings or get_settings()
        slot = _pipeline_slot(state, task.id)
        warnings: List[str] = []
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []
        summary = ""
        checks: List[ReviewCheck] = []

        if use_llm:
            selected_model = model_key or resolved.reviewer_worker_model_key
            try:
                llm = Models.worker(
                    ReviewCheckCompilerOutput,
                    model_key=selected_model,
                    max_completion_tokens=resolved.reviewer_critiquer_max_completion_tokens,
                )
                prompt = _render_compiler_prompt(state, task, slot)
                traced = trace_llm_call(
                    llm,
                    prompt,
                    state=state,
                    node_name=node_name,
                    model_key=selected_model,
                    schema_name="ReviewCheckCompilerOutput",
                    input_summary={"task_id": task.id, "target_files": task.target_files},
                )
                response = parse_structured_output(traced.result, ReviewCheckCompilerOutput)
                llm_tokens = traced.tokens
                llm_trace = traced.trace_records
                checks = _normalize_compiled_checks(task, response.checks)
                summary = response.summary
                warnings.extend(response.warnings)
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}: {exc}")
                logger.warning("%s failed for task_id=%s: %s", node_name, task.id, exc)

        if not checks:
            checks = _fallback_checks(task, slot)
            if not summary:
                summary = "Deterministic fallback checks from task evidence obligations."
            warnings.append("review_check_compiler_fallback_used")

        checks, coverage_floor = _ensure_compiler_coverage_floor(
            state=state,
            task=task,
            checks=checks,
        )
        warnings.extend(coverage_floor.get("warnings", []))

        metadata = _set_task_review_checks_meta(
            state,
            task.id,
            {
                "compiler_summary": summary,
                "compiled_checks": [check.model_dump(mode="json") for check in checks],
                "compiled_count": len(checks),
                "compiler_coverage_floor": coverage_floor,
                "compiler_warnings": warnings,
            },
        )
        if _trace_enabled(state):
            trace_logger.info(
                "TRACE %s run_id=%s task_id=%s checks=%s",
                node_name,
                state.get("run_id", "unknown"),
                task.id,
                len(checks),
            )
        return {
            "metadata": metadata,
            "node_history": [node_name],
            "token_usage": llm_tokens,
            "llm_trace": llm_trace,
        }

    return review_check_compiler_node


def _compiled_checks_from_metadata(state: GraphState, task_id: str) -> List[ReviewCheck]:
    metadata = state.get("metadata", {}) or {}
    block = metadata.get("review_checks", {}) if isinstance(metadata, dict) else {}
    by_task = block.get("by_task", {}) if isinstance(block, dict) else {}
    slot = by_task.get(task_id, {}) if isinstance(by_task, dict) else {}
    raw_checks = slot.get("compiled_checks", []) if isinstance(slot, dict) else []
    checks: List[ReviewCheck] = []
    for raw in raw_checks or []:
        try:
            checks.append(raw if isinstance(raw, ReviewCheck) else ReviewCheck.model_validate(raw))
        except Exception:
            continue
    return checks


def _is_vague_question(question: str) -> bool:
    text = question.strip().lower()
    if not text:
        return True
    if any(marker == text or marker in text for marker in _VAGUE_QUESTIONS):
        return True
    words = re.findall(r"[a-zA-Z0-9_]+", text)
    return len(words) < 5


def _changed_files_from_diff(git_diff: str) -> set[str]:
    files: set[str] = set()
    for line in git_diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                files.add(parts[3].removeprefix("b/").replace("\\", "/"))
        elif line.startswith("+++ b/"):
            files.add(line[6:].strip().replace("\\", "/"))
    return {path for path in files if path and path != "/dev/null"}


def _anchor_matches_changed_surface(
    check: ReviewCheck,
    *,
    state: GraphState | None = None,
    task: ReviewTask | None = None,
    slot: Mapping[str, Any] | None = None,
) -> bool:
    if state is None and task is None:
        return True
    file_path = check.file_path.replace("\\", "/")
    task_files = {path.replace("\\", "/") for path in (task.target_files if task else [])}
    changed_files = _changed_files_from_diff(str((state or {}).get("git_diff") or ""))
    if task_files and file_path not in task_files:
        return False
    if changed_files and file_path not in changed_files:
        return False

    anchor = check.changed_code_anchor.strip().lower()
    if not anchor:
        return False
    if file_path.lower() in anchor or anchor in file_path.lower():
        return True

    evidence_blob = ""
    if state is not None:
        evidence_blob += "\n" + str(state.get("git_diff") or "")
    if slot is not None:
        evidence_blob += "\n" + _task_evidence_text(slot)
    tokens = _meaningful_tokens(anchor)
    if not tokens:
        return False
    blob = evidence_blob.lower()
    return any(token in blob for token in tokens)


def validate_review_check(
    check: ReviewCheck,
    *,
    state: GraphState | None = None,
    task: ReviewTask | None = None,
    slot: Mapping[str, Any] | None = None,
) -> List[str]:
    reasons: List[str] = []
    if not check.file_path.strip():
        reasons.append("missing_file_path")
    if not check.changed_code_anchor.strip():
        reasons.append("missing_changed_code_anchor")
    elif not _anchor_matches_changed_surface(check, state=state, task=task, slot=slot):
        reasons.append("anchor_not_in_changed_code")
    if _is_vague_question(check.behavioral_question):
        reasons.append("vague_behavioral_question")
    if not check.affected_invariant.strip():
        reasons.append("missing_affected_invariant")
    if not [item for item in check.required_evidence if str(item).strip()]:
        reasons.append("missing_required_evidence")
    if not [item for item in check.suppress_criteria if str(item).strip()]:
        reasons.append("missing_suppress_criteria")
    if not [item for item in check.report_criteria if str(item).strip()]:
        reasons.append("missing_report_criteria")
    if not [item for item in check.allowed_retrieval if str(item).strip()]:
        reasons.append("missing_allowed_retrieval")
    if check.budget < 1:
        reasons.append("invalid_budget")
    return reasons


def make_review_check_validator_node():
    node_name = "review_check_validator"

    def review_check_validator_node(state: GraphState) -> Dict[str, Any]:
        task = _task_from_state(state)
        if task is None:
            return {"node_history": [f"{node_name}:skipped"]}

        valid: List[ReviewCheck] = []
        invalid: List[InvalidReviewCheck] = []
        reason_counts: Dict[str, int] = {}
        slot = _pipeline_slot(state, task.id)
        for check in _compiled_checks_from_metadata(state, task.id):
            reasons = validate_review_check(check, state=state, task=task, slot=slot)
            if reasons:
                invalid.append(InvalidReviewCheck(check=check, reasons=reasons))
                for reason in reasons:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
            else:
                valid.append(check)

        metadata = _set_task_review_checks_meta(
            state,
            task.id,
            {
                "valid_check_ids": [check.check_id for check in valid],
                "invalid_checks": [item.model_dump(mode="json") for item in invalid],
                "validation": {
                    "valid_count": len(valid),
                    "invalid_count": len(invalid),
                    "reason_counts": reason_counts,
                },
            },
        )
        if _trace_enabled(state):
            trace_logger.info(
                "TRACE %s run_id=%s task_id=%s valid=%s invalid=%s",
                node_name,
                state.get("run_id", "unknown"),
                task.id,
                len(valid),
                len(invalid),
            )
        return {
            "review_checks": valid,
            "invalid_review_checks": invalid,
            "metadata": metadata,
            "node_history": [node_name],
        }

    return review_check_validator_node


def _checks_for_task(state: GraphState, task_id: str) -> List[ReviewCheck]:
    out: List[ReviewCheck] = []
    for raw in state.get("review_checks", []) or []:
        try:
            check = raw if isinstance(raw, ReviewCheck) else ReviewCheck.model_validate(raw)
        except Exception:
            continue
        if check.patch_task_id == task_id:
            out.append(check)
    return out


def _results_for_task(state: GraphState, task_id: str) -> List[ReviewCheckResult]:
    out: List[ReviewCheckResult] = []
    for raw in state.get("review_check_results", []) or []:
        try:
            result = raw if isinstance(raw, ReviewCheckResult) else ReviewCheckResult.model_validate(raw)
        except Exception:
            continue
        if result.patch_task_id == task_id:
            out.append(result)
    return out


def _latest_result_by_check(state: GraphState, task_id: str) -> Dict[str, ReviewCheckResult]:
    latest: Dict[str, ReviewCheckResult] = {}
    for result in _results_for_task(state, task_id):
        latest[result.check_id] = result
    return latest


def _request_count_for_check(state: GraphState, check_id: str) -> int:
    prefix = f"check:{check_id}:"
    count = 0
    for req in state.get("focused_context_requests", []) or []:
        if isinstance(req, FocusedContextRequest):
            request_id = req.request_id
        elif isinstance(req, dict):
            request_id = str(req.get("request_id") or "")
        else:
            request_id = ""
        if request_id.startswith(prefix):
            count += 1
    return count


def _next_request_id_for_check(state: GraphState, check: ReviewCheck) -> str:
    return f"check:{check.check_id}:{_request_count_for_check(state, check.check_id) + 1}"


def _check_budget_remaining(state: GraphState, check: ReviewCheck) -> bool:
    return _request_count_for_check(state, check.check_id) < check.budget


def _allows_focused_retrieval(check: ReviewCheck) -> bool:
    allowed = [str(item).strip().lower() for item in check.allowed_retrieval if str(item).strip()]
    if not allowed:
        return False
    if all(item in {"none", "task_evidence", "task evidence"} for item in allowed):
        return False
    return True


def _missing_evidence_for_check(
    *,
    state: GraphState,
    task_id: str,
    check: ReviewCheck,
    slot: Mapping[str, Any],
) -> List[str]:
    latest = _latest_result_by_check(state, task_id).get(check.check_id)
    if latest is not None:
        if not _allows_focused_retrieval(check):
            return []
        return [item for item in latest.missing_evidence if str(item).strip()]
    if not _check_needs_focused_context(check, slot):
        return []
    evidence_blob = _task_evidence_text(slot)
    missing = [
        requirement
        for requirement in check.required_evidence
        if str(requirement).strip() and not _evidence_covers_requirement(requirement, evidence_blob)
    ]
    if missing:
        return missing
    return list(check.required_evidence[:3])


def _executable_checks_for_task(state: GraphState, task_id: str) -> List[ReviewCheck]:
    latest = _latest_result_by_check(state, task_id)
    checks: List[ReviewCheck] = []
    for check in _checks_for_task(state, task_id):
        result = latest.get(check.check_id)
        if result is None:
            checks.append(check)
            continue
        if result.decision in _TERMINAL_CHECK_DECISIONS:
            continue
        if result.missing_evidence and _check_budget_remaining(state, check):
            checks.append(check)
    return checks


def should_continue_review_check_loop(state: GraphState) -> bool:
    task = _task_from_state(state)
    if task is None:
        return False
    checks = {check.check_id: check for check in _checks_for_task(state, task.id)}
    latest = _latest_result_by_check(state, task.id)
    for check_id, result in latest.items():
        check = checks.get(check_id)
        if check is None:
            continue
        if result.decision in _TERMINAL_CHECK_DECISIONS:
            continue
        if result.missing_evidence and _check_budget_remaining(state, check):
            return True
    return False


def _meaningful_tokens(text: str) -> set[str]:
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


def _evidence_covers_requirement(requirement: str, evidence_blob: str) -> bool:
    tokens = _meaningful_tokens(requirement)
    if not tokens:
        return False
    blob = evidence_blob.lower()
    hits = sum(1 for token in tokens if token in blob)
    return hits >= max(1, min(2, len(tokens)))


def _check_needs_focused_context(check: ReviewCheck, slot: Mapping[str, Any]) -> bool:
    if not _allows_focused_retrieval(check):
        return False
    evidence_blob = _task_evidence_text(slot)
    if any(marker in " ".join(check.required_evidence).lower() for marker in _FOCUS_MARKERS):
        return True
    return any(
        not _evidence_covers_requirement(requirement, evidence_blob)
        for requirement in check.required_evidence
    )


def _is_generic_query(text: str) -> bool:
    tokens = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower()))
    return bool(tokens) and tokens.issubset(_GENERIC_QUERY_TOKENS)


def _query_for_requirement(requirement: str, check: ReviewCheck) -> str:
    text = re.sub(r"\s+", " ", requirement).strip()
    if _is_generic_query(text):
        text = " ".join(
            part
            for part in (
                check.file_path,
                check.changed_code_anchor,
                check.affected_invariant,
                requirement,
            )
            if str(part).strip()
        )
    if len(text) > 160:
        text = text[:157] + "..."
    return text


def _focused_request_signature(req: FocusedContextRequest | Mapping[str, Any]) -> tuple[Any, ...]:
    if isinstance(req, FocusedContextRequest):
        file_paths = req.file_paths
        symbol_queries = req.symbol_queries
        text_queries = req.text_queries
    else:
        file_paths = req.get("file_paths", []) if isinstance(req.get("file_paths"), list) else []
        symbol_queries = req.get("symbol_queries", []) if isinstance(req.get("symbol_queries"), list) else []
        text_queries = req.get("text_queries", []) if isinstance(req.get("text_queries"), list) else []
    return (
        tuple(sorted(str(path).strip().replace("\\", "/") for path in file_paths if str(path).strip())),
        tuple(sorted(str(query).strip().lower() for query in symbol_queries if str(query).strip())),
        tuple(sorted(str(query).strip().lower() for query in text_queries if str(query).strip())),
    )


def make_review_check_context_planner_node():
    node_name = "review_check_context_planner"

    def review_check_context_planner_node(state: GraphState) -> Dict[str, Any]:
        task = _task_from_state(state)
        if task is None:
            return {"node_history": [f"{node_name}:skipped"]}

        slot = _pipeline_slot(state, task.id)
        existing_ids = {
            getattr(req, "request_id", None) if isinstance(req, FocusedContextRequest) else req.get("request_id")
            for req in (state.get("focused_context_requests", []) or [])
            if isinstance(req, (FocusedContextRequest, dict))
        }
        existing_signatures = {
            _focused_request_signature(req)
            for req in (state.get("focused_context_requests", []) or [])
            if isinstance(req, (FocusedContextRequest, dict))
        }
        scope = allowed_review_paths(state, task_target_files=task.target_files)
        requests: List[FocusedContextRequest] = []
        missing_by_check: Dict[str, List[str]] = {}
        for check in _executable_checks_for_task(state, task.id):
            missing_evidence = _missing_evidence_for_check(
                state=state,
                task_id=task.id,
                check=check,
                slot=slot,
            )
            if not missing_evidence:
                continue
            missing_by_check[check.check_id] = missing_evidence
            if not _check_budget_remaining(state, check):
                continue
            request_id = _next_request_id_for_check(state, check)
            if request_id in existing_ids:
                continue
            req = FocusedContextRequest(
                request_id=request_id,
                candidate_id=check.check_id,
                requested_by_specialty=task.specialty,
                file_read_mode="slice",
                file_paths=[check.file_path] if check.file_path else task.target_files[:1],
                symbol_queries=[check.changed_code_anchor] if check.changed_code_anchor else [],
                text_queries=[
                    _query_for_requirement(req_text, check)
                    for req_text in missing_evidence[:3]
                    if str(req_text).strip()
                ],
                reason=(
                    f"Gather missing evidence for review check {check.check_id} "
                    f"at {check.file_path}:{check.line_start}-{check.line_end}; "
                    f"anchor={check.changed_code_anchor}; invariant={check.affected_invariant}; "
                    f"missing={', '.join(missing_evidence[:2])}"
                ),
            )
            clamped = clamp_focused_context_request(
                req,
                scope,
                fallback_path=check.file_path or (task.target_files[0] if task.target_files else None),
            )
            signature = _focused_request_signature(clamped)
            if signature in existing_signatures:
                continue
            existing_signatures.add(signature)
            requests.append(clamped)

        metadata = _set_task_review_checks_meta(
            state,
            task.id,
            {
                "context_requests": [req.model_dump(mode="json") for req in requests],
                "context_request_count": len(requests),
                "missing_evidence_by_check": missing_by_check,
                "loop_pending": bool(requests),
            },
        )
        if _trace_enabled(state):
            trace_logger.info(
                "TRACE %s run_id=%s task_id=%s requests=%s",
                node_name,
                state.get("run_id", "unknown"),
                task.id,
                len(requests),
            )
        return {
            "focused_context_requests": requests,
            "metadata": metadata,
            "node_history": [node_name],
        }

    return review_check_context_planner_node


def _focused_context_for_check(state: GraphState, check_id: str) -> str:
    chunks: List[str] = []
    focused = focused_snippets_for_candidate(state, check_id, max_chars=8000)
    if focused.strip():
        chunks.append(focused)
    for request_id, raw in (state.get("focused_context_results", {}) or {}).items():
        if not str(request_id).startswith(f"check:{check_id}:"):
            continue
        try:
            candidate_id = raw.candidate_id if hasattr(raw, "candidate_id") else raw.get("candidate_id")
        except Exception:
            candidate_id = ""
        if candidate_id == check_id:
            continue
        chunks.append(str(raw)[:4000])
    return "\n\n".join(chunk for chunk in chunks if chunk.strip())


def _render_executor_prompt(
    state: GraphState,
    task: ReviewTask,
    checks: List[ReviewCheck],
    slot: Mapping[str, Any],
) -> str:
    focused = {
        check.check_id: _focused_context_for_check(state, check.check_id)
        for check in checks
    }
    sections = {
        "Assigned Task": (
            f"Task ID: {task.id}\n"
            f"Title: {task.title}\n"
            f"Description: {task.description}\n"
            f"Target files: {task.target_files}"
        ),
        "Validated Checks JSON": _json_for_prompt(
            [check.model_dump(mode="json") for check in checks],
            max_chars=10000,
        ),
        "Repository Code Evidence": str(slot.get("direct_context") or "")[:16000],
        "Focused Evidence By Check": _json_for_prompt(focused, max_chars=10000),
        "Mental Model Excerpt": str(slot.get("mental_model_excerpt") or ""),
        "Review KB Context": str(slot.get("review_kb_excerpt") or ""),
    }
    return render_reviewer_prompt("review_check_executor.md", sections)


def _file_contents_from_slot(slot: Mapping[str, Any]) -> Mapping[str, str] | None:
    te = slot.get("task_evidence") if isinstance(slot.get("task_evidence"), dict) else {}
    if isinstance(te, dict) and isinstance(te.get("file_contents"), dict):
        return te["file_contents"]
    return None


def _normalize_executor_results(
    *,
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    checks: List[ReviewCheck],
    results: Iterable[ReviewCheckResult],
) -> tuple[List[ReviewCheckResult], List[str]]:
    warnings: List[str] = []
    by_check = {check.check_id: check for check in checks}
    normalized: List[ReviewCheckResult] = []
    for raw in results:
        if raw.check_id not in by_check:
            warnings.append(f"executor_result_unknown_check:{raw.check_id}")
            continue
        check = by_check[raw.check_id]
        result = raw.model_copy(update={"patch_task_id": task.id})
        candidate = result.candidate
        if candidate is not None:
            cid = candidate.candidate_id.strip() or f"{check.check_id}:candidate"
            patched = candidate.model_copy(
                update={
                    "candidate_id": cid,
                    "patch_task_id": task.id,
                    "file_path": candidate.file_path or check.file_path,
                    "line_start": candidate.line_start or check.line_start,
                    "line_end": max(candidate.line_end or check.line_end, candidate.line_start or check.line_start),
                }
            )
            normed = _normalize_candidates(
                task,
                [patched],
                pipeline_slot={"task_evidence": {"file_contents": _file_contents_from_slot(slot) or {}}},
                git_diff=state.get("git_diff", "") or "",
            )
            if normed:
                result = result.model_copy(update={"candidate": normed[0], "decision": "candidate"})
            else:
                warnings.append(f"executor_candidate_dropped_by_normalizer:{cid}")
                result = result.model_copy(update={"candidate": None, "decision": "unsupported"})
        normalized.append(result)
        if (
            result.decision == "unsupported"
            and result.missing_evidence
            and not _check_budget_remaining(state, check)
        ):
            result = result.model_copy(
                update={
                    "decision": "budget_exhausted",
                    "warnings": list(result.warnings) + ["review_check_budget_exhausted"],
                }
            )
            normalized[-1] = result
    present = {item.check_id for item in normalized}
    for check in checks:
        if check.check_id not in present:
            normalized.append(
                ReviewCheckResult(
                    check_id=check.check_id,
                    patch_task_id=task.id,
                    decision="unsupported",
                    warnings=["executor_missing_result"],
                )
            )
    return normalized, warnings


def make_review_check_executor_node(
    model_key: str | None = None,
    use_llm: bool = True,
    settings: Settings | None = None,
):
    node_name = "review_check_executor"

    def review_check_executor_node(state: GraphState) -> Dict[str, Any]:
        task = _task_from_state(state)
        if task is None:
            return {"node_history": [f"{node_name}:skipped"]}
        checks = _executable_checks_for_task(state, task.id)
        if not checks:
            metadata = _set_task_review_checks_meta(
                state,
                task.id,
                {"executor_warnings": ["no_executable_checks"], "executor_result_count": 0},
            )
            return {"metadata": metadata, "node_history": [f"{node_name}:skipped"]}

        resolved = settings or get_settings()
        slot = _pipeline_slot(state, task.id)
        warnings: List[str] = []
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []
        results: List[ReviewCheckResult] = []

        if use_llm:
            selected_model = model_key or resolved.reviewer_worker_model_key
            try:
                llm = Models.worker(
                    ReviewCheckExecutorOutput,
                    model_key=selected_model,
                    max_completion_tokens=resolved.reviewer_critiquer_max_completion_tokens,
                )
                prompt = _render_executor_prompt(state, task, checks, slot)
                traced = trace_llm_call(
                    llm,
                    prompt,
                    state=state,
                    node_name=node_name,
                    model_key=selected_model,
                    schema_name="ReviewCheckExecutorOutput",
                    input_summary={"task_id": task.id, "check_ids": [c.check_id for c in checks]},
                )
                response = parse_structured_output(traced.result, ReviewCheckExecutorOutput)
                llm_tokens = traced.tokens
                llm_trace = traced.trace_records
                results, norm_warnings = _normalize_executor_results(
                    state=state,
                    task=task,
                    slot=slot,
                    checks=checks,
                    results=response.results,
                )
                warnings.extend(response.warnings)
                warnings.extend(norm_warnings)
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                warnings.append(f"{node_name}_llm_failed:{exc.__class__.__name__}: {exc}")
                logger.warning("%s failed for task_id=%s: %s", node_name, task.id, exc)

        if not results:
            results = [
                ReviewCheckResult(
                    check_id=check.check_id,
                    patch_task_id=task.id,
                    decision="unsupported",
                    warnings=["review_check_executor_no_result"],
                )
                for check in checks
            ]

        metadata = _set_task_review_checks_meta(
            state,
            task.id,
            {
                "executor_result_count": len(results),
                "executor_decision_counts": {
                    decision: sum(1 for result in results if result.decision == decision)
                    for decision in sorted({result.decision for result in results})
                },
                "executor_candidate_ids": [
                    result.candidate.candidate_id
                    for result in results
                    if result.candidate is not None
                ],
                "executor_warnings": warnings,
            },
        )
        if _trace_enabled(state):
            trace_logger.info(
                "TRACE %s run_id=%s task_id=%s results=%s candidates=%s",
                node_name,
                state.get("run_id", "unknown"),
                task.id,
                len(results),
                sum(1 for result in results if result.candidate is not None),
            )
        return {
            "review_check_results": results,
            "metadata": metadata,
            "node_history": [node_name],
            "token_usage": llm_tokens,
            "llm_trace": llm_trace,
        }

    return review_check_executor_node


def _candidate_speculative(candidate: CandidateFinding, result: ReviewCheckResult) -> bool:
    if candidate.claim_type == "uncertain":
        return True
    blob = " ".join(
        [
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            result.reportable_reason,
        ]
    ).lower()
    return any(marker in blob for marker in _SPECULATIVE_MARKERS)


def _candidate_names_affected_path(candidate: CandidateFinding, result: ReviewCheckResult) -> bool:
    blob = " ".join(
        [
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            result.reportable_reason,
        ]
    ).lower()
    return any(marker in blob for marker in _AFFECTED_PATH_MARKERS)


def _result_has_repo_evidence(result: ReviewCheckResult, check: ReviewCheck) -> bool:
    refs = [ref.strip().replace("\\", "/") for ref in result.evidence_refs if str(ref).strip()]
    if not refs:
        return False
    check_path = check.file_path.replace("\\", "/")
    return any(check_path in ref or ref.startswith("focused_context:") for ref in refs)


def _candidate_passes_gate(
    candidate: CandidateFinding,
    result: ReviewCheckResult,
    check: ReviewCheck,
) -> tuple[bool, str]:
    if candidate.file_path.replace("\\", "/") != check.file_path.replace("\\", "/"):
        return False, "candidate_anchor_file_mismatch"
    if candidate.line_end < candidate.line_start or candidate.line_start < 1:
        return False, "invalid_candidate_line_range"
    if not check.affected_invariant.strip():
        return False, "missing_check_invariant"
    if not candidate.failure_mode.strip():
        return False, "missing_failure_mode"
    if not _candidate_names_affected_path(candidate, result):
        return False, "missing_affected_path"
    if not candidate.evidence_summary.strip():
        return False, "missing_supporting_evidence"
    if not _result_has_repo_evidence(result, check):
        return False, "missing_repo_evidence_ref"
    if not result.reportable_reason.strip():
        return False, "missing_reportable_reason"
    if result.suppressing_evidence:
        return False, "suppressing_evidence_present"
    if _candidate_speculative(candidate, result):
        return False, "speculative_or_uncertain_claim"
    if not (candidate.recommendation or "").strip():
        return False, "missing_recommendation"
    return True, "evidence_gate_passed"


def make_review_check_evidence_gate_node():
    node_name = "review_check_evidence_gate"

    def review_check_evidence_gate_node(state: GraphState) -> Dict[str, Any]:
        task = _task_from_state(state)
        if task is None:
            return {"node_history": [f"{node_name}:skipped"]}
        checks = {check.check_id: check for check in _checks_for_task(state, task.id)}
        promoted: List[CandidateFinding] = []
        lifecycle: Dict[str, Any] = {}
        gated_results: List[ReviewCheckResult] = []
        dropped = 0
        for result in _results_for_task(state, task.id):
            check = checks.get(result.check_id)
            candidate = result.candidate
            if result.decision != "candidate" or candidate is None or check is None:
                continue
            passed, reason = _candidate_passes_gate(candidate, result, check)
            if passed:
                promoted.append(candidate)
                gated_results.append(
                    result.model_copy(update={"gate_decision": "passed", "gate_reason": reason})
                )
                lifecycle[candidate.candidate_id] = {
                    "decision": "passed",
                    "check_id": result.check_id,
                    "reason": reason,
                }
            else:
                dropped += 1
                gated_results.append(
                    result.model_copy(update={"gate_decision": "dropped", "gate_reason": reason})
                )
                lifecycle[candidate.candidate_id] = {
                    "decision": "dropped",
                    "check_id": result.check_id,
                    "reason": reason,
                }

        latest_results = list(_latest_result_by_check(state, task.id).values())
        health_warnings: List[str] = []
        if checks and latest_results and not any(result.candidate is not None for result in latest_results):
            decisions = {result.decision for result in latest_results}
            if decisions.issubset({"no_finding", "unsupported", "suppressed", "budget_exhausted"}):
                health_warnings.append("no_executor_candidates_for_valid_checks")
        if checks and not gated_results:
            health_warnings.append("evidence_gate_not_exercised")

        metadata = _set_task_review_checks_meta(
            state,
            task.id,
            {
                "gate": {
                    "promoted_count": len(promoted),
                    "dropped_count": dropped,
                    "evaluated_count": len(gated_results),
                    "candidate_lifecycle": lifecycle,
                    "health_warnings": health_warnings,
                },
                "health_warnings": health_warnings,
            },
        )
        if _trace_enabled(state):
            trace_logger.info(
                "TRACE %s run_id=%s task_id=%s promoted=%s dropped=%s",
                node_name,
                state.get("run_id", "unknown"),
                task.id,
                len(promoted),
                dropped,
            )
        return {
            "candidate_findings": promoted,
            "review_check_results": gated_results,
            "task_status_by_id": {task.id: "completed"},
            "metadata": metadata,
            "node_history": [node_name],
        }

    return review_check_evidence_gate_node


def make_review_check_scout_node():
    """V1 escape hatch placeholder; disabled until routed by a separate flag."""

    node_name = "review_check_scout"

    def review_check_scout_node(state: GraphState) -> Dict[str, Any]:
        task = _task_from_state(state)
        task_id = task.id if task is not None else "unknown"
        metadata = _set_task_review_checks_meta(
            state,
            task_id,
            {"scout": {"status": "disabled_v1"}},
        )
        return {"metadata": metadata, "node_history": [f"{node_name}:skipped"]}

    return review_check_scout_node
