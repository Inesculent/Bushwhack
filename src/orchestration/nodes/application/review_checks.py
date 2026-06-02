"""Check-first review nodes for the adversarial reviewer ablation."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Literal, Mapping, Sequence

from pydantic import BaseModel, Field

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
    ReviewSurface,
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
from src.orchestration.context.surface_ledger import (
    changed_files_from_diff,
    changed_file_sources_from_state,
    surface_by_id,
    surface_ledger_from_state,
)
from src.orchestration.nodes.application.review_check_executor_support import (
    missing_evidence_for_weak_no_finding as _support_missing_evidence_for_weak_no_finding,
    no_finding_needs_semantic_suppression_audit,
    normalize_executor_results as _support_normalize_executor_results,
)
from src.orchestration.nodes.application import review_check_compiler_support as compiler_support
from src.orchestration.nodes.application.review_check_source_scope import (
    changed_task_files as _changed_task_files,
    compiled_check_is_source_local as _source_scope_compiled_check_is_source_local,
    evidence_covers_requirement as _evidence_covers_requirement,
    evidence_requirements_for_check as _evidence_requirements_for_check,
    meaningful_tokens as _meaningful_tokens,
    requires_external_evidence as _requires_external_evidence,
    task_evidence_text as _task_evidence_text,
    tokens_overlap as _tokens_overlap,
)
from src.orchestration.prompts.renderer import render_reviewer_prompt

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

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
_GENERIC_QUERY_TOKENS = {"changed", "code", "behavior", "repository", "evidence", "context"}
_EXECUTOR_BATCH_SIZE = 3
_GENERIC_QUERY_PHRASES = (
    "changed code behavior",
    "full file content",
    "confirm returns",
    "confirm explicit bounds",
    "confirm unexpected behavior",
)


class SuppressionAuditItem(BaseModel):
    check_id: str
    verdict: Literal["sufficient", "insufficient", "unsupported"] = "unsupported"
    rationale: str = Field(default="", max_length=500)
    missing_evidence: List[str] = Field(default_factory=list)


class SuppressionAuditOutput(BaseModel):
    items: List[SuppressionAuditItem] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _explicit_surface_ledger_from_state(state: GraphState | None) -> List[ReviewSurface]:
    if state is None:
        return []
    metadata = state.get("metadata", {}) or {}
    slot = metadata.get("mental_model", {}) if isinstance(metadata, Mapping) else {}
    if not isinstance(slot, Mapping) or not isinstance(slot.get("surface_ledger"), list):
        return []
    return surface_ledger_from_state(state)


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


























_BROAD_SURFACE_INVARIANTS = (
    "preserves its existing observable contract",
    "preserve changed-surface behavior",
    "preserve api contract",
    "preserves caller-visible inputs, outputs, and exception behavior",
    "assigned surface behavior",
)

















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
        checks: List[ReviewCheck] = compiler_support.checks_from_surface_invariants(
            state,
            task,
            settings=resolved,
        )
        check_origins = compiler_support.origins_for_checks(
            checks,
            "surface_invariant",
            "derived_from_behavioral_surface_invariant",
        )
        if checks:
            warnings.append(f"surface_invariant_checks_added:{len(checks)}")

        if use_llm:
            selected_model = model_key or resolved.reviewer_worker_model_key
            try:
                llm = Models.worker(
                    ReviewCheckCompilerOutput,
                    model_key=selected_model,
                    max_completion_tokens=resolved.reviewer_critiquer_max_completion_tokens,
                )
                prompt = compiler_support.render_compiler_prompt(state, task, slot)
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
                llm_checks = compiler_support.normalize_compiled_checks(state, task, response.checks)
                llm_origins = compiler_support.origins_for_checks(
                    llm_checks,
                    "llm_compiled",
                    "compiled_by_review_check_llm",
                )
                merged_origins = {**check_origins, **llm_origins}
                checks = compiler_support.prioritize_compiled_checks(
                    compiler_support.dedupe_checks([*llm_checks, *checks]),
                    task=task,
                    slot=slot,
                )
                check_origins = {
                    check.check_id: merged_origins.get(
                        check.check_id,
                        compiler_support.check_origin(
                            check,
                            "llm_compiled",
                            "compiled_by_review_check_llm",
                        ),
                    )
                    for check in checks
                }
                summary = response.summary
                warnings.extend(response.warnings)
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}: {exc}")
                logger.warning("%s failed for task_id=%s: %s", node_name, task.id, exc)

        if not checks:
            checks = compiler_support.fallback_checks(state, task, slot)
            check_origins = compiler_support.origins_for_checks(
                checks,
                "deterministic_fallback",
                "deterministic_fallback_from_task_evidence",
            )
            if not summary:
                summary = "Deterministic fallback checks from task evidence obligations."
            warnings.append("review_check_compiler_fallback_used")

        checks, coverage_floor, check_origins = compiler_support.ensure_compiler_coverage_floor(
            state=state,
            task=task,
            checks=checks,
            check_origins=check_origins,
        )
        warnings.extend(coverage_floor.get("warnings", []))

        metadata = _set_task_review_checks_meta(
            state,
            task.id,
            {
                "compiler_summary": summary,
                "compiled_checks": [check.model_dump(mode="json") for check in checks],
                "compiled_check_origins": check_origins,
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
    changed_files = (
        {
            path
            for paths in changed_file_sources_from_state(state or {}).values()
            for path in paths
        }
        if state is not None
        else set()
    ) or set(changed_files_from_diff(str((state or {}).get("git_diff") or "")))
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


def _check_is_cross_surface(check: ReviewCheck) -> bool:
    blob = f"{check.changed_code_anchor} {check.behavioral_question} {check.affected_invariant}".lower()
    return "cross-surface" in blob or "integration" in blob or "call path" in blob


def validate_review_check(
    check: ReviewCheck,
    *,
    state: GraphState | None = None,
    task: ReviewTask | None = None,
    slot: Mapping[str, Any] | None = None,
) -> List[str]:
    reasons: List[str] = []
    ledger = _explicit_surface_ledger_from_state(state)
    if ledger:
        by_id = surface_by_id(ledger)
        valid_surface_ids = [sid for sid in check.surface_ids if sid in by_id]
        if not valid_surface_ids:
            reasons.append("missing_surface_id")
        elif len(valid_surface_ids) > 1 and not _check_is_cross_surface(check):
            reasons.append("ambiguous_surface_id")
        elif len(valid_surface_ids) == 1:
            surface = by_id[valid_surface_ids[0]]
            if check.file_path.strip().replace("\\", "/") != surface.file_path:
                reasons.append("surface_file_mismatch")
            if surface.line_start is None:
                reasons.append("missing_surface_line_anchor")
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
    requirements = _evidence_requirements_for_check(check)
    missing = [
        requirement
        for requirement in requirements
        if str(requirement).strip()
        and (
            _requires_external_evidence(requirement)
            or not _evidence_covers_requirement(requirement, evidence_blob)
        )
    ]
    if missing:
        return missing
    return list(requirements[:3])


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






def _check_needs_focused_context(check: ReviewCheck, slot: Mapping[str, Any]) -> bool:
    if not _allows_focused_retrieval(check):
        return False
    evidence_blob = _task_evidence_text(slot)
    requirements = _evidence_requirements_for_check(check)
    requirement_blob = " ".join(requirements).lower()
    if any(marker in requirement_blob for marker in _FOCUS_MARKERS):
        return True
    if any(_requires_external_evidence(requirement) for requirement in requirements):
        return True
    return any(
        not _evidence_covers_requirement(requirement, evidence_blob)
        for requirement in requirements
    )



def _compiled_check_is_source_local(
    check: ReviewCheck,
    meta: Mapping[str, Any] | None,
    slot: Mapping[str, Any] | None,
    task_files: set[str],
) -> bool:
    return _source_scope_compiled_check_is_source_local(
        check,
        meta,
        _task_evidence_text(slot or {}) if slot else None,
        task_files,
        _evidence_requirements_for_check(check),
    )


def _is_generic_query(text: str) -> bool:
    tokens = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower()))
    lowered = text.lower().replace("|", " ")
    return (
        (bool(tokens) and tokens.issubset(_GENERIC_QUERY_TOKENS))
        or any(phrase in lowered for phrase in _GENERIC_QUERY_PHRASES)
    )


def _query_for_requirement(requirement: str, check: ReviewCheck) -> str:
    text = re.sub(r"[|]+", " ", requirement)
    text = re.sub(r"\s+", " ", text).strip()
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
    elif check.changed_code_anchor and check.changed_code_anchor.lower() not in text.lower():
        text = f"{check.changed_code_anchor} {text}"
    if check.file_path and check.file_path.lower() not in text.lower():
        text = f"{check.file_path} {text}"
    if len(text) > 160:
        text = text[:157] + "..."
    return text


def _focused_request_signature(req: FocusedContextRequest | Mapping[str, Any]) -> tuple[Any, ...]:
    if isinstance(req, FocusedContextRequest):
        file_read_mode = req.file_read_mode
        file_paths = req.file_paths
        symbol_queries = req.symbol_queries
        text_queries = req.text_queries
    else:
        file_read_mode = str(req.get("file_read_mode") or "slice")
        file_paths = req.get("file_paths", []) if isinstance(req.get("file_paths"), list) else []
        symbol_queries = req.get("symbol_queries", []) if isinstance(req.get("symbol_queries"), list) else []
        text_queries = req.get("text_queries", []) if isinstance(req.get("text_queries"), list) else []
    return (
        file_read_mode,
        tuple(sorted(str(path).strip().replace("\\", "/") for path in file_paths if str(path).strip())),
        tuple(sorted(str(query).strip().lower() for query in symbol_queries if str(query).strip())),
        tuple(sorted(str(query).strip().lower() for query in text_queries if str(query).strip())),
    )


_TRUNCATED_CONTEXT_MARKERS = (
    "truncated",
    "only class declaration",
    "only the class declaration",
    "class body not",
    "body is not visible",
    "implementation is not visible",
    "implementation details are not visible",
    "full class definition",
)


def _has_full_file_request_for_check(state: GraphState, check: ReviewCheck) -> bool:
    for req in state.get("focused_context_requests", []) or []:
        if isinstance(req, FocusedContextRequest):
            candidate_id = req.candidate_id
            mode = req.file_read_mode
            paths = req.file_paths
        elif isinstance(req, dict):
            candidate_id = str(req.get("candidate_id") or "")
            mode = str(req.get("file_read_mode") or "")
            paths = req.get("file_paths", []) if isinstance(req.get("file_paths"), list) else []
        else:
            continue
        if candidate_id != check.check_id or mode != "full":
            continue
        norm_paths = {str(path).replace("\\", "/") for path in paths}
        if check.file_path.replace("\\", "/") in norm_paths:
            return True
    return False


def _should_retry_full_file_for_check(state: GraphState, check: ReviewCheck, latest: ReviewCheckResult | None) -> bool:
    if latest is None or latest.decision != "unsupported":
        return False
    if _has_full_file_request_for_check(state, check):
        return False
    blob = " ".join(
        [
            latest.reportable_reason,
            " ".join(latest.missing_evidence),
            " ".join(latest.warnings),
        ]
    ).lower()
    return any(marker in blob for marker in _TRUNCATED_CONTEXT_MARKERS)


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
            latest = _latest_result_by_check(state, task.id).get(check.check_id)
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
            file_read_mode = "full" if _should_retry_full_file_for_check(state, check, latest) else "slice"
            req = FocusedContextRequest(
                request_id=request_id,
                candidate_id=check.check_id,
                requested_by_specialty=task.specialty,
                file_read_mode=file_read_mode,
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


def _missing_evidence_for_weak_no_finding(check: ReviewCheck) -> List[str]:
    return _support_missing_evidence_for_weak_no_finding(check, _evidence_requirements_for_check)


def _check_batches(checks: Sequence[ReviewCheck]) -> List[List[ReviewCheck]]:
    return [list(checks[index : index + _EXECUTOR_BATCH_SIZE]) for index in range(0, len(checks), _EXECUTOR_BATCH_SIZE)]


def _render_suppression_audit_prompt(
    *,
    task: ReviewTask,
    checks_by_id: Mapping[str, ReviewCheck],
    results: Sequence[ReviewCheckResult],
) -> str:
    payload = []
    for result in results:
        check = checks_by_id[result.check_id]
        payload.append(
            {
                "check": check.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
            }
        )
    return (
        "Audit these no_finding review-check results.\n"
        "Decide whether each suppression directly answers the exact check dimension and report criteria.\n"
        "Return sufficient only when the suppressing evidence addresses the named behavior, not merely a nearby "
        "dimension such as outer return type, container shape, branch visibility, schema declaration, or generic "
        "absence of proof.\n"
        "Return insufficient when the result should become unsupported because the suppression proves only a "
        "neighboring dimension. Return unsupported when the evidence is too incomplete to decide.\n\n"
        f"Task: {task.model_dump(mode='json')}\n\n"
        f"Audit Items JSON:\n{_json_for_prompt(payload, max_chars=18000)}"
    )


def _audit_no_finding_suppressions(
    *,
    state: GraphState,
    task: ReviewTask,
    checks: Sequence[ReviewCheck],
    results: List[ReviewCheckResult],
    selected_model: str,
    llm_tokens: int,
    llm_trace: List[Dict[str, Any]],
    warnings: List[str],
) -> tuple[List[ReviewCheckResult], int, List[Dict[str, Any]], List[str]]:
    checks_by_id = {check.check_id: check for check in checks}
    targets = [
        result for result in results
        if result.check_id in checks_by_id
        and no_finding_needs_semantic_suppression_audit(result, checks_by_id[result.check_id])
    ]
    if not targets:
        return results, llm_tokens, llm_trace, warnings
    prompt = _render_suppression_audit_prompt(task=task, checks_by_id=checks_by_id, results=targets)
    audit_by_id: Dict[str, SuppressionAuditItem] = {}
    try:
        llm = Models.worker(SuppressionAuditOutput, model_key=selected_model, max_completion_tokens=1600)
        traced = trace_llm_call(
            llm,
            prompt,
            state=state,
            node_name="review_check_suppression_audit",
            model_key=selected_model,
            schema_name="SuppressionAuditOutput",
            input_summary={"task_id": task.id, "check_ids": [result.check_id for result in targets]},
        )
        response = parse_structured_output(traced.result, SuppressionAuditOutput)
        llm_tokens += traced.tokens
        llm_trace.extend(traced.trace_records)
        warnings.extend(response.warnings)
        audit_by_id = {
            item.check_id: item
            for item in response.items
            if item.check_id in checks_by_id
        }
    except Exception as exc:  # noqa: BLE001
        llm_trace.extend(trace_from_exception(exc))
        warnings.append(f"review_check_suppression_audit_failed:{exc.__class__.__name__}: {exc}")
    audited: List[ReviewCheckResult] = []
    target_ids = {result.check_id for result in targets}
    for result in results:
        if result.check_id not in target_ids:
            audited.append(result)
            continue
        item = audit_by_id.get(result.check_id)
        if item is not None and item.verdict == "sufficient":
            audited.append(
                result.model_copy(
                    update={
                        "warnings": list(result.warnings) + ["llm_suppression_audit_sufficient"],
                    }
                )
            )
            continue
        check = checks_by_id[result.check_id]
        missing = list(item.missing_evidence) if item is not None and item.missing_evidence else _missing_evidence_for_weak_no_finding(check)
        warning = "llm_suppression_audit_insufficient"
        if item is None:
            warning = "llm_suppression_audit_missing_or_failed"
        elif item.verdict == "unsupported":
            warning = "llm_suppression_audit_unsupported"
        audited.append(
            result.model_copy(
                update={
                    "decision": "unsupported",
                    "missing_evidence": missing[:3],
                    "suppressing_evidence": [],
                    "warnings": list(result.warnings) + [warning],
                    "reportable_reason": (item.rationale if item is not None else result.reportable_reason)[:500],
                }
            )
        )
        warnings.append(f"{warning}:{result.check_id}")
    return audited, llm_tokens, llm_trace, warnings


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
        missing_result_check_ids: List[str] = []
        executor_retry_count = 0
        executor_retry_success_count = 0

        if use_llm:
            selected_model = model_key or resolved.reviewer_worker_model_key
            for batch_index, batch in enumerate(_check_batches(checks), start=1):
                try:
                    llm = Models.worker(
                        ReviewCheckExecutorOutput,
                        model_key=selected_model,
                        max_completion_tokens=resolved.reviewer_critiquer_max_completion_tokens,
                    )
                    prompt = _render_executor_prompt(state, task, batch, slot)
                    traced = trace_llm_call(
                        llm,
                        prompt,
                        state=state,
                        node_name=node_name,
                        model_key=selected_model,
                        schema_name="ReviewCheckExecutorOutput",
                        input_summary={
                            "task_id": task.id,
                            "batch": batch_index,
                            "check_ids": [c.check_id for c in batch],
                        },
                    )
                    response = parse_structured_output(traced.result, ReviewCheckExecutorOutput)
                    llm_tokens += traced.tokens
                    llm_trace.extend(traced.trace_records)
                    changed_task_files = set(_changed_task_files(state, task))
                    batch_results, norm_warnings = _support_normalize_executor_results(
                        state=state,
                        task=task,
                        slot=slot,
                        checks=batch,
                        results=response.results,
                        git_diff=state.get("git_diff", "") or "",
                        check_budget_remaining=_check_budget_remaining,
                        evidence_requirements_for_check=_evidence_requirements_for_check,
                        compiled_check_is_source_local=lambda check: _compiled_check_is_source_local(
                            check,
                            None,
                            slot,
                            changed_task_files,
                        ),
                        include_missing_results=False,
                    )
                    present = {result.check_id for result in batch_results}
                    missing_checks = [check for check in batch if check.check_id not in present]
                    if missing_checks:
                        missing_result_check_ids.extend(check.check_id for check in missing_checks)
                        executor_retry_count += len(missing_checks)
                        warnings.extend(f"executor_missing_result:{check.check_id}" for check in missing_checks)
                        try:
                            retry_llm = Models.worker(
                                ReviewCheckExecutorOutput,
                                model_key=selected_model,
                                max_completion_tokens=resolved.reviewer_critiquer_max_completion_tokens,
                            )
                            retry_prompt = _render_executor_prompt(state, task, missing_checks, slot)
                            retry_traced = trace_llm_call(
                                retry_llm,
                                retry_prompt,
                                state=state,
                                node_name=node_name,
                                model_key=selected_model,
                                schema_name="ReviewCheckExecutorOutput",
                                input_summary={
                                    "task_id": task.id,
                                    "batch": batch_index,
                                    "retry_missing_check_ids": [c.check_id for c in missing_checks],
                                },
                            )
                            retry_response = parse_structured_output(retry_traced.result, ReviewCheckExecutorOutput)
                            llm_tokens += retry_traced.tokens
                            llm_trace.extend(retry_traced.trace_records)
                            retry_results, retry_warnings = _support_normalize_executor_results(
                                state=state,
                                task=task,
                                slot=slot,
                                checks=missing_checks,
                                results=retry_response.results,
                                git_diff=state.get("git_diff", "") or "",
                                check_budget_remaining=_check_budget_remaining,
                                evidence_requirements_for_check=_evidence_requirements_for_check,
                                compiled_check_is_source_local=lambda check: _compiled_check_is_source_local(
                                    check,
                                    None,
                                    slot,
                                    changed_task_files,
                                ),
                                include_missing_results=False,
                            )
                            retry_present = {result.check_id for result in retry_results}
                            executor_retry_success_count += len(retry_present)
                            batch_results.extend(retry_results)
                            warnings.extend(retry_response.warnings)
                            warnings.extend(retry_warnings)
                            still_missing = [
                                check for check in missing_checks if check.check_id not in retry_present
                            ]
                        except Exception as retry_exc:  # noqa: BLE001
                            llm_trace.extend(trace_from_exception(retry_exc))
                            warnings.append(
                                f"{node_name}_retry_failed:{batch_index}:{retry_exc.__class__.__name__}: {retry_exc}"
                            )
                            logger.warning(
                                "%s retry failed for task_id=%s batch=%s: %s",
                                node_name,
                                task.id,
                                batch_index,
                                retry_exc,
                            )
                            still_missing = missing_checks
                        for check in still_missing:
                            batch_results.append(
                                ReviewCheckResult(
                                    check_id=check.check_id,
                                    patch_task_id=task.id,
                                    decision="unsupported",
                                    warnings=["executor_missing_result_after_retry"],
                                )
                            )
                    batch_results, llm_tokens, llm_trace, warnings = _audit_no_finding_suppressions(
                        state=state,
                        task=task,
                        checks=batch,
                        results=batch_results,
                        selected_model=selected_model,
                        llm_tokens=llm_tokens,
                        llm_trace=llm_trace,
                        warnings=warnings,
                    )
                    results.extend(batch_results)
                    warnings.extend(response.warnings)
                    warnings.extend(norm_warnings)
                except Exception as exc:  # noqa: BLE001
                    llm_trace.extend(trace_from_exception(exc))
                    warnings.append(f"{node_name}_batch_failed:{batch_index}:{exc.__class__.__name__}: {exc}")
                    logger.warning("%s failed for task_id=%s batch=%s: %s", node_name, task.id, batch_index, exc)
                    results.extend(
                        ReviewCheckResult(
                            check_id=check.check_id,
                            patch_task_id=task.id,
                            decision="unsupported",
                            missing_evidence=_missing_evidence_for_weak_no_finding(check),
                            warnings=[f"review_check_executor_batch_failed:{batch_index}"],
                        )
                        for check in batch
                    )

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
                "executor_batch_size": _EXECUTOR_BATCH_SIZE,
                "executor_batch_count": len(_check_batches(checks)),
                "executor_missing_result_check_ids": list(dict.fromkeys(missing_result_check_ids)),
                "executor_retry_count": executor_retry_count,
                "executor_retry_success_count": executor_retry_success_count,
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


def _candidate_names_affected_path(
    candidate: CandidateFinding,
    result: ReviewCheckResult,
    check: ReviewCheck,
) -> bool:
    blob = " ".join(
        [
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            result.reportable_reason,
        ]
    ).lower()
    if any(marker in blob for marker in _AFFECTED_PATH_MARKERS):
        return True
    if _tokens_overlap(check.changed_code_anchor, blob):
        return True
    if _tokens_overlap(check.affected_invariant, blob):
        return True
    check_path = check.file_path.replace("\\", "/")
    if any(check_path in ref.replace("\\", "/") for ref in result.evidence_refs):
        return True
    return candidate.file_path.replace("\\", "/") == check_path and candidate.line_start >= 1


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
    if not _candidate_names_affected_path(candidate, result, check):
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
        gate_reason_counts: Dict[str, int] = {}
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
            gate_reason_counts[reason] = gate_reason_counts.get(reason, 0) + 1

        latest_results = list(_latest_result_by_check(state, task.id).values())
        health_warnings: List[str] = []
        if checks and latest_results and not any(result.candidate is not None for result in latest_results):
            decisions = {result.decision for result in latest_results}
            if decisions.issubset({"no_finding", "unsupported", "suppressed", "budget_exhausted"}):
                health_warnings.append("no_executor_candidates_for_valid_checks")
        if checks and not gated_results:
            health_warnings.append("evidence_gate_not_exercised")
        ledger = surface_ledger_from_state(state)
        by_id = surface_by_id(ledger)
        high_confidence_unsupported_surfaces = sorted(
            {
                sid
                for result in latest_results
                if result.decision in {"unsupported", "budget_exhausted"}
                for sid in (checks.get(result.check_id).surface_ids if checks.get(result.check_id) else [])
                if sid in by_id and by_id[sid].confidence >= 0.75
            }
        )

        metadata = _set_task_review_checks_meta(
            state,
            task.id,
            {
                "gate": {
                    "promoted_count": len(promoted),
                    "dropped_count": dropped,
                    "evaluated_count": len(gated_results),
                    "reason_counts": gate_reason_counts,
                    "candidate_lifecycle": lifecycle,
                    "health_warnings": health_warnings,
                    "unsupported_high_confidence_surface_ids": high_confidence_unsupported_surfaces,
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
