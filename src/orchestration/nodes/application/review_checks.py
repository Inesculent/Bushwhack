"""Check-first review nodes for the adversarial reviewer ablation."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence

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
from src.orchestration.context.task_evidence import code_slice_from_task_evidence
from src.orchestration.context.surface_ledger import (
    changed_files_from_diff,
    changed_file_sources_from_state,
    surface_by_id,
    surface_ledger_from_state,
)
from src.orchestration.nodes.application.review_check_executor_support import (
    file_contents_from_slot,
    missing_evidence_for_unanswered_check as _support_missing_evidence_for_unanswered_check,
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
from src.orchestration.nodes.application.critiquer import _is_length_finish_error
from src.orchestration.prompts.renderer import render_reviewer_prompt
from src.orchestration.routing.claim_digest import claim_digest_for_candidate

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
_TERMINAL_CHECK_DECISIONS = {"candidate", "no_finding", "budget_exhausted"}
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
_SOURCE_CONTEXT_EXTENSIONS = (
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".scala",
    ".sql",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".md",
)
_EXECUTOR_BATCH_SIZE = 3
_EXECUTOR_CODE_EVIDENCE_CHARS = 60000
_EXECUTOR_FOCUSED_EVIDENCE_CHARS = 40000
_EXECUTOR_COMPACT_CODE_EVIDENCE_CHARS = 4000
_EXECUTOR_COMPACT_FOCUSED_EVIDENCE_CHARS = 3000
_EXECUTOR_COMPACT_CONTEXT_CHARS = 8000
_EXECUTOR_MAX_MULTI_CHECK_PROMPT_CHARS = 44000
_EXECUTOR_COMPACT_RETRY_APPENDIX = (
    "\n\n## OUTPUT BUDGET (retry - required)\n"
    "Your previous response exceeded the length limit. This retry contains exactly one input check. "
    "Return exactly one ReviewCheckResult for that check_id. Keep reportable_reason, failure_mode, "
    "evidence_summary, recommendation, and each suppressing_evidence item under 300 characters. "
    "Do not include prose outside schema fields."
)
_GENERIC_QUERY_PHRASES = (
    "changed code behavior",
    "full file content",
    "confirm returns",
    "confirm explicit bounds",
    "confirm unexpected behavior",
)
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


def _text_contains_recursive(value: Any, needle: str) -> bool:
    if isinstance(value, Mapping):
        return any(_text_contains_recursive(item, needle) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_text_contains_recursive(item, needle) for item in value)
    return needle in str(value)


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
        lens_selection = compiler_support.compiler_lens_selection_diagnostics(
            task,
            slot,
            state=state,
            settings=resolved,
        )
        warnings: List[str] = []
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []
        summary = ""
        checks: List[ReviewCheck] = compiler_support.checks_from_contract_questions(
            state,
            task,
            settings=resolved,
        )
        check_origins = compiler_support.origins_for_checks(
            checks,
            "contract_question",
            "derived_from_behavioral_contract_question",
        )
        if checks:
            warnings.append(f"contract_question_checks_added:{len(checks)}")
        if use_llm:
            selected_model = model_key or resolved.reviewer_worker_model_key
            try:
                llm = Models.worker(
                    ReviewCheckCompilerOutput,
                    model_key=selected_model,
                    max_completion_tokens=resolved.reviewer_critiquer_max_completion_tokens,
                )
                prompt = compiler_support.render_compiler_prompt(
                    state,
                    task,
                    slot,
                    settings=resolved,
                )
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
                checks = compiler_support.enrich_checks_with_completeness_contracts(
                    compiler_support.dedupe_checks([*llm_checks, *checks]),
                    slot=slot,
                )
                checks = compiler_support.prioritize_compiled_checks(checks)
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

        checks = compiler_support.enrich_checks_with_completeness_contracts(checks, slot=slot)
        checks, coverage, check_origins = compiler_support.cap_compiled_checks(
            state=state,
            task=task,
            checks=checks,
            check_origins=check_origins,
        )
        checks = compiler_support.normalize_compiled_checks(state, task, checks)
        warnings.extend(coverage.get("warnings", []))
        lens_counts = compiler_support.checks_per_selected_lens(
            checks,
            lens_selection.get("selected_keys", []) if isinstance(lens_selection, dict) else [],
        )

        metadata = _set_task_review_checks_meta(
            state,
            task.id,
            {
                "compiler_summary": summary,
                "compiled_checks": [check.model_dump(mode="json") for check in checks],
                "compiled_check_origins": check_origins,
                "compiled_count": len(checks),
                "compiler_coverage": coverage,
                "compiler_warnings": warnings,
                "contract_lens_selection": lens_selection,
                "checks_per_selected_lens": lens_counts,
            },
        )
        if _trace_enabled(state):
            trace_logger.info(
                "TRACE %s run_id=%s task_id=%s checks=%s lens_keys=%s",
                node_name,
                state.get("run_id", "unknown"),
                task.id,
                len(checks),
                lens_selection.get("selected_keys", []),
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


def _check_file_scope_reason(
    check: ReviewCheck,
    *,
    state: GraphState | None = None,
    task: ReviewTask | None = None,
) -> str:
    """Name the scope gate a check's file fails, or return "" when the file is in scope."""
    if state is None and task is None:
        return ""
    file_path = check.file_path.replace("\\", "/")
    task_files = {path.replace("\\", "/") for path in (task.target_files if task else [])}
    if task_files and file_path not in task_files:
        return "file_not_in_task_targets"
    changed_files = (
        {
            path
            for paths in changed_file_sources_from_state(state or {}).values()
            for path in paths
        }
        if state is not None
        else set()
    ) or set(changed_files_from_diff(str((state or {}).get("git_diff") or "")))
    if changed_files and file_path not in changed_files:
        return "file_not_in_changed_code"
    return ""


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
    anchor = check.changed_code_anchor.strip().lower()
    if not anchor:
        return False
    if file_path.lower() in anchor or anchor in file_path.lower():
        return True

    ledger = _explicit_surface_ledger_from_state(state) if state is not None else []
    if ledger and check.surface_ids:
        by_id = surface_by_id(ledger)
        anchor_tokens = set(_meaningful_tokens(anchor))
        for surface_id in check.surface_ids:
            surface = by_id.get(surface_id)
            if surface is None:
                continue
            if surface.file_path != file_path or surface.line_start is None:
                continue
            surface_name = surface.name.strip().lower()
            if anchor == surface_name or anchor_tokens.intersection(_meaningful_tokens(surface_name)):
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
    scope_reason = ""
    if not check.file_path.strip():
        reasons.append("missing_file_path")
    else:
        scope_reason = _check_file_scope_reason(check, state=state, task=task)
        if scope_reason:
            reasons.append(scope_reason)
    if not check.changed_code_anchor.strip():
        reasons.append("missing_changed_code_anchor")
    elif not scope_reason and not _anchor_matches_changed_surface(check, state=state, task=task, slot=slot):
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


def _check_specificity_rank(check: ReviewCheck) -> tuple[int, int, int, int]:
    """Prefer the check most able to own a promotable result for a duplicated id."""
    return (
        0 if check.audit_only else 1,
        1 if check.owned_contract_scope.strip() else 0,
        len([sid for sid in check.surface_ids if str(sid).strip()]),
        len(check.behavioral_question or ""),
    )


def _checks_by_id_preferring_promotable(checks: Iterable[ReviewCheck]) -> Dict[str, ReviewCheck]:
    by_id: Dict[str, ReviewCheck] = {}
    for check in checks:
        existing = by_id.get(check.check_id)
        if existing is None or _check_specificity_rank(check) > _check_specificity_rank(existing):
            by_id[check.check_id] = check
    return by_id


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


def _normalize_source_context_path(path: str) -> str:
    normalized = path.strip().strip("`'\".,;)(").replace("\\", "/").lstrip("/")
    normalized = re.sub(r":\d+(?::\d+)?$", "", normalized)
    if normalized.startswith(("a/", "b/")):
        normalized = normalized[2:]
    return normalized


def _looks_like_source_context_path(path: str) -> bool:
    normalized = _normalize_source_context_path(path)
    if not normalized or any(ch.isspace() for ch in normalized) or "://" in normalized:
        return False
    lower = normalized.lower()
    return any(lower.endswith(ext) for ext in _SOURCE_CONTEXT_EXTENSIONS)


def _source_context_paths_from_text(text: str) -> List[str]:
    if not text:
        return []
    patterns = (
        r"file:([A-Za-z0-9_.\-/\\]+?\.[A-Za-z0-9_]+(?::\d+(?::\d+)?)?)",
        r"`([^`]+?\.[A-Za-z0-9_]+(?::\d+(?::\d+)?)?)`",
        r"\(([A-Za-z0-9_.\-/\\]+?\.[A-Za-z0-9_]+(?::\d+(?::\d+)?)?)\)",
        r"\b([A-Za-z0-9_.\-/\\]+/[A-Za-z0-9_.\-/\\]+?\.[A-Za-z0-9_]+(?::\d+(?::\d+)?)?)\b",
    )
    paths: List[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            path = _normalize_source_context_path(match.group(1))
            if path and path not in seen and _looks_like_source_context_path(path):
                seen.add(path)
                paths.append(path)
    return paths


def _contract_context_paths_for_check(
    *,
    check: ReviewCheck,
    missing_evidence: Sequence[str] = (),
    max_paths: int = 4,
) -> List[str]:
    """Source paths the check or the executor explicitly named as needed contract evidence.

    Only text written for this check is scanned: its own evidence requirements,
    its allowed retrieval list, and the executor's latest missing-evidence list.
    Mental-model and repository-KB text is not mined for paths; doing so pulled
    whole unrelated files into focused context.
    """
    paths: List[str] = []
    seen: set[str] = set()
    check_path = _normalize_source_context_path(check.file_path)
    evidence_paths = {_normalize_source_context_path(str(path)) for path in check.evidence_paths}
    for text in (*check.required_evidence, *check.allowed_retrieval, *missing_evidence):
        for path in _source_context_paths_from_text(str(text or "")):
            normalized = _normalize_source_context_path(path)
            if (
                not normalized
                or normalized == check_path
                or normalized in evidence_paths
                or normalized in seen
                or not _looks_like_source_context_path(normalized)
            ):
                continue
            seen.add(normalized)
            paths.append(normalized)
            if len(paths) >= max_paths:
                return paths
    return paths


def _unique_source_paths(paths: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for path in paths:
        normalized = _normalize_source_context_path(str(path or ""))
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _planned_context_request_for_check(
    *,
    state: GraphState,
    task: ReviewTask,
    check: ReviewCheck,
    slot: Mapping[str, Any],
    latest: ReviewCheckResult | None = None,
    existing_ids: set[str] | None = None,
    existing_signatures: set[tuple[Any, ...]] | None = None,
    scope: frozenset[str] | None = None,
) -> FocusedContextRequest | None:
    if latest is not None:
        if not _allows_focused_retrieval(check):
            return None
        missing_evidence = [item for item in latest.missing_evidence if str(item).strip()]
    else:
        missing_evidence = _missing_evidence_for_check(
            state=state,
            task_id=task.id,
            check=check,
            slot=slot,
        )
    if not missing_evidence:
        return None
    if not _check_budget_remaining(state, check):
        return None

    if existing_ids is None:
        existing_ids = {
            getattr(req, "request_id", None) if isinstance(req, FocusedContextRequest) else req.get("request_id")
            for req in (state.get("focused_context_requests", []) or [])
            if isinstance(req, (FocusedContextRequest, dict))
        }
    if existing_signatures is None:
        existing_signatures = {
            _focused_request_signature(req)
            for req in (state.get("focused_context_requests", []) or [])
            if isinstance(req, (FocusedContextRequest, dict))
        }
    if scope is None:
        scope = allowed_review_paths(state, task_target_files=task.target_files)

    request_id = _next_request_id_for_check(state, check)
    if request_id in existing_ids:
        return None
    file_read_mode = "full" if _should_retry_full_file_for_check(state, check, latest) else "slice"
    evidence_paths = [
        path.strip().replace("\\", "/")
        for path in (check.evidence_paths or [check.file_path])
        if path and path.strip()
    ]
    contract_context_paths = _contract_context_paths_for_check(
        check=check,
        missing_evidence=missing_evidence,
    )
    request_file_paths = _unique_source_paths(
        [
            *evidence_paths,
            *contract_context_paths,
            *(task.target_files[:1] if not evidence_paths else []),
        ]
    )
    if contract_context_paths:
        scope = frozenset({*scope, *contract_context_paths})
    contract_reason = (
        f"; contract_context_paths={', '.join(contract_context_paths[:4])}"
        if contract_context_paths
        else ""
    )
    req = FocusedContextRequest(
        request_id=request_id,
        candidate_id=check.check_id,
        requested_by_specialty=task.specialty,
        file_read_mode=file_read_mode,
        file_paths=request_file_paths,
        symbol_queries=[check.changed_code_anchor] if check.changed_code_anchor else [],
        text_queries=[
            _query_for_requirement(req_text, check)
            for req_text in missing_evidence[:3]
            if str(req_text).strip()
        ],
        reason=(
            f"Gather missing evidence for review check {check.check_id} "
            f"at {check.file_path}:{check.line_start}-{check.line_end}; "
            f"evidence_paths={evidence_paths}; "
            f"anchor={check.changed_code_anchor}; invariant={check.affected_invariant}; "
            f"missing={', '.join(missing_evidence[:2])}{contract_reason}"
        ),
    )
    clamped = clamp_focused_context_request(
        req,
        scope,
        fallback_path=check.file_path or (task.target_files[0] if task.target_files else None),
    )
    if _focused_request_signature(clamped) in existing_signatures:
        return None
    return clamped


def _terminalize_unretryable_results(
    *,
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    checks: Sequence[ReviewCheck],
    results: Sequence[ReviewCheckResult],
) -> tuple[List[ReviewCheckResult], List[str]]:
    by_check = {check.check_id: check for check in checks}
    terminalized: List[ReviewCheckResult] = []
    warnings: List[str] = []
    for result in results:
        check = by_check.get(result.check_id)
        if (
            check is None
            or result.decision != "unsupported"
            or not result.missing_evidence
            or _planned_context_request_for_check(
                state=state,
                task=task,
                check=check,
                slot=slot,
                latest=result,
            )
            is not None
        ):
            terminalized.append(result)
            continue
        warnings.append(f"executor_no_retry_path_budget_exhausted:{result.check_id}")
        terminalized.append(
            result.model_copy(
                update={
                    "decision": "budget_exhausted",
                    "warnings": list(result.warnings)
                    + ["review_check_budget_exhausted", "review_check_no_retry_path"],
                }
            )
        )
    return terminalized, warnings


def _executable_checks_for_task(state: GraphState, task_id: str) -> List[ReviewCheck]:
    latest = _latest_result_by_check(state, task_id)
    task = _task_from_state(state)
    slot = _pipeline_slot(state, task_id)
    checks: List[ReviewCheck] = []
    for check in _checks_by_id_preferring_promotable(_checks_for_task(state, task_id)).values():
        result = latest.get(check.check_id)
        if result is None:
            checks.append(check)
            continue
        if result.decision in _TERMINAL_CHECK_DECISIONS:
            continue
        if (
            task is not None
            and result.missing_evidence
            and _planned_context_request_for_check(
                state=state,
                task=task,
                check=check,
                slot=slot,
                latest=result,
            )
            is not None
        ):
            checks.append(check)
    return checks


def should_continue_review_check_loop(state: GraphState) -> bool:
    task = _task_from_state(state)
    if task is None:
        return False
    checks = _checks_by_id_preferring_promotable(_checks_for_task(state, task.id))
    latest = _latest_result_by_check(state, task.id)
    for check_id, result in latest.items():
        check = checks.get(check_id)
        if check is None:
            continue
        if result.decision in _TERMINAL_CHECK_DECISIONS:
            continue
        if not result.missing_evidence:
            continue
        slot = _pipeline_slot(state, task.id)
        if _planned_context_request_for_check(
            state=state,
            task=task,
            check=check,
            slot=slot,
            latest=result,
        ) is not None:
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
            clamped = _planned_context_request_for_check(
                state=state,
                task=task,
                check=check,
                slot=slot,
                latest=latest,
                existing_ids=existing_ids,
                existing_signatures=existing_signatures,
                scope=scope,
            )
            if clamped is None:
                continue
            signature = _focused_request_signature(clamped)
            if signature in existing_signatures:
                continue
            existing_ids.add(clamped.request_id)
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
    focused = focused_snippets_for_candidate(state, check_id, max_chars=30000)
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


def _focused_context_degraded_for_check(state: GraphState, check: ReviewCheck) -> List[str]:
    metadata = state.get("metadata", {}) or {}
    fc = metadata.get("focused_context", {}) if isinstance(metadata, Mapping) else {}
    diagnostics = fc.get("diagnostics", []) if isinstance(fc, Mapping) else []
    reasons: List[str] = []
    for row in diagnostics if isinstance(diagnostics, list) else []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("candidate_id") or "") != check.check_id:
            continue
        outcomes = {str(item) for item in row.get("outcomes", []) or []}
        reason = str(row.get("reason") or "")
        if reason:
            outcomes.add(reason)
        if "no_hits" in outcomes:
            reasons.append("focused_context_no_hits")
        if "tool_unavailable" in outcomes:
            reasons.append("focused_context_tool_unavailable")
        if "path_mismatch" in outcomes:
            reasons.append("focused_context_path_mismatch")
    return list(dict.fromkeys(reasons))


def _seen_claim_digests_for_task(state: GraphState, task_id: str) -> List[Dict[str, str]]:
    seen: Dict[str, str] = {}
    for raw in state.get("candidate_findings", []) or []:
        try:
            candidate = raw if isinstance(raw, CandidateFinding) else CandidateFinding.model_validate(raw)
        except Exception:
            continue
        if candidate.patch_task_id == task_id:
            continue
        digest = claim_digest_for_candidate(candidate)
        if not digest:
            continue
        seen.setdefault(digest, candidate.candidate_id)
    return [{"claim_digest": digest, "candidate_id": candidate_id} for digest, candidate_id in seen.items()][:24]


def _source_evidence_for_check(
    check: ReviewCheck,
    slot: Mapping[str, Any],
    *,
    max_chars: int,
) -> str:
    """Render check-addressable source already collected from the repository."""
    task_evidence = (
        slot.get("task_evidence")
        if isinstance(slot.get("task_evidence"), dict)
        else {}
    )
    files = (
        task_evidence.get("file_contents")
        if isinstance(task_evidence.get("file_contents"), dict)
        else {}
    )
    paths = [
        path.strip().replace("\\", "/")
        for path in (check.evidence_paths or [check.file_path])
        if path and path.strip()
    ]
    if not paths or not files:
        return ""

    per_path = max(1500, max_chars // len(paths))
    chunks: List[str] = []
    for path in paths:
        body = str(files.get(path) or files.get(path.replace("/", "\\")) or "")
        if not body.strip():
            continue
        if path == check.file_path.strip().replace("\\", "/"):
            excerpt = code_slice_from_task_evidence(
                task_evidence,
                path,
                check.line_start,
                check.line_end,
                padding=80,
            )
            if not excerpt.strip():
                excerpt = body
        else:
            excerpt = body
        chunks.append(f"--- {path} ---\n{excerpt[:per_path]}")
    return "\n\n".join(chunks)[:max_chars]


def _contract_packet_for_check(check: ReviewCheck) -> Dict[str, Any]:
    """Compact, non-judgmental check packet for the executor LLM."""
    return {
        "check_id": check.check_id,
        "file_path": check.file_path,
        "line_start": check.line_start,
        "line_end": check.line_end,
        "surface_ids": list(check.surface_ids),
        "lens": check.lens,
        "changed_code_anchor": check.changed_code_anchor,
        "owned_contract_scope": check.owned_contract_scope,
        "expected_behavior": check.expected_behavior,
        "contract_source": (
            check.contract_source.model_dump(mode="json") if check.contract_source is not None else None
        ),
        "behavioral_question": check.behavioral_question,
        "affected_invariant": check.affected_invariant,
        "required_evidence": list(check.required_evidence[:5]),
        "suppress_criteria": list(check.suppress_criteria[:4]),
        "report_criteria": list(check.report_criteria[:4]),
        "allowed_retrieval": list(check.allowed_retrieval[:4]),
        "evidence_paths": list(check.evidence_paths[:5]),
        "budget": check.budget,
    }


def _executor_context_presence(
    state: GraphState,
    checks: Sequence[ReviewCheck],
    slot: Mapping[str, Any],
) -> Dict[str, Any]:
    focused_with_evidence = [
        check.check_id for check in checks if _focused_context_for_check(state, check.check_id).strip()
    ]
    focused_set = set(focused_with_evidence)
    focused_missing = [
        check.check_id
        for check in checks
        if "focused_context" in check.allowed_retrieval and check.check_id not in focused_set
    ]
    return {
        "direct_context": bool(str(slot.get("direct_context") or "").strip()),
        "task_evidence_file_count": len(file_contents_from_slot(slot) or {}),
        "mental_model_excerpt": bool(str(slot.get("mental_model_excerpt") or "").strip()),
        "review_kb_excerpt": bool(str(slot.get("review_kb_excerpt") or "").strip()),
        "focused_context_check_ids": focused_with_evidence,
        "focused_context_check_count": len(focused_with_evidence),
        "focused_context_missing_check_ids": focused_missing,
        "focused_context_missing_check_count": len(focused_missing),
    }


def _render_executor_prompt(
    state: GraphState,
    task: ReviewTask,
    checks: List[ReviewCheck],
    slot: Mapping[str, Any],
    *,
    compact_retry: bool = False,
) -> str:
    focused = {
        check.check_id: _focused_context_for_check(state, check.check_id)
        for check in checks
    }
    code_limit = _EXECUTOR_COMPACT_CODE_EVIDENCE_CHARS if compact_retry else _EXECUTOR_CODE_EVIDENCE_CHARS
    focused_limit = (
        _EXECUTOR_COMPACT_FOCUSED_EVIDENCE_CHARS if compact_retry else _EXECUTOR_FOCUSED_EVIDENCE_CHARS
    )
    context_limit = _EXECUTOR_COMPACT_CONTEXT_CHARS
    source_limit = 6000 if compact_retry else (30000 if len(checks) == 1 else 9000)
    mental_model_excerpt = str(slot.get("mental_model_excerpt") or "")
    review_kb_excerpt = str(slot.get("review_kb_excerpt") or "")
    mental_model_excerpt = mental_model_excerpt[:context_limit]
    review_kb_excerpt = review_kb_excerpt[:context_limit]
    sections = {
        "Assigned Task": (
            f"Task ID: {task.id}\n"
            f"Title: {task.title}\n"
            f"Description: {task.description[:1000]}\n"
            f"Target files: {task.target_files}"
        ),
        "Check Contract Packets": _json_for_prompt(
            [_contract_packet_for_check(check) for check in checks],
            max_chars=20000,
        ),
        "Already Seen Claim Digests": _json_for_prompt(
            _seen_claim_digests_for_task(state, task.id),
            max_chars=3000,
        ),
        "Repository Source Evidence By Check": _json_for_prompt(
            {
                check.check_id: _source_evidence_for_check(
                    check,
                    slot,
                    max_chars=source_limit,
                )
                for check in checks
            },
            max_chars=36000,
        ),
        "Repository Code Evidence": str(slot.get("direct_context") or "")[:code_limit],
        "Focused Evidence By Check": _json_for_prompt(focused, max_chars=focused_limit),
        "Mental Model Excerpt": mental_model_excerpt,
        "Review KB Context": review_kb_excerpt,
    }
    prompt = render_reviewer_prompt("review_check_executor.md", sections)
    if compact_retry:
        prompt = f"{prompt}{_EXECUTOR_COMPACT_RETRY_APPENDIX}"
    return prompt


def _missing_evidence_for_unanswered_check(check: ReviewCheck) -> List[str]:
    return _support_missing_evidence_for_unanswered_check(check, _evidence_requirements_for_check)


def _check_batches(checks: Sequence[ReviewCheck]) -> List[List[ReviewCheck]]:
    """Batch local checks; isolate cross-file checks so their evidence is not truncated."""
    batches: List[List[ReviewCheck]] = []
    pending: List[ReviewCheck] = []
    for check in checks:
        evidence_paths = {
            path.strip().replace("\\", "/")
            for path in check.evidence_paths
            if path and path.strip()
        }
        if len(evidence_paths) > 1:
            if pending:
                batches.append(pending)
                pending = []
            batches.append([check])
            continue
        pending.append(check)
        if len(pending) >= _EXECUTOR_BATCH_SIZE:
            batches.append(pending)
            pending = []
    if pending:
        batches.append(pending)
    return batches


def _executor_prompt_batches(
    state: GraphState,
    task: ReviewTask,
    checks: Sequence[ReviewCheck],
    slot: Mapping[str, Any],
) -> tuple[List[List[ReviewCheck]], List[Dict[str, Any]]]:
    batches: List[List[ReviewCheck]] = []
    split_events: List[Dict[str, Any]] = []
    for batch_index, batch in enumerate(_check_batches(checks), start=1):
        if len(batch) > 1:
            prompt = _render_executor_prompt(state, task, batch, slot)
            if len(prompt) > _EXECUTOR_MAX_MULTI_CHECK_PROMPT_CHARS:
                split_events.append(
                    {
                        "batch_index": batch_index,
                        "check_ids": [check.check_id for check in batch],
                        "prompt_chars": len(prompt),
                    }
                )
                batches.extend([[check] for check in batch])
                continue
        batches.append(batch)
    return batches, split_events


def _executor_result_rank(result: ReviewCheckResult) -> int:
    if result.decision == "candidate" and result.candidate is not None:
        return 50
    if result.decision == "no_finding":
        return 30
    if result.decision == "unsupported":
        return 20
    if result.decision == "budget_exhausted":
        return 10
    return 0


def _canonicalize_executor_results(
    results: Sequence[ReviewCheckResult],
) -> tuple[List[ReviewCheckResult], List[str]]:
    best: Dict[str, tuple[int, int, ReviewCheckResult]] = {}
    order: List[str] = []
    counts: Dict[str, int] = {}
    for index, result in enumerate(results):
        check_id = result.check_id
        counts[check_id] = counts.get(check_id, 0) + 1
        if check_id not in best:
            order.append(check_id)
            best[check_id] = (_executor_result_rank(result), index, result)
            continue
        rank = _executor_result_rank(result)
        old_rank, old_index, _old = best[check_id]
        if (rank, index) >= (old_rank, old_index):
            best[check_id] = (rank, index, result)
    duplicates = [check_id for check_id, count in counts.items() if count > 1]
    return [best[check_id][2] for check_id in order], duplicates


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
        duplicate_result_check_ids: List[str] = []
        result_count_before_canonicalization = 0
        executor_retry_count = 0
        executor_retry_success_count = 0
        length_limit_batch_failures: List[Dict[str, Any]] = []
        length_limit_retry_count = 0
        length_limit_retry_success_count = 0
        length_limit_retry_failed_check_ids: List[str] = []
        executor_batches, executor_oversized_batch_splits = _executor_prompt_batches(
            state,
            task,
            checks,
            slot,
        )
        warnings.extend(
            f"executor_oversized_batch_split:{event['batch_index']}"
            for event in executor_oversized_batch_splits
        )

        if use_llm:
            selected_model = model_key or resolved.reviewer_worker_model_key
            changed_task_files = set(_changed_task_files(state, task))

            def _run_compact_length_retry(
                *,
                check: ReviewCheck,
                batch_index: int,
            ) -> tuple[List[ReviewCheckResult], bool]:
                nonlocal llm_tokens, llm_trace, result_count_before_canonicalization
                nonlocal length_limit_retry_count, length_limit_retry_success_count
                nonlocal warnings
                length_limit_retry_count += 1
                try:
                    retry_llm = Models.worker(
                        ReviewCheckExecutorOutput,
                        model_key=selected_model,
                        max_completion_tokens=resolved.reviewer_critiquer_max_completion_tokens,
                    )
                    retry_prompt = _render_executor_prompt(
                        state,
                        task,
                        [check],
                        slot,
                        compact_retry=True,
                    )
                    retry_traced = trace_llm_call(
                        retry_llm,
                        retry_prompt,
                        state=state,
                        node_name=node_name,
                        model_key=selected_model,
                        schema_name="ReviewCheckExecutorOutput",
                        request_label="compact_length_retry",
                        input_summary={
                            "task_id": task.id,
                            "batch": batch_index,
                            "length_limit_retry_check_id": check.check_id,
                        },
                    )
                    retry_response = parse_structured_output(
                        retry_traced.result,
                        ReviewCheckExecutorOutput,
                    )
                    llm_tokens += retry_traced.tokens
                    llm_trace.extend(retry_traced.trace_records)
                    retry_results, retry_warnings = _support_normalize_executor_results(
                        state=state,
                        task=task,
                        slot=slot,
                        checks=[check],
                        results=retry_response.results,
                        git_diff=state.get("git_diff", "") or "",
                        check_budget_remaining=_check_budget_remaining,
                        evidence_requirements_for_check=_evidence_requirements_for_check,
                        compiled_check_is_source_local=lambda item: _compiled_check_is_source_local(
                            item,
                            None,
                            slot,
                            changed_task_files,
                        ),
                        include_missing_results=False,
                    )
                    retry_present = any(result.check_id == check.check_id for result in retry_results)
                    if not retry_present:
                        raise ValueError("compact length retry returned no result for requested check")
                    result_count_before_canonicalization += len(retry_results)
                    retry_results, retry_duplicate_ids = _canonicalize_executor_results(retry_results)
                    duplicate_result_check_ids.extend(retry_duplicate_ids)
                    warnings.extend(retry_response.warnings)
                    warnings.extend(retry_warnings)
                    length_limit_retry_success_count += 1
                    return retry_results, True
                except Exception as retry_exc:  # noqa: BLE001
                    llm_trace.extend(trace_from_exception(retry_exc))
                    length_limit_retry_failed_check_ids.append(check.check_id)
                    warnings.append(f"executor_length_limit_retry_failed:{check.check_id}")
                    result_count_before_canonicalization += 1
                    return [
                        ReviewCheckResult(
                            check_id=check.check_id,
                            patch_task_id=task.id,
                            decision="unsupported",
                            missing_evidence=_missing_evidence_for_unanswered_check(check),
                            warnings=["executor_length_limit_retry_failed"],
                        )
                    ], False

            for batch_index, batch in enumerate(executor_batches, start=1):
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
                        include_missing_results=True,
                        missing_result_warning="executor_omitted_result_recorded_unsupported",
                    )
                    batch_check_ids = {check.check_id for check in batch}
                    response_ids = {
                        result.check_id
                        for result in response.results
                        if result.check_id in batch_check_ids
                    }
                    missing_checks = [check for check in batch if check.check_id not in response_ids]
                    if missing_checks:
                        missing_result_check_ids.extend(check.check_id for check in missing_checks)
                        warnings.extend(
                            f"executor_omitted_result_recorded_unsupported:{check.check_id}"
                            for check in missing_checks
                        )
                    result_count_before_canonicalization += len(batch_results)
                    batch_results, batch_duplicate_ids = _canonicalize_executor_results(batch_results)
                    duplicate_result_check_ids.extend(batch_duplicate_ids)
                    results.extend(batch_results)
                    warnings.extend(response.warnings)
                    warnings.extend(norm_warnings)
                except Exception as exc:  # noqa: BLE001
                    llm_trace.extend(trace_from_exception(exc))
                    if _is_length_finish_error(exc):
                        length_limit_batch_failures.append(
                            {
                                "batch_index": batch_index,
                                "check_ids": [check.check_id for check in batch],
                            }
                        )
                        warnings.append(f"executor_length_limit_batch_retry:{batch_index}")
                        logger.warning(
                            "%s length-limit retry for task_id=%s batch=%s checks=%s",
                            node_name,
                            task.id,
                            batch_index,
                            [check.check_id for check in batch],
                        )
                        for check in batch:
                            compact_results, _compact_success = _run_compact_length_retry(
                                check=check,
                                batch_index=batch_index,
                            )
                            results.extend(compact_results)
                        continue
                    warnings.append(f"{node_name}_batch_failed:{batch_index}:{exc.__class__.__name__}: {exc}")
                    logger.warning("%s failed for task_id=%s batch=%s: %s", node_name, task.id, batch_index, exc)
                    results.extend(
                        ReviewCheckResult(
                            check_id=check.check_id,
                            patch_task_id=task.id,
                            decision="unsupported",
                            missing_evidence=_missing_evidence_for_unanswered_check(check),
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
            result_count_before_canonicalization = len(results)
        else:
            if result_count_before_canonicalization < len(results):
                result_count_before_canonicalization = len(results)
            results, final_duplicate_ids = _canonicalize_executor_results(results)
            duplicate_result_check_ids.extend(final_duplicate_ids)
        results, terminal_warnings = _terminalize_unretryable_results(
            state=state,
            task=task,
            slot=slot,
            checks=checks,
            results=results,
        )
        warnings.extend(terminal_warnings)
        missing_candidate_payload_check_ids = [
            warning.split(":", 1)[1]
            for warning in warnings
            if warning.startswith("executor_candidate_missing_payload:")
        ]
        missing_contract_proof_events = []
        for warning in warnings:
            if not warning.startswith("executor_candidate_missing_contract_proof:"):
                continue
            _prefix, rest = warning.split(":", 1)
            check_id, raw_fields = (rest.rsplit(":", 1) + [""])[:2]
            missing_contract_proof_events.append(
                {
                    "check_id": check_id,
                    "fields": [field for field in raw_fields.split(",") if field],
                }
            )
        candidate_contract_unbacked_events = []
        for warning in warnings:
            if not warning.startswith("executor_candidate_contract_unbacked:"):
                continue
            _prefix, rest = warning.split(":", 1)
            check_id, reason = (rest.rsplit(":", 1) + [""])[:2]
            candidate_contract_unbacked_events.append({"check_id": check_id, "reason": reason})
        contract_status_counts = {
            status: sum(1 for result in results if result.contract_status == status)
            for status in sorted({result.contract_status for result in results})
        }
        executor_claim_digests = {
            result.candidate.candidate_id: result.candidate.claim_digest
            for result in results
            if result.candidate is not None and result.candidate.claim_digest.strip()
        }

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
                "executor_claim_digests": executor_claim_digests,
                "executor_claim_digest_count": len(executor_claim_digests),
                "executor_warnings": warnings,
                "executor_batch_size": _EXECUTOR_BATCH_SIZE,
                "executor_batch_count": len(executor_batches),
                "executor_context_presence": _executor_context_presence(state, checks, slot),
                "executor_max_multi_check_prompt_chars": _EXECUTOR_MAX_MULTI_CHECK_PROMPT_CHARS,
                "executor_oversized_batch_splits": executor_oversized_batch_splits,
                "executor_missing_result_check_ids": list(dict.fromkeys(missing_result_check_ids)),
                "executor_duplicate_result_check_ids": list(dict.fromkeys(duplicate_result_check_ids)),
                "executor_result_count_before_canonicalization": result_count_before_canonicalization,
                "executor_retry_count": executor_retry_count,
                "executor_retry_success_count": executor_retry_success_count,
                "executor_candidate_missing_payload_check_ids": list(
                    dict.fromkeys(missing_candidate_payload_check_ids)
                ),
                "executor_candidate_missing_contract_proof": missing_contract_proof_events,
                "executor_candidate_missing_contract_proof_count": len(missing_contract_proof_events),
                "executor_contract_status_counts": contract_status_counts,
                "executor_candidate_contract_unbacked": candidate_contract_unbacked_events,
                "executor_candidate_contract_unbacked_count": len(candidate_contract_unbacked_events),
                "executor_length_limit_batch_failures": length_limit_batch_failures,
                "executor_length_limit_retry_count": length_limit_retry_count,
                "executor_length_limit_retry_success_count": length_limit_retry_success_count,
                "executor_length_limit_retry_failed_check_ids": list(
                    dict.fromkeys(length_limit_retry_failed_check_ids)
                ),
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
            candidate.evidence_for_contract,
            candidate.counterexample,
            candidate.rejection_check,
            result.reportable_reason,
        ]
    ).lower()
    return any(marker in blob for marker in _SPECULATIVE_MARKERS)


def _expected_behavior_is_generic_best_practice(candidate: CandidateFinding) -> bool:
    expected = candidate.expected_behavior.strip().lower()
    if not expected:
        return False
    advisory_markers = (
        "best practice",
        "nice to have",
        "consider adding",
        "could add",
        "should add",
        "recommended to add",
        "would be safer",
        "would improve",
    )
    if not any(marker in expected for marker in advisory_markers):
        return False
    contract_markers = (
        "preserve",
        "return",
        "must",
        "contract",
        "declared",
        "expected",
        "intended",
        "required",
        "guarantee",
    )
    return not any(marker in expected for marker in contract_markers)


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
            candidate.evidence_for_contract,
            candidate.counterexample,
            candidate.rejection_check,
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


def _audit_only_candidate_is_promotable(candidate: CandidateFinding, result: ReviewCheckResult) -> bool:
    blob = " ".join(
        [
            candidate.content,
            candidate.expected_behavior,
            candidate.failure_mode,
            candidate.evidence_summary,
            result.reportable_reason,
            candidate.suspected_category,
        ]
    ).lower()
    low_signal_markers = (
        "style",
        "readability",
        "comment",
        "typo",
        "formatting",
        "lint",
        "unused import",
        "unused variable",
        "dead code",
        "cleanup",
    )
    if any(marker in blob for marker in low_signal_markers):
        return False
    if (candidate.claim_type or "").strip() and candidate.claim_type != "defect":
        return False
    return True


def _candidate_passes_gate(
    candidate: CandidateFinding,
    result: ReviewCheckResult,
    check: ReviewCheck,
    state: GraphState,
) -> tuple[bool, str]:
    # audit_only is a compile-time softness signal, not a post-execution shredder:
    # concrete candidates still face the same evidence gates below.
    if candidate.file_path.replace("\\", "/") != check.file_path.replace("\\", "/"):
        return False, "candidate_anchor_file_mismatch"
    if candidate.line_end < candidate.line_start or candidate.line_start < 1:
        return False, "invalid_candidate_line_range"
    candidate_span = int(candidate.line_end or candidate.line_start) - int(candidate.line_start or 1) + 1
    check_span = int(check.line_end or check.line_start) - int(check.line_start or 1) + 1
    whole_function_claim = (candidate.root_operation in {"resource_use", "exception_scope"} and candidate_span <= 140)
    has_narrow_evidence_ref = any(
        ref.strip().replace("\\", "/").startswith(candidate.file_path.replace("\\", "/") + ":")
        and re.search(r":\d+", ref)
        for ref in result.evidence_refs
    )
    if candidate_span > 120 and not (whole_function_claim or has_narrow_evidence_ref or check_span > 120):
        return False, "candidate_anchor_too_broad"
    if not check.affected_invariant.strip():
        return False, "missing_check_invariant"
    if not candidate.expected_behavior.strip():
        return False, "missing_expected_behavior"
    if _expected_behavior_is_generic_best_practice(candidate):
        return False, "generic_expected_behavior_not_contract"
    if not candidate.evidence_for_contract.strip():
        return False, "missing_contract_evidence"
    if not candidate.counterexample.strip():
        return False, "missing_counterexample"
    if not candidate.rejection_check.strip():
        return False, "missing_rejection_check"
    proof_blob = " ".join(
        [
            candidate.evidence_for_contract,
            candidate.counterexample,
            candidate.rejection_check,
        ]
    ).lower()
    if any(
        marker in proof_blob
        for marker in (
            "may be intentional",
            "might be intentional",
            "could be intentional",
            "may not align with user expectations",
            "clarify whether",
        )
    ):
        return False, "weak_contract_proof"
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
    focused_degradation = _focused_context_degraded_for_check(state, check)
    if focused_degradation:
        return False, focused_degradation[0]
    if _candidate_speculative(candidate, result):
        return False, "speculative_or_uncertain_claim"
    if check.audit_only and not _audit_only_candidate_is_promotable(candidate, result):
        return False, "audit_only_check_not_promotable"
    if not (candidate.recommendation or "").strip():
        return False, "missing_recommendation"
    if check.audit_only:
        return True, "evidence_gate_passed_audit_only"
    return True, "evidence_gate_passed"


def make_review_check_evidence_gate_node():
    node_name = "review_check_evidence_gate"

    def review_check_evidence_gate_node(state: GraphState) -> Dict[str, Any]:
        task = _task_from_state(state)
        if task is None:
            return {"node_history": [f"{node_name}:skipped"]}
        checks = _checks_by_id_preferring_promotable(_checks_for_task(state, task.id))
        promoted: List[CandidateFinding] = []
        promoted_ids: set[str] = set()
        lifecycle: Dict[str, Any] = {}
        gate_reason_counts: Dict[str, int] = {}
        malformed_candidate_result_check_ids: List[str] = []
        missing_contract_proof_candidate_ids: List[str] = []
        weak_contract_proof_candidate_ids: List[str] = []
        dropped = 0
        gate_evaluated_count = 0
        latest_results = list(_latest_result_by_check(state, task.id).values())
        for result in latest_results:
            check = checks.get(result.check_id)
            candidate = result.candidate
            if result.decision == "candidate" and candidate is None:
                reason = "candidate_payload_missing"
                malformed_candidate_result_check_ids.append(result.check_id)
                dropped += 1
                gate_evaluated_count += 1
                lifecycle[result.check_id] = {
                    "decision": "dropped",
                    "check_id": result.check_id,
                    "reason": reason,
                }
                gate_reason_counts[reason] = gate_reason_counts.get(reason, 0) + 1
                continue
            if result.decision != "candidate" or check is None:
                continue
            gate_evaluated_count += 1
            passed, reason = _candidate_passes_gate(candidate, result, check, state)
            if passed:
                if candidate.candidate_id not in promoted_ids:
                    promoted.append(candidate)
                    promoted_ids.add(candidate.candidate_id)
                lifecycle[candidate.candidate_id] = {
                    "decision": "passed",
                    "check_id": result.check_id,
                    "reason": reason,
                }
            else:
                dropped += 1
                if reason in {
                    "missing_expected_behavior",
                    "missing_contract_evidence",
                    "missing_counterexample",
                    "missing_rejection_check",
                }:
                    missing_contract_proof_candidate_ids.append(candidate.candidate_id)
                elif reason == "weak_contract_proof":
                    weak_contract_proof_candidate_ids.append(candidate.candidate_id)
                existing_lifecycle = lifecycle.get(candidate.candidate_id)
                if not (
                    isinstance(existing_lifecycle, dict)
                    and existing_lifecycle.get("decision") == "passed"
                ):
                    lifecycle[candidate.candidate_id] = {
                        "decision": "dropped",
                        "check_id": result.check_id,
                        "reason": reason,
                    }
            gate_reason_counts[reason] = gate_reason_counts.get(reason, 0) + 1

        candidate_decision_count = sum(1 for result in latest_results if result.decision == "candidate")
        gate_expected_count = candidate_decision_count
        health_warnings: List[str] = []
        if checks and candidate_decision_count and not gate_evaluated_count:
            health_warnings.append("evidence_gate_not_exercised")
        focused_health = []
        metadata = state.get("metadata", {}) or {}
        fc = metadata.get("focused_context", {}) if isinstance(metadata, Mapping) else {}
        diagnostics = fc.get("diagnostics", []) if isinstance(fc, Mapping) else []
        for row in diagnostics if isinstance(diagnostics, list) else []:
            if not isinstance(row, Mapping):
                continue
            candidate_id = str(row.get("candidate_id") or "")
            if candidate_id not in checks:
                continue
            outcomes = {str(item) for item in row.get("outcomes", []) or []}
            for outcome, warning in (
                ("no_hits", "focused_context_no_hits"),
                ("tool_unavailable", "focused_context_tool_unavailable"),
                ("path_mismatch", "focused_context_path_mismatch"),
            ):
                if outcome in outcomes:
                    focused_health.append(warning)
        health_warnings.extend(sorted(set(focused_health)))
        ledger = surface_ledger_from_state(state)
        by_id = surface_by_id(ledger)
        high_confidence_unsupported_surfaces = sorted(
            {
                sid
                for result in latest_results
                if result.decision in {"unsupported", "budget_exhausted"}
                for sid in (checks.get(result.check_id).surface_ids if checks.get(result.check_id) else [])
                if sid in by_id
                and by_id[sid].confidence >= 0.75
                and checks.get(result.check_id) is not None
                and not checks[result.check_id].audit_only
            }
        )
        if (
            high_confidence_unsupported_surfaces
            and not any(result.candidate is not None for result in latest_results)
        ):
            health_warnings.append("no_executor_candidates_for_high_confidence_non_audit_checks")

        metadata = _set_task_review_checks_meta(
            state,
            task.id,
            {
                "gate": {
                    "promoted_count": len(promoted),
                    "dropped_count": dropped,
                    "evaluated_count": gate_evaluated_count,
                    "candidate_decision_count": candidate_decision_count,
                    "gate_expected_count": gate_expected_count,
                    "gate_evaluated_count": gate_evaluated_count,
                    "reason_counts": gate_reason_counts,
                    "candidate_lifecycle": lifecycle,
                    "malformed_candidate_result_check_ids": malformed_candidate_result_check_ids,
                    "contract_proof": {
                        "missing_candidate_ids": missing_contract_proof_candidate_ids,
                        "weak_candidate_ids": weak_contract_proof_candidate_ids,
                        "missing_count": len(missing_contract_proof_candidate_ids),
                        "weak_count": len(weak_contract_proof_candidate_ids),
                    },
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
            "task_status_by_id": {task.id: "completed"},
            "metadata": metadata,
            "node_history": [node_name],
        }

    return review_check_evidence_gate_node
