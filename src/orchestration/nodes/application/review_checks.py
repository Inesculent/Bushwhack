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
from src.orchestration.context.surface_ledger import (
    changed_files_from_diff,
    changed_file_sources_from_state,
    compact_surface_ledger_json,
    surface_by_id,
    surface_ledger_from_state,
)
from src.orchestration.nodes.application.review_check_executor_support import (
    missing_evidence_for_weak_no_finding as _support_missing_evidence_for_weak_no_finding,
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
_EXECUTOR_CODE_EVIDENCE_CHARS = 60000
_EXECUTOR_FOCUSED_EVIDENCE_CHARS = 40000
_EXECUTOR_COMPACT_CODE_EVIDENCE_CHARS = 4000
_EXECUTOR_COMPACT_FOCUSED_EVIDENCE_CHARS = 3000
_EXECUTOR_COMPACT_CONTEXT_CHARS = 8000
_EXECUTOR_MAX_MULTI_CHECK_PROMPT_CHARS = 44000
_COVERAGE_CRITIC_MAX_EMITTED_CHECKS = 3
_COVERAGE_CRITIC_MAX_WARNINGS = 5
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
_BROAD_DIFF_SIGNAL_FAMILIES = {"", "broad_fallback", "surface_fallback", "file_fallback"}


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


def _coverage_critic_should_run(
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    checks: Sequence[ReviewCheck],
) -> tuple[bool, str]:
    if not checks:
        return True, "no_initial_checks"
    if all(check.audit_only for check in checks):
        return True, "all_checks_audit_only"
    non_audit_paths = {
        check.file_path.strip().replace("\\", "/")
        for check in checks
        if not check.audit_only and check.file_path.strip()
    }
    task_paths = {
        path.strip().replace("\\", "/")
        for path in (_changed_task_files(state, task) or task.target_files)
        if str(path).strip()
    }
    if task_paths and not task_paths.issubset(non_audit_paths):
        return True, "missing_changed_surface_check"
    metadata = state.get("metadata", {}) or {}
    fc = metadata.get("focused_context", {}) if isinstance(metadata, Mapping) else {}
    diagnostics = fc.get("diagnostics", []) if isinstance(fc, Mapping) else []
    check_ids = {check.check_id for check in checks}
    for row in diagnostics if isinstance(diagnostics, list) else []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("candidate_id") or "") not in check_ids:
            continue
        outcomes = {str(item) for item in row.get("outcomes", []) or []}
        reason = str(row.get("reason") or "")
        if reason:
            outcomes.add(reason)
        if outcomes & {"no_hits", "tool_unavailable", "path_mismatch"}:
            return True, "focused_context_evidence_gap"
    previous_missing = slot.get("missing_evidence_by_check")
    if isinstance(previous_missing, Mapping) and any(previous_missing.values()):
        return True, "explicit_evidence_gap"
    return False, ""


def _render_coverage_critic_prompt(
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    checks: Sequence[ReviewCheck],
    *,
    compact_retry: bool = False,
) -> str:
    ledger = surface_ledger_from_state(state)
    direct_limit = 12000 if compact_retry else 60000
    mental_limit = 4000 if compact_retry else 12000
    kb_limit = 4000 if compact_retry else 12000
    check_limit = 12000 if compact_retry else 30000
    max_records = 24 if compact_retry else 60
    sections = {
        "Assigned Task": (
            f"Task ID: {task.id}\n"
            f"Title: {task.title}\n"
            f"Description: {task.description[:1000 if compact_retry else 2000]}\n"
            f"Target files: {task.target_files}\n"
            f"Specialty: {task.specialty}"
        ),
        "Changed Code And Direct Context": str(slot.get("direct_context") or "")[:direct_limit],
        "Mental Model Excerpt": str(slot.get("mental_model_excerpt") or "")[:mental_limit],
        "Review KB Context": str(slot.get("review_kb_excerpt") or "")[:kb_limit],
        "Surface Ledger": compact_surface_ledger_json(ledger, max_records=max_records) if ledger else "[]",
        "Compiled Checks": _json_for_prompt(
            [check.model_dump(mode="json") for check in checks],
            max_chars=check_limit,
        ),
    }
    body = render_reviewer_prompt("review_check_compiler.md", sections)
    return (
        f"{body}\n\n"
        "## COVERAGE CRITIC MODE\n"
        "You are not looking for memorized bug classes. Identify missing contract variants in the existing checks.\n"
        "For each changed owner, compare the compiled checks against the changed code and contract context. "
        "If a materially distinct trigger/default/boundary/empty/null/optional/multi-item/error/fallback/"
        "return/serialization path is not represented, emit one additional ReviewCheck for that variant.\n"
        "Only emit checks tied to changed code, concrete expected behavior, a trigger variant, an operation, "
        "and a possible impact. Put the trigger and operation into owned_contract_scope. "
        "If the current checks cover the material variants, return an empty checks list. "
        f"Emit at most {_COVERAGE_CRITIC_MAX_EMITTED_CHECKS} checks and at most "
        f"{_COVERAGE_CRITIC_MAX_WARNINGS} concise warnings."
    )


























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
        covered_by_questions = {
            sid
            for check in checks
            for sid in check.surface_ids
        }
        check_origins = compiler_support.origins_for_checks(
            checks,
            "contract_question",
            "derived_from_behavioral_contract_question",
        )
        if checks:
            warnings.append(f"contract_question_checks_added:{len(checks)}")
        invariant_checks: List[ReviewCheck] = compiler_support.checks_from_surface_invariants(
            state,
            task,
            settings=resolved,
            exclude_surface_ids=covered_by_questions,
        )
        invariant_origins = compiler_support.origins_for_checks(
            invariant_checks,
            "surface_invariant",
            "derived_from_behavioral_surface_invariant",
        )
        checks = [*checks, *invariant_checks]
        check_origins = {**check_origins, **invariant_origins}
        if invariant_checks:
            warnings.append(f"surface_invariant_checks_added:{len(invariant_checks)}")

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
                checks = compiler_support.prioritize_compiled_checks(
                    checks,
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

        coverage_critic_meta: Dict[str, Any] = {"status": "not_run"}
        run_critic, critic_reason = _coverage_critic_should_run(state, task, slot, checks)
        if use_llm and run_critic:
            selected_model = model_key or resolved.reviewer_worker_model_key
            def _invoke_coverage_critic(*, compact_retry: bool = False) -> tuple[ReviewCheckCompilerOutput, Any]:
                critic_llm = Models.worker(
                    ReviewCheckCompilerOutput,
                    model_key=selected_model,
                    max_completion_tokens=resolved.reviewer_critiquer_max_completion_tokens,
                )
                critic_prompt = _render_coverage_critic_prompt(
                    state,
                    task,
                    slot,
                    checks,
                    compact_retry=compact_retry,
                )
                critic_traced = trace_llm_call(
                    critic_llm,
                    critic_prompt,
                    state=state,
                    node_name=node_name,
                    model_key=selected_model,
                    schema_name="ReviewCheckCompilerOutput",
                    request_label="coverage_critic_compact_retry" if compact_retry else "coverage_critic",
                    input_summary={
                        "task_id": task.id,
                        "reason": critic_reason,
                        "compact_retry": compact_retry,
                    },
                )
                critic_response = parse_structured_output(
                    critic_traced.result,
                    ReviewCheckCompilerOutput,
                )
                return critic_response, critic_traced

            try:
                compact_retry_used = False
                try:
                    critic_response, critic_traced = _invoke_coverage_critic()
                except Exception as exc:  # noqa: BLE001
                    if not _is_length_finish_error(exc):
                        raise
                    llm_trace.extend(trace_from_exception(exc))
                    warnings.append("coverage_critic_length_retry")
                    compact_retry_used = True
                    try:
                        critic_response, critic_traced = _invoke_coverage_critic(compact_retry=True)
                    except Exception as retry_exc:  # noqa: BLE001
                        if _is_length_finish_error(retry_exc):
                            llm_trace.extend(trace_from_exception(retry_exc))
                            warnings.append("coverage_critic_degraded_length")
                            coverage_critic_meta = {
                                "status": "degraded_length",
                                "reason": critic_reason,
                                "emitted_count": 0,
                                "emitted_check_ids": [],
                                "compact_retry": True,
                                "error": f"{retry_exc.__class__.__name__}: {retry_exc}",
                            }
                            raise RuntimeError("coverage_critic_degraded_length") from retry_exc
                        raise
                llm_tokens += critic_traced.tokens
                llm_trace.extend(critic_traced.trace_records)
                critic_checks = compiler_support.normalize_compiled_checks(
                    state,
                    task,
                    critic_response.checks[:_COVERAGE_CRITIC_MAX_EMITTED_CHECKS],
                )
                critic_checks = compiler_support.enrich_checks_with_completeness_contracts(
                    critic_checks,
                    slot=slot,
                )
                critic_origins = compiler_support.origins_for_checks(
                    critic_checks,
                    "coverage_critic",
                    f"coverage_critic_missing_contract_variants:{critic_reason}",
                )
                checks = compiler_support.prioritize_compiled_checks(
                    compiler_support.dedupe_checks([*checks, *critic_checks]),
                    task=task,
                    slot=slot,
                )
                check_origins = {
                    **check_origins,
                    **{
                        check.check_id: critic_origins.get(check.check_id)
                        for check in critic_checks
                        if check.check_id in critic_origins
                    },
                }
                critic_warnings = list(critic_response.warnings)[:_COVERAGE_CRITIC_MAX_WARNINGS]
                if len(critic_response.warnings) > len(critic_warnings):
                    critic_warnings.append(
                        f"coverage_critic_warnings_truncated:{len(critic_response.warnings) - len(critic_warnings)}"
                    )
                warnings.extend(critic_warnings)
                coverage_critic_meta = {
                    "status": "ok",
                    "reason": critic_reason,
                    "emitted_count": len(critic_checks),
                    "emitted_check_ids": [check.check_id for check in critic_checks],
                    "warnings": critic_warnings,
                    "compact_retry": compact_retry_used,
                }
                if critic_checks:
                    warnings.append(f"coverage_critic_checks_added:{len(critic_checks)}")
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                if coverage_critic_meta.get("status") != "degraded_length":
                    warnings.append(f"coverage_critic_failed:{exc.__class__.__name__}: {exc}")
                    coverage_critic_meta = {
                        "status": "failed",
                        "reason": critic_reason,
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }

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
        checks, coverage_floor, check_origins = compiler_support.ensure_compiler_coverage_floor(
            state=state,
            task=task,
            checks=checks,
            check_origins=check_origins,
        )
        warnings.extend(coverage_floor.get("warnings", []))
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
                "compiler_coverage_floor": coverage_floor,
                "coverage_critic": coverage_critic_meta,
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
    for check in _checks_for_task(state, task_id):
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
    checks = {check.check_id: check for check in _checks_for_task(state, task.id)}
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
        "behavioral_question": check.behavioral_question,
        "affected_invariant": check.affected_invariant,
        "required_evidence": list(check.required_evidence[:5]),
        "suppress_criteria": list(check.suppress_criteria[:4]),
        "report_criteria": list(check.report_criteria[:4]),
        "allowed_retrieval": list(check.allowed_retrieval[:4]),
        "budget": check.budget,
    }


def _render_executor_prompt(
    state: GraphState,
    task: ReviewTask,
    checks: List[ReviewCheck],
    slot: Mapping[str, Any],
    *,
    compact_retry: bool = False,
    appendix: str = "",
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
        "Repository Code Evidence": str(slot.get("direct_context") or "")[:code_limit],
        "Focused Evidence By Check": _json_for_prompt(focused, max_chars=focused_limit),
        "Mental Model Excerpt": mental_model_excerpt,
        "Review KB Context": review_kb_excerpt,
    }
    prompt = render_reviewer_prompt("review_check_executor.md", sections)
    if compact_retry:
        prompt = f"{prompt}{_EXECUTOR_COMPACT_RETRY_APPENDIX}"
    if appendix:
        prompt = f"{prompt}{appendix}"
    return prompt


def _missing_evidence_for_weak_no_finding(check: ReviewCheck) -> List[str]:
    return _support_missing_evidence_for_weak_no_finding(check, _evidence_requirements_for_check)


def _check_batches(checks: Sequence[ReviewCheck]) -> List[List[ReviewCheck]]:
    return [list(checks[index : index + _EXECUTOR_BATCH_SIZE]) for index in range(0, len(checks), _EXECUTOR_BATCH_SIZE)]


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
    if result.decision == "no_finding" and "llm_suppression_audit_sufficient" in result.warnings:
        return 40
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


def _executor_continuation_targets(
    checks: Sequence[ReviewCheck],
    results: Sequence[ReviewCheckResult],
) -> List[ReviewCheck]:
    checks_by_id = {check.check_id: check for check in checks}
    candidate_files: set[str] = set()
    candidate_surfaces: set[str] = set()
    for result in results:
        if result.decision != "candidate" or result.candidate is None:
            continue
        check = checks_by_id.get(result.check_id)
        candidate_files.add(result.candidate.file_path.replace("\\", "/"))
        if check is not None:
            candidate_files.add(check.file_path.replace("\\", "/"))
            candidate_surfaces.update(check.surface_ids)
    if not candidate_files and not candidate_surfaces:
        return []
    targets: List[ReviewCheck] = []
    seen_scopes: set[str] = set()
    for result in results:
        if result.decision not in {"no_finding", "unsupported"}:
            continue
        check = checks_by_id.get(result.check_id)
        if check is None:
            continue
        same_file = check.file_path.replace("\\", "/") in candidate_files
        same_surface = bool(candidate_surfaces.intersection(check.surface_ids))
        if same_file or same_surface:
            scope = check.owned_contract_scope.strip() or check.check_id
            if scope in seen_scopes:
                continue
            seen_scopes.add(scope)
            targets.append(check)
        if len(targets) >= 2:
            break
    return targets


def _executor_result_summary(result: ReviewCheckResult) -> Dict[str, Any]:
    candidate = result.candidate
    return {
        "check_id": result.check_id,
        "decision": result.decision,
        "candidate": (
            {
                "candidate_id": candidate.candidate_id,
                "file_path": candidate.file_path,
                "line_start": candidate.line_start,
                "line_end": candidate.line_end,
                "content": candidate.content[:300],
                "failure_mode": candidate.failure_mode[:240],
            }
            if candidate is not None
            else None
        ),
        "reportable_reason": result.reportable_reason[:400],
        "missing_evidence": list(result.missing_evidence[:4]),
    }


def _executor_continuation_appendix(
    *,
    batch_results: Sequence[ReviewCheckResult],
    target_checks: Sequence[ReviewCheck],
) -> str:
    target_ids = [check.check_id for check in target_checks]
    candidate_summaries = [
        _executor_result_summary(result)
        for result in batch_results
        if result.decision == "candidate" and result.candidate is not None
    ][:6]
    return (
        "\n\n## SAME-BATCH CONTINUATION (required)\n"
        "The same batch already produced the candidate results below. Do not repeat or rephrase them.\n"
        "Reconsider only the listed target check_ids from this same batch for distinct reachable failures "
        "that may have been overshadowed. Return exactly one ReviewCheckResult for each target check_id. "
        "Do not return any check_id outside this list and do not invent new checks.\n\n"
        f"Target check_ids: {json.dumps(target_ids, ensure_ascii=False)}\n"
        "Existing candidate results:\n"
        f"{json.dumps(candidate_summaries, indent=2, ensure_ascii=False)}"
    )


def _merge_executor_continuation_results(
    batch_results: Sequence[ReviewCheckResult],
    continuation_results: Sequence[ReviewCheckResult],
    target_ids: set[str],
) -> tuple[List[ReviewCheckResult], List[str]]:
    merged: List[ReviewCheckResult] = list(batch_results)
    index_by_check = {result.check_id: index for index, result in enumerate(merged)}
    revised: List[str] = []
    for result in continuation_results:
        if result.check_id not in target_ids:
            continue
        old_index = index_by_check.get(result.check_id)
        if old_index is None:
            merged.append(result)
            index_by_check[result.check_id] = len(merged) - 1
            revised.append(result.check_id)
            continue
        old = merged[old_index]
        if old.model_dump(mode="json") != result.model_dump(mode="json"):
            merged[old_index] = result
            revised.append(result.check_id)
    return merged, revised


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
) -> tuple[List[ReviewCheckResult], int, List[Dict[str, Any]], List[str], Dict[str, Dict[str, Any]]]:
    return results, llm_tokens, llm_trace, warnings, {}


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
        suppression_audits: Dict[str, Dict[str, Any]] = {}
        length_limit_batch_failures: List[Dict[str, Any]] = []
        length_limit_retry_count = 0
        length_limit_retry_success_count = 0
        length_limit_retry_failed_check_ids: List[str] = []
        executor_continuation_count = 0
        executor_continuation_revised_check_ids: List[str] = []
        executor_continuation_failed_batches: List[int] = []
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
                    (
                        retry_results,
                        llm_tokens,
                        llm_trace,
                        warnings,
                        retry_audits,
                    ) = _audit_no_finding_suppressions(
                        state=state,
                        task=task,
                        checks=[check],
                        results=retry_results,
                        selected_model=selected_model,
                        llm_tokens=llm_tokens,
                        llm_trace=llm_trace,
                        warnings=warnings,
                    )
                    suppression_audits.update(retry_audits)
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
                            missing_evidence=_missing_evidence_for_weak_no_finding(check),
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
                    continuation_targets = _executor_continuation_targets(batch, batch_results)
                    if continuation_targets:
                        executor_continuation_count += 1
                        try:
                            continuation_llm = Models.worker(
                                ReviewCheckExecutorOutput,
                                model_key=selected_model,
                                max_completion_tokens=resolved.reviewer_critiquer_max_completion_tokens,
                            )
                            continuation_prompt = _render_executor_prompt(
                                state,
                                task,
                                continuation_targets,
                                slot,
                                appendix=_executor_continuation_appendix(
                                    batch_results=batch_results,
                                    target_checks=continuation_targets,
                                ),
                            )
                            continuation_traced = trace_llm_call(
                                continuation_llm,
                                continuation_prompt,
                                state=state,
                                node_name=node_name,
                                model_key=selected_model,
                                schema_name="ReviewCheckExecutorOutput",
                                request_label="same_batch_continuation",
                                input_summary={
                                    "task_id": task.id,
                                    "batch": batch_index,
                                    "target_check_ids": [
                                        check.check_id for check in continuation_targets
                                    ],
                                },
                            )
                            continuation_response = parse_structured_output(
                                continuation_traced.result,
                                ReviewCheckExecutorOutput,
                            )
                            llm_tokens += continuation_traced.tokens
                            llm_trace.extend(continuation_traced.trace_records)
                            continuation_results, continuation_warnings = _support_normalize_executor_results(
                                state=state,
                                task=task,
                                slot=slot,
                                checks=continuation_targets,
                                results=continuation_response.results,
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
                            target_ids = {check.check_id for check in continuation_targets}
                            batch_results, revised_ids = _merge_executor_continuation_results(
                                batch_results,
                                continuation_results,
                                target_ids,
                            )
                            executor_continuation_revised_check_ids.extend(revised_ids)
                            result_count_before_canonicalization += len(continuation_results)
                            warnings.extend(continuation_response.warnings)
                            warnings.extend(continuation_warnings)
                            if revised_ids:
                                warnings.append(
                                    "executor_same_batch_continuation_revised:"
                                    + ",".join(sorted(set(revised_ids)))
                                )
                        except Exception as continuation_exc:  # noqa: BLE001
                            llm_trace.extend(trace_from_exception(continuation_exc))
                            executor_continuation_failed_batches.append(batch_index)
                            warnings.append(
                                "executor_same_batch_continuation_failed:"
                                f"{batch_index}:{continuation_exc.__class__.__name__}: {continuation_exc}"
                            )
                        batch_results, continuation_duplicate_ids = _canonicalize_executor_results(batch_results)
                        duplicate_result_check_ids.extend(continuation_duplicate_ids)
                    (
                        batch_results,
                        llm_tokens,
                        llm_trace,
                        warnings,
                        batch_audits,
                    ) = _audit_no_finding_suppressions(
                        state=state,
                        task=task,
                        checks=batch,
                        results=batch_results,
                        selected_model=selected_model,
                        llm_tokens=llm_tokens,
                        llm_trace=llm_trace,
                        warnings=warnings,
                    )
                    suppression_audits.update(batch_audits)
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
        synthesized_candidate_check_ids = [
            warning.split(":", 1)[1]
            for warning in warnings
            if warning.startswith("executor_candidate_payload_synthesized:")
        ]
        missing_candidate_payload_check_ids = [
            warning.split(":", 1)[1]
            for warning in warnings
            if warning.startswith("executor_candidate_missing_payload:")
        ]
        contract_backfill_events = []
        for warning in warnings:
            if not warning.startswith("executor_contract_proof_backfilled:"):
                continue
            _prefix, rest = warning.split(":", 1)
            check_id, raw_fields = (rest.rsplit(":", 1) + [""])[:2]
            contract_backfill_events.append(
                {
                    "check_id": check_id,
                    "fields": [field for field in raw_fields.split(",") if field],
                }
            )
        source_only_override_check_ids = [
            warning.split(":", 1)[1]
            for warning in warnings
            if warning.startswith("executor_source_only_no_finding_overridden:")
        ]
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
                "executor_max_multi_check_prompt_chars": _EXECUTOR_MAX_MULTI_CHECK_PROMPT_CHARS,
                "executor_oversized_batch_splits": executor_oversized_batch_splits,
                "executor_missing_result_check_ids": list(dict.fromkeys(missing_result_check_ids)),
                "executor_duplicate_result_check_ids": list(dict.fromkeys(duplicate_result_check_ids)),
                "executor_result_count_before_canonicalization": result_count_before_canonicalization,
                "executor_retry_count": executor_retry_count,
                "executor_retry_success_count": executor_retry_success_count,
                "executor_candidate_payload_synthesized_check_ids": list(
                    dict.fromkeys(synthesized_candidate_check_ids)
                ),
                "executor_candidate_missing_payload_check_ids": list(
                    dict.fromkeys(missing_candidate_payload_check_ids)
                ),
                "executor_contract_proof_backfills": contract_backfill_events,
                "executor_contract_proof_backfill_count": len(contract_backfill_events),
                "executor_source_only_override_check_ids": list(dict.fromkeys(source_only_override_check_ids)),
                "executor_length_limit_batch_failures": length_limit_batch_failures,
                "executor_length_limit_retry_count": length_limit_retry_count,
                "executor_length_limit_retry_success_count": length_limit_retry_success_count,
                "executor_length_limit_retry_failed_check_ids": list(
                    dict.fromkeys(length_limit_retry_failed_check_ids)
                ),
                "executor_same_batch_continuation_count": executor_continuation_count,
                "executor_same_batch_continuation_revised_check_ids": list(
                    dict.fromkeys(executor_continuation_revised_check_ids)
                ),
                "executor_same_batch_continuation_failed_batches": executor_continuation_failed_batches,
                "suppression_audits": suppression_audits,
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
        checks = {check.check_id: check for check in _checks_for_task(state, task.id)}
        promoted: List[CandidateFinding] = []
        lifecycle: Dict[str, Any] = {}
        gated_results: List[ReviewCheckResult] = []
        gate_reason_counts: Dict[str, int] = {}
        malformed_candidate_result_check_ids: List[str] = []
        missing_contract_proof_candidate_ids: List[str] = []
        weak_contract_proof_candidate_ids: List[str] = []
        dropped = 0
        for result in _results_for_task(state, task.id):
            check = checks.get(result.check_id)
            candidate = result.candidate
            if result.decision == "candidate" and candidate is None:
                reason = "candidate_payload_missing"
                malformed_candidate_result_check_ids.append(result.check_id)
                dropped += 1
                gated_results.append(
                    result.model_copy(update={"gate_decision": "dropped", "gate_reason": reason})
                )
                lifecycle[result.check_id] = {
                    "decision": "dropped",
                    "check_id": result.check_id,
                    "reason": reason,
                }
                gate_reason_counts[reason] = gate_reason_counts.get(reason, 0) + 1
                continue
            if result.decision != "candidate" or check is None:
                continue
            passed, reason = _candidate_passes_gate(candidate, result, check, state)
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
                if reason in {
                    "missing_expected_behavior",
                    "missing_contract_evidence",
                    "missing_counterexample",
                    "missing_rejection_check",
                }:
                    missing_contract_proof_candidate_ids.append(candidate.candidate_id)
                elif reason == "weak_contract_proof":
                    weak_contract_proof_candidate_ids.append(candidate.candidate_id)
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
        candidate_decision_count = sum(1 for result in latest_results if result.decision == "candidate")
        gate_expected_count = candidate_decision_count
        gate_evaluated_count = len(gated_results)
        health_warnings: List[str] = []
        if checks and candidate_decision_count and not gated_results:
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
                    "evaluated_count": len(gated_results),
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
            "review_check_results": gated_results,
            "task_status_by_id": {task.id: "completed"},
            "metadata": metadata,
            "node_history": [node_name],
        }

    return review_check_evidence_gate_node


def should_run_review_check_scout(state: GraphState) -> bool:
    task = _task_from_state(state)
    if task is None:
        return False
    metadata = state.get("metadata", {}) or {}
    block = metadata.get("review_checks", {}) if isinstance(metadata, dict) else {}
    by_task = block.get("by_task", {}) if isinstance(block, dict) else {}
    slot = by_task.get(task.id, {}) if isinstance(by_task, dict) else {}
    if not isinstance(slot, dict):
        return False
    scout = slot.get("scout") if isinstance(slot.get("scout"), dict) else {}
    if scout.get("status") in {"emitted", "not_needed", "no_concrete_obligations"}:
        return False
    gate = slot.get("gate") if isinstance(slot.get("gate"), dict) else {}
    if int(gate.get("promoted_count") or 0) > 0:
        return False
    unsupported = gate.get("unsupported_high_confidence_surface_ids")
    if not isinstance(unsupported, list) or not unsupported:
        return False
    coverage_floor = slot.get("compiler_coverage_floor") if isinstance(slot.get("compiler_coverage_floor"), dict) else {}
    obligations = coverage_floor.get("uncovered_obligations")
    if not isinstance(obligations, list):
        return False
    return any(
        isinstance(item, Mapping) and _obligation_is_concrete_contract_delta(item)
        for item in obligations
    )


def _obligation_is_concrete_contract_delta(obligation: Mapping[str, Any]) -> bool:
    family = str(obligation.get("diff_signal_family") or obligation.get("issue_family") or "").strip()
    if family in _BROAD_DIFF_SIGNAL_FAMILIES:
        return False
    if family == "contract_delta":
        return bool(
            str(obligation.get("diff_signal") or "").strip()
            or str(obligation.get("evidence") or "").strip()
        )
    return True


def make_review_check_scout_node():
    node_name = "review_check_scout"

    def review_check_scout_node(state: GraphState) -> Dict[str, Any]:
        task = _task_from_state(state)
        if task is None:
            return {"node_history": [f"{node_name}:skipped"]}
        metadata = state.get("metadata", {}) or {}
        block = metadata.get("review_checks", {}) if isinstance(metadata, dict) else {}
        by_task = block.get("by_task", {}) if isinstance(block, dict) else {}
        slot = by_task.get(task.id, {}) if isinstance(by_task, dict) else {}
        slot = dict(slot) if isinstance(slot, dict) else {}
        coverage_floor = slot.get("compiler_coverage_floor") if isinstance(slot.get("compiler_coverage_floor"), dict) else {}
        raw_obligations = coverage_floor.get("uncovered_obligations")
        obligations = [item for item in raw_obligations if isinstance(item, Mapping)] if isinstance(raw_obligations, list) else []
        existing_ids = {check.check_id for check in _checks_for_task(state, task.id)}
        concrete = [
            item
            for item in obligations
            if _obligation_is_concrete_contract_delta(item)
        ][:2]
        checks: List[ReviewCheck] = []
        for index, obligation in enumerate(concrete, start=1):
            check = compiler_support.coverage_check_for_obligation(
                state,
                task,
                obligation,
                900 + index,
            )
            check_id = f"{task.id}:scout:{index}"
            if check_id in existing_ids:
                continue
            checks.append(
                check.model_copy(
                    update={
                        "check_id": check_id,
                        "allowed_retrieval": ["task_evidence"],
                        "budget": 1,
                        "audit_only": False,
                    }
                )
            )
        status = "emitted" if checks else "no_concrete_obligations"
        metadata = _set_task_review_checks_meta(
            state,
            task.id,
            {
                "scout": {
                    "status": status,
                    "emitted_check_ids": [check.check_id for check in checks],
                    "source": "unsupported_high_confidence_surface_gap",
                }
            },
        )
        return {
            "review_checks": checks,
            "metadata": metadata,
            "node_history": [node_name if checks else f"{node_name}:skipped"],
        }

    return review_check_scout_node
