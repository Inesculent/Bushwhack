"""Check-first review nodes for the adversarial reviewer ablation."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from src.config import Settings, get_settings
from src.domain.schemas import (
    BehavioralSpec,
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
from src.infrastructure.behavioral_spec_store import BehavioralSpecStore
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import trace_from_exception, trace_llm_call
from src.orchestration.context.focus_request_scope import (
    allowed_review_paths,
    clamp_focused_context_request,
)
from src.orchestration.context.context_packets import focused_snippets_for_candidate
from src.orchestration.context.surface_ledger import (
    compact_surface_ledger_json,
    surface_by_id,
    surface_ids_for_task,
    surface_ids_for_text,
    surface_ledger_from_state,
)
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
_MAX_CHECKS_PER_TASK = 10
_EXECUTOR_BATCH_SIZE = 3
_DIMENSION_RELEVANCE_KEYWORDS = {
    "contract completeness": ("contract", "return", "return_types", "declared", "schema", "required", "optional"),
    "branch exhaustiveness": ("branch", "elif", "else", "case", "mode", "fallback", "exhaustive"),
    "boundary/index handling": ("index", "bounds", "length", "empty", "slice", "range", "off-by-one"),
    "structured data preservation": ("structured", "tuple", "row", "record", "json", "dict", "field", "payload"),
    "aggregation/serialization safety": ("join", "aggregate", "serialize", "serialization", "format", "json"),
    "exception/control-flow scope": ("exception", "error", "try", "except", "raise", "regex", "invalid pattern"),
    "resource-amplification risk": ("resource", "performance", "unbounded", "loop", "regex", "expensive", "redundant"),
    "migration caller-reliance contract": (
        "migration",
        "migrate",
        "replace",
        "removed",
        "caller",
        "call site",
        "precondition",
        "reliance",
    ),
    "state/cache lifecycle migration contract": (
        "state",
        "cache",
        "lifecycle",
        "block",
        "slot",
        "queue",
        "reuse",
        "invalidate",
    ),
    "api/signature compatibility": ("api", "signature", "caller", "interface", "type", "public", "framework"),
    "dependency/import availability": ("import", "dependency", "module", "symbol", "undefined", "include"),
    "nullability/panic safety": ("null", "none", "nil", "panic", "optional", "nullable", "guard"),
    "state/cache lifecycle": ("state", "cache", "lifecycle", "invalidate", "reset", "cleanup"),
    "protocol/output fidelity": ("protocol", "output", "format", "status", "header", "message", "response"),
    "concurrency/shared-state safety": ("concurrency", "thread", "async", "await", "lock", "race", "shared"),
    "security/input boundary": ("security", "auth", "permission", "sanitize", "escape", "input", "validation"),
    "repository convention contract": ("convention", "framework", "input_types", "return_types", "repository"),
    "public/user contract": ("public", "user-visible", "docs", "tooltip", "cli", "message"),
    "maintainability contract": ("maintainability", "unused", "dead code", "duplicate", "deprecated"),
}
_EXTERNAL_EVIDENCE_MARKERS = (
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


def _dimension_to_lens(dimension: str) -> str:
    dim = dimension.lower()
    if "security" in dim or "permission" in dim:
        return "permission_boundary"
    if "state" in dim or "cache" in dim:
        return "state_transition"
    if "null" in dim or "input" in dim or "validation" in dim:
        return "input_validation"
    if "exception" in dim or "error" in dim:
        return "error_propagation"
    if "resource" in dim or "lifecycle" in dim:
        return "resource_lifecycle"
    if "migration" in dim or "caller-reliance" in dim or "api" in dim or "signature" in dim or "contract" in dim:
        return "api_compatibility"
    if "structured" in dim or "index" in dim or "aggregation" in dim or "serialization" in dim:
        return "data_shape_consistency"
    if "concurrency" in dim or "ordering" in dim:
        return "concurrency_ordering"
    if "test" in dim:
        return "test_oracle_strength"
    return "other"


def _surface_ids_for_check_context(
    *,
    task: ReviewTask,
    file_path: str,
    anchor: str,
    state: GraphState,
) -> List[str]:
    ledger = surface_ledger_from_state(state)
    if not ledger:
        return list(task.surface_ids)
    ids = surface_ids_for_text(anchor, ledger)
    if not ids:
        by_id = surface_by_id(ledger)
        ids = [sid for sid in task.surface_ids if sid in by_id]
    if not ids and (not anchor.strip() or anchor.strip().replace("\\", "/") == file_path):
        ids = surface_ids_for_task(task, ledger)
    if file_path and ids:
        by_id = surface_by_id(ledger)
        file_matches = [sid for sid in ids if by_id.get(sid) and by_id[sid].file_path == file_path]
        if file_matches:
            ids = file_matches
    return ids


def _surface_anchor_update(surface_ids: List[str], state: GraphState) -> Dict[str, Any]:
    if len(surface_ids) != 1:
        return {}
    surface = surface_by_id(surface_ledger_from_state(state)).get(surface_ids[0])
    if surface is None:
        return {}
    update: Dict[str, Any] = {
        "file_path": surface.file_path,
        "changed_code_anchor": surface.name,
        "surface_ids": [surface.surface_id],
    }
    if surface.line_start is not None:
        update["line_start"] = surface.line_start
        update["line_end"] = surface.line_end or surface.line_start
    return update


def _fallback_checks(state: GraphState, task: ReviewTask, slot: Mapping[str, Any]) -> List[ReviewCheck]:
    obligations = _rotate_tied_obligations(_ranked_coverage_obligations(task, slot))
    checks: List[ReviewCheck] = []
    for index, raw in enumerate(obligations[:6], start=1):
        row = raw if isinstance(raw, Mapping) else {}
        file_path = str(row.get("file_path") or (task.target_files[0] if task.target_files else ""))
        dimension = str(row.get("dimension") or "task contract")
        surface = str(row.get("surface") or file_path or "changed code")
        surface_ids = _surface_ids_for_check_context(
            task=task,
            file_path=file_path,
            anchor=surface,
            state=state,
        )[:1]
        anchor_update = _surface_anchor_update(surface_ids, state)
        file_path = str(anchor_update.get("file_path") or file_path)
        surface = str(anchor_update.get("changed_code_anchor") or surface)
        contract_material = [
            f"mental model/KB contract hypothesis to verify: {item}"
            for item in row.get("mental_model_contract_material", [])
            if str(item).strip()
        ][:2]
        checks.append(
            ReviewCheck(
                check_id=f"{task.id}:check:{index}",
                patch_task_id=task.id,
                surface_ids=surface_ids,
                lens=_dimension_to_lens(dimension),  # type: ignore[arg-type]
                file_path=file_path,
                line_start=int(anchor_update.get("line_start") or 1),
                line_end=int(anchor_update.get("line_end") or anchor_update.get("line_start") or 1),
                changed_code_anchor=surface,
                behavioral_question=f"Does the changed {surface} preserve {dimension}?",
                affected_invariant=dimension,
                required_evidence=[
                    str(row.get("evidence") or f"code evidence for {dimension}"),
                    *contract_material,
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
    surface_ids = _surface_ids_for_check_context(
        task=task,
        file_path=file_path,
        anchor=task.title,
        state=state,
    )[:1]
    anchor_update = _surface_anchor_update(surface_ids, state)
    return [
        ReviewCheck(
            check_id=f"{task.id}:check:1",
            patch_task_id=task.id,
            surface_ids=surface_ids,
            lens="other",
            file_path=str(anchor_update.get("file_path") or file_path),
            line_start=int(anchor_update.get("line_start") or 1),
            line_end=int(anchor_update.get("line_end") or anchor_update.get("line_start") or 1),
            changed_code_anchor=str(anchor_update.get("changed_code_anchor") or file_path or task.title),
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


def _coverage_check_for_file(state: GraphState, task: ReviewTask, file_path: str, index: int) -> ReviewCheck:
    surface_ids = _surface_ids_for_check_context(
        task=task,
        file_path=file_path,
        anchor=file_path,
        state=state,
    )[:1]
    anchor_update = _surface_anchor_update(surface_ids, state)
    return ReviewCheck(
        check_id=f"{task.id}:coverage:{index}",
        patch_task_id=task.id,
        surface_ids=surface_ids,
        lens="other",
        file_path=str(anchor_update.get("file_path") or file_path),
        line_start=int(anchor_update.get("line_start") or 1),
        line_end=int(anchor_update.get("line_end") or anchor_update.get("line_start") or 1),
        changed_code_anchor=str(anchor_update.get("changed_code_anchor") or file_path),
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


def _coverage_obligations(slot: Mapping[str, Any]) -> List[Dict[str, Any]]:
    raw_obligations = slot.get("coverage_obligations")
    if not isinstance(raw_obligations, list):
        return []
    obligations: List[Dict[str, Any]] = []
    for raw in raw_obligations:
        if not isinstance(raw, Mapping):
            continue
        file_path = str(raw.get("file_path") or "").strip().replace("\\", "/")
        dimension = str(raw.get("dimension") or "").strip()
        surface = str(raw.get("surface") or file_path or "changed code").strip()
        if not file_path or not dimension:
            continue
        row = dict(raw)
        row["file_path"] = file_path
        row["dimension"] = dimension
        row["surface"] = surface
        obligations.append(row)
    return obligations


def _contains_any_keyword(text: str, keywords: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords if keyword)


def _mental_model_contract_lines(slot: Mapping[str, Any]) -> List[str]:
    text = "\n".join(
        str(slot.get(key) or "")
        for key in ("mental_model_excerpt", "review_kb_excerpt")
    )
    lines: List[str] = []
    markers = (
        "contract",
        "expect",
        "risk",
        "uncertain",
        "return",
        "input_types",
        "return_types",
        "required",
        "optional",
        "schema",
        "framework",
        "convention",
        "regex",
        "invalid",
        "exception",
        "output",
        "migration",
        "migrate",
        "caller",
        "call site",
        "precondition",
        "reliance",
        "lifecycle",
    )
    for raw in text.splitlines():
        line = re.sub(r"^[-*#\s]+", "", raw).strip()
        if not line:
            continue
        if line.lower() in {"intent", "behavior", "contracts", "risks", "guidance", "uncertainties"}:
            continue
        if _contains_any_keyword(line, markers):
            lines.append(line[:300])
        if len(lines) >= 8:
            break
    return lines


def _relevance_context(task: ReviewTask, slot: Mapping[str, Any]) -> Dict[str, str]:
    return {
        "task": f"{task.title} {task.description} {task.specialty}",
        "mental_model": str(slot.get("mental_model_excerpt") or ""),
        "review_kb": str(slot.get("review_kb_excerpt") or ""),
    }


def _score_obligation_relevance(
    task: ReviewTask,
    slot: Mapping[str, Any],
    obligation: Mapping[str, Any],
) -> tuple[int, List[str], List[str]]:
    surface = str(obligation.get("surface") or "")
    dimension = str(obligation.get("dimension") or "").lower()
    evidence = str(obligation.get("evidence") or "")
    contexts = _relevance_context(task, slot)
    keywords = _DIMENSION_RELEVANCE_KEYWORDS.get(dimension, ())
    score = 1
    reasons: List[str] = ["baseline_changed_surface"]

    if _tokens_overlap(surface, contexts["task"]):
        score += 3
        reasons.append("surface_in_task")
    if _tokens_overlap(surface, contexts["mental_model"]):
        score += 4
        reasons.append("surface_in_mental_model")
    if _tokens_overlap(surface, contexts["review_kb"]):
        score += 3
        reasons.append("surface_in_review_kb")
    if _contains_any_keyword(contexts["mental_model"], keywords):
        score += 4
        reasons.append("dimension_in_mental_model")
    if _contains_any_keyword(contexts["review_kb"], keywords):
        score += 3
        reasons.append("dimension_in_review_kb")
    if _contains_any_keyword(contexts["task"], keywords):
        score += 2
        reasons.append("dimension_in_task")
    if _tokens_overlap(evidence, contexts["mental_model"]) or _tokens_overlap(evidence, contexts["review_kb"]):
        score += 1
        reasons.append("evidence_matches_context")

    material: List[str] = []
    for line in _mental_model_contract_lines(slot):
        if (
            _tokens_overlap(surface, line)
            or _contains_any_keyword(line, keywords)
            or _tokens_overlap(evidence, line)
        ):
            material.append(line)
        if len(material) >= 2:
            break
    return score, reasons, material


def _ranked_coverage_obligations(task: ReviewTask, slot: Mapping[str, Any]) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    for index, obligation in enumerate(_coverage_obligations(slot)):
        row = dict(obligation)
        score, reasons, material = _score_obligation_relevance(task, slot, row)
        row["source_index"] = index
        row["relevance_score"] = score
        row["relevance_reasons"] = reasons
        row["mental_model_contract_material"] = material
        ranked.append(row)
    return sorted(
        ranked,
        key=lambda row: (
            -int(row.get("relevance_score") or 0),
            str(row.get("file_path") or ""),
            str(row.get("surface") or ""),
            int(row.get("source_index") or 0),
        ),
    )


def _surface_key(obligation: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(obligation.get("file_path") or "").replace("\\", "/"),
        str(obligation.get("surface") or ""),
    )


def _rotate_tied_obligations(obligations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered: List[Dict[str, Any]] = []
    scores = sorted(
        {int(row.get("relevance_score") or 0) for row in obligations},
        reverse=True,
    )
    for score in scores:
        bucket = [row for row in obligations if int(row.get("relevance_score") or 0) == score]
        groups: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
        for row in bucket:
            groups.setdefault(_surface_key(row), []).append(row)
        keys = sorted(groups)
        while keys:
            next_keys: List[tuple[str, str]] = []
            for key in keys:
                group = groups[key]
                if group:
                    ordered.append(group.pop(0))
                if group:
                    next_keys.append(key)
            keys = next_keys
    return ordered


def _tokens_overlap(left: str, right: str) -> bool:
    left_tokens = _meaningful_tokens(left)
    right_tokens = _meaningful_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    return bool(left_tokens & right_tokens)


def _check_covers_obligation(check: ReviewCheck, obligation: Mapping[str, Any]) -> bool:
    file_path = str(obligation.get("file_path") or "").strip().replace("\\", "/")
    if check.file_path.strip().replace("\\", "/") != file_path:
        return False
    surface = str(obligation.get("surface") or "")
    dimension = str(obligation.get("dimension") or "")
    blob = " ".join(
        [
            check.changed_code_anchor,
            check.behavioral_question,
            check.affected_invariant,
            " ".join(check.required_evidence),
            " ".join(check.report_criteria),
        ]
    )
    surface_ok = not surface.strip() or _tokens_overlap(surface, blob) or surface.lower() in blob.lower()
    dimension_ok = _tokens_overlap(dimension, blob) or dimension.lower() in blob.lower()
    return surface_ok and dimension_ok


def _coverage_check_for_obligation(
    state: GraphState,
    task: ReviewTask,
    obligation: Mapping[str, Any],
    index: int,
) -> ReviewCheck:
    file_path = str(obligation.get("file_path") or (task.target_files[0] if task.target_files else ""))
    surface = str(obligation.get("surface") or file_path or "changed code")
    dimension = str(obligation.get("dimension") or "task contract")
    evidence = str(obligation.get("evidence") or f"repository evidence for {dimension}")
    surface_ids = _surface_ids_for_check_context(
        task=task,
        file_path=file_path,
        anchor=surface,
        state=state,
    )[:1]
    anchor_update = _surface_anchor_update(surface_ids, state)
    file_path = str(anchor_update.get("file_path") or file_path)
    surface = str(anchor_update.get("changed_code_anchor") or surface)
    contract_material = [
        f"mental model/KB contract hypothesis to verify: {item}"
        for item in obligation.get("mental_model_contract_material", [])
        if str(item).strip()
    ][:2]
    return ReviewCheck(
        check_id=f"{task.id}:coverage:{index}",
        patch_task_id=task.id,
        surface_ids=surface_ids,
        lens=_dimension_to_lens(dimension),  # type: ignore[arg-type]
        file_path=file_path,
        line_start=int(anchor_update.get("line_start") or 1),
        line_end=int(anchor_update.get("line_end") or anchor_update.get("line_start") or 1),
        changed_code_anchor=surface,
        behavioral_question=f"Does the changed {surface} preserve {dimension}?",
        affected_invariant=dimension,
        required_evidence=[
            evidence,
            *contract_material,
            f"changed behavior of {surface} in {file_path}",
            "caller, contract, framework, or repository-convention evidence if the local code is not enough",
        ],
        suppress_criteria=[
            f"Concrete repository evidence shows {surface} preserves {dimension}.",
        ],
        report_criteria=[
            f"The changed {surface} violates {dimension} on a concrete reachable path.",
        ],
        allowed_retrieval=["task_evidence", "focused_context"],
        budget=2,
    )


def _migration_context_present(state: GraphState, task: ReviewTask, slot: Mapping[str, Any]) -> bool:
    blob = "\n".join(
        [
            str(state.get("git_diff") or ""),
            task.title,
            task.description,
            str(slot.get("mental_model_excerpt") or ""),
            str(slot.get("review_kb_excerpt") or ""),
        ]
    ).lower()
    markers = (
        "migration",
        "migrate",
        "merged",
        "merge",
        "replace",
        "removed",
        "removal",
        "rename",
    )
    return any(marker in blob for marker in markers)


def _check_covers_dimension(check: ReviewCheck, dimension: str) -> bool:
    blob = " ".join(
        [
            check.lens,
            check.behavioral_question,
            check.affected_invariant,
            " ".join(check.required_evidence),
        ]
    ).lower()
    lower_dimension = dimension.lower()
    if "migration" in lower_dimension:
        return "migration" in blob and ("reliance" in blob or "old-path" in blob or "old path" in blob)
    if "maintainability" in lower_dimension:
        return check.affected_invariant.strip().lower() == "maintainability contract"
    tokens = sorted(_meaningful_tokens(dimension))
    return bool(tokens) and all(token in blob for token in tokens[:2])


def _surface_check_for_dimension(
    *,
    state: GraphState,
    task: ReviewTask,
    surface: ReviewSurface,
    dimension: str,
    index: int,
) -> ReviewCheck:
    line_start = surface.line_start or 1
    line_end = surface.line_end or line_start
    if "state/cache" in dimension:
        required_evidence = [
            f"changed state/cache lifecycle code for {surface.name}",
            "old-path lifecycle ordering from deleted diff or repository precedent",
            "caller evidence for which state objects are passed, released, reused, or invalidated",
        ]
        question = f"Does the migrated {surface.name} preserve state/cache lifecycle ordering?"
    else:
        required_evidence = [
            f"changed implementation or call-site evidence for {surface.name}",
            "old or deleted-path contract evidence for the behavior being migrated",
            "new callee signature and required arguments/state inputs",
            "caller reliance on preconditions, computed state, exception behavior, and lifecycle order",
        ]
        question = f"Does the migrated {surface.name} preserve caller-reliance and old-vs-new contract behavior?"
    return ReviewCheck(
        check_id=f"{task.id}:coverage:{index}",
        patch_task_id=task.id,
        surface_ids=[surface.surface_id],
        lens=_dimension_to_lens(dimension),  # type: ignore[arg-type]
        file_path=surface.file_path,
        line_start=line_start,
        line_end=line_end,
        changed_code_anchor=surface.name,
        behavioral_question=question,
        affected_invariant=dimension,
        required_evidence=required_evidence,
        suppress_criteria=[
            f"Concrete repository evidence shows {surface.name} preserves {dimension}.",
        ],
        report_criteria=[
            f"The changed {surface.name} violates {dimension} on a concrete reachable path.",
        ],
        allowed_retrieval=["task_evidence", "focused_context"],
        budget=2,
    )


def _migration_floor_checks(
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    checks: Sequence[ReviewCheck],
    start_index: int,
) -> List[ReviewCheck]:
    if not _migration_context_present(state, task, slot):
        return []
    if any(_check_covers_dimension(check, "migration caller-reliance contract") for check in checks):
        return []
    ledger = surface_ledger_from_state(state)
    by_id = surface_by_id(ledger)
    surfaces = [by_id[sid] for sid in surface_ids_for_task(task, ledger) if sid in by_id]
    if not surfaces:
        target_files = {path.replace("\\", "/") for path in task.target_files}
        surfaces = [surface for surface in ledger if surface.file_path in target_files and surface.kind != "file"]
    added: List[ReviewCheck] = []
    for surface in surfaces[:2]:
        added.append(
            _surface_check_for_dimension(
                state=state,
                task=task,
                surface=surface,
                dimension="migration caller-reliance contract",
                index=start_index + len(added),
            )
        )
        lower = f"{surface.name} {surface.file_path}".lower()
        if any(token in lower for token in ("cache", "block", "slot", "state", "queue", "resource")):
            added.append(
                _surface_check_for_dimension(
                    state=state,
                    task=task,
                    surface=surface,
                    dimension="state/cache lifecycle migration contract",
                    index=start_index + len(added),
                )
            )
    return added


def _maintainability_floor_checks(
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    checks: Sequence[ReviewCheck],
    start_index: int,
) -> List[ReviewCheck]:
    if any(_check_covers_dimension(check, "maintainability contract") for check in checks):
        return []
    blob = "\n".join(
        [
            str(state.get("git_diff") or ""),
            task.title,
            task.description,
            str(slot.get("mental_model_excerpt") or ""),
            str(slot.get("review_kb_excerpt") or ""),
        ]
    ).lower()
    if not any(marker in blob for marker in ("maintainability", "readability", "doc", "comment", "typo", "spelling")):
        return []
    if not re.search(r"^\+.*(#|//|/\*|'''|\"\"\"|doc|string|comment|typo|spelling)", blob, re.MULTILINE):
        return []
    ledger = surface_ledger_from_state(state)
    by_id = surface_by_id(ledger)
    surface = next((by_id[sid] for sid in surface_ids_for_task(task, ledger) if sid in by_id), None)
    if surface is None:
        return []
    line_start = surface.line_start or 1
    return [
        ReviewCheck(
            check_id=f"{task.id}:coverage:{start_index}",
            patch_task_id=task.id,
            surface_ids=[surface.surface_id],
            lens="other",
            file_path=surface.file_path,
            line_start=line_start,
            line_end=surface.line_end or line_start,
            changed_code_anchor=surface.name,
            behavioral_question=f"Does the changed {surface.name} avoid concrete docs/comment/readability regressions?",
            affected_invariant="maintainability contract",
            required_evidence=[
                f"changed docs/comment/readability evidence for {surface.name}",
                "repository naming or documentation convention evidence when needed",
            ],
            suppress_criteria=["The changed text is correct, consistent, and non-misleading."],
            report_criteria=["The changed text is concretely wrong or misleading on the changed surface."],
            allowed_retrieval=["task_evidence"],
            budget=1,
        )
    ]


def _ensure_compiler_coverage_floor(
    *,
    state: GraphState,
    task: ReviewTask,
    checks: List[ReviewCheck],
) -> tuple[List[ReviewCheck], Dict[str, Any]]:
    slot = _pipeline_slot(state, task.id)
    obligations = _ranked_coverage_obligations(task, slot)
    uncovered_obligations = [
        obligation
        for obligation in obligations
        if not any(_check_covers_obligation(check, obligation) for check in checks)
    ]
    uncovered_for_floor = _rotate_tied_obligations(uncovered_obligations)
    added: List[ReviewCheck] = []
    skipped_due_to_cap: List[Dict[str, Any]] = []
    for obligation in uncovered_for_floor:
        if len(checks) + len(added) >= _MAX_CHECKS_PER_TASK:
            skipped_due_to_cap.append(dict(obligation))
            continue
        added.append(_coverage_check_for_obligation(state, task, obligation, len(checks) + len(added) + 1))

    deterministic_floor = [
        *_migration_floor_checks(state, task, slot, [*checks, *added], len(checks) + len(added) + 1),
        *_maintainability_floor_checks(state, task, slot, [*checks, *added], len(checks) + len(added) + 1),
    ]
    for check in deterministic_floor:
        if len(checks) + len(added) >= _MAX_CHECKS_PER_TASK:
            skipped_due_to_cap.append(
                {"file_path": check.file_path, "surface": check.changed_code_anchor, "dimension": check.affected_invariant}
            )
            continue
        if any(existing.check_id == check.check_id for existing in checks + added):
            check = check.model_copy(update={"check_id": f"{check.check_id}:{len(added) + 1}"})
        added.append(check)

    coverage_files = _changed_task_files(state, task)
    checked_files = {
        check.file_path.strip().replace("\\", "/")
        for check in checks + added
        if check.file_path.strip()
    }
    missing_files = [path for path in coverage_files if path not in checked_files]
    for file_path in missing_files:
        if len(checks) + len(added) >= _MAX_CHECKS_PER_TASK:
            skipped_due_to_cap.append(
                {"file_path": file_path, "surface": file_path, "dimension": "file coverage"}
            )
            continue
        added.append(_coverage_check_for_file(state, task, file_path, len(checks) + len(added) + 1))

    trimmed_existing: List[str] = []
    if len(checks) > _MAX_CHECKS_PER_TASK:
        trimmed_existing = [check.check_id for check in checks[_MAX_CHECKS_PER_TASK:]]
        checks = checks[:_MAX_CHECKS_PER_TASK]
        added = []

    warnings = [f"compiler_coverage_floor_added:{check.check_id}" for check in added]
    if skipped_due_to_cap:
        warnings.append(f"compiler_coverage_floor_cap_reached:{len(skipped_due_to_cap)}")
    return checks + added, {
        "coverage_files": coverage_files,
        "missed_files": missing_files,
        "ranked_obligations": [dict(item) for item in obligations],
        "uncovered_obligations": [dict(item) for item in uncovered_obligations],
        "added_checks": [check.model_dump(mode="json") for check in added],
        "added_coverage_checks": [check.check_id for check in added],
        "skipped_due_to_cap": skipped_due_to_cap,
        "trimmed_existing_check_ids": trimmed_existing,
        "max_checks": _MAX_CHECKS_PER_TASK,
        "warnings": warnings,
    }


def _behavioral_spec_from_state(state: GraphState, settings: Settings) -> BehavioralSpec | None:
    ref = state.get("behavioral_spec_ref")
    if not isinstance(ref, str) or not ref.strip():
        return None
    try:
        return BehavioralSpecStore(settings).read(ref)
    except Exception:  # noqa: BLE001
        return None


def _checks_from_surface_invariants(
    state: GraphState,
    task: ReviewTask,
    *,
    settings: Settings,
) -> List[ReviewCheck]:
    spec = _behavioral_spec_from_state(state, settings)
    if spec is None or not spec.surface_invariants:
        return []
    ledger = spec.surfaces or surface_ledger_from_state(state)
    by_id = surface_by_id(ledger)
    task_surface_ids = surface_ids_for_task(task, ledger)
    checks: List[ReviewCheck] = []
    for invariant in spec.surface_invariants:
        if invariant.surface_id not in task_surface_ids:
            continue
        surface = by_id.get(invariant.surface_id)
        if surface is None:
            continue
        line_start = surface.line_start or 1
        line_end = surface.line_end or line_start
        checks.append(
            ReviewCheck(
                check_id=f"{task.id}:surface:{len(checks) + 1}",
                patch_task_id=task.id,
                surface_ids=[surface.surface_id],
                lens=_dimension_to_lens(invariant.dimension),  # type: ignore[arg-type]
                file_path=surface.file_path,
                line_start=line_start,
                line_end=line_end,
                changed_code_anchor=surface.name,
                behavioral_question=(
                    f"Does the changed {surface.name} preserve {invariant.dimension}?"
                ),
                affected_invariant=invariant.expected_behavior[:400] or invariant.dimension,
                required_evidence=invariant.required_evidence
                or [f"changed implementation for {surface.name}"],
                suppress_criteria=[
                    f"Repository evidence shows {surface.name} preserves {invariant.dimension}."
                ],
                report_criteria=[
                    f"The changed {surface.name} violates {invariant.dimension} on a reachable path."
                ],
                allowed_retrieval=["task_evidence", "focused_context"],
                budget=2,
            )
        )
        if len(checks) >= _MAX_CHECKS_PER_TASK:
            break
    return checks


def _dedupe_checks(checks: Iterable[ReviewCheck]) -> List[ReviewCheck]:
    seen: set[str] = set()
    out: List[ReviewCheck] = []
    for check in checks:
        if check.check_id in seen:
            continue
        seen.add(check.check_id)
        out.append(check)
    return out


def _normalize_compiled_checks(
    state: GraphState,
    task: ReviewTask,
    checks: Iterable[ReviewCheck],
) -> List[ReviewCheck]:
    normalized: List[ReviewCheck] = []
    seen: set[str] = set()
    fallback_path = task.target_files[0] if task.target_files else ""
    ledger = surface_ledger_from_state(state)
    for index, check in enumerate(checks, start=1):
        cid = check.check_id.strip() or f"{task.id}:check:{index}"
        if not cid.startswith(task.id):
            cid = f"{task.id}:{cid}"
        if cid in seen:
            cid = f"{cid}:{index}"
        seen.add(cid)
        path = check.file_path.strip().replace("\\", "/") or fallback_path
        surface_ids = [sid for sid in check.surface_ids if sid in surface_by_id(ledger)]
        if ledger and not surface_ids:
            surface_ids = _surface_ids_for_check_context(
                task=task,
                file_path=path,
                anchor=f"{check.changed_code_anchor} {check.behavioral_question}",
                state=state,
            )
        anchor_update = _surface_anchor_update(surface_ids[:1], state)
        if anchor_update:
            path = str(anchor_update.get("file_path") or path)
        line_start = max(1, check.line_start)
        line_end = max(line_start, check.line_end)
        if anchor_update.get("line_start") and line_start == 1 and line_end == 1:
            line_start = int(anchor_update["line_start"])
            line_end = int(anchor_update.get("line_end") or line_start)
        normalized.append(
            check.model_copy(
                update={
                    "check_id": cid,
                    "patch_task_id": task.id,
                    "surface_ids": surface_ids,
                    "file_path": path,
                    "line_start": line_start,
                    "line_end": line_end,
                    "changed_code_anchor": str(
                        anchor_update.get("changed_code_anchor") or check.changed_code_anchor
                    ),
                }
            )
        )
    return normalized


def _render_compiler_prompt(state: GraphState, task: ReviewTask, slot: Mapping[str, Any]) -> str:
    ranked_obligations = _ranked_coverage_obligations(task, slot)
    ledger = surface_ledger_from_state(state)
    task_surface_ids = surface_ids_for_task(task, ledger) if ledger else task.surface_ids
    sections = {
        "Assigned Task": (
            f"Task ID: {task.id}\n"
            f"Title: {task.title}\n"
            f"Description: {task.description}\n"
            f"Specialty: {task.specialty}\n"
            f"Target files: {task.target_files}\n"
            f"Surface IDs: {task_surface_ids}"
        ),
        "Surface Ledger": compact_surface_ledger_json(ledger, max_records=40) if ledger else "[]",
        "Repository Code Evidence": str(slot.get("direct_context") or "")[:16000],
        "Mental Model Excerpt": str(slot.get("mental_model_excerpt") or ""),
        "Review KB Context": str(slot.get("review_kb_excerpt") or ""),
        "Mental Model Contract Material": "\n".join(
            f"- {line}" for line in _mental_model_contract_lines(slot)
        ),
        "Ranked Coverage Obligations": _json_for_prompt(ranked_obligations, max_chars=9000),
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
        checks: List[ReviewCheck] = _checks_from_surface_invariants(
            state,
            task,
            settings=resolved,
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
                llm_checks = _normalize_compiled_checks(state, task, response.checks)
                checks = _dedupe_checks([*checks, *llm_checks])
                summary = response.summary
                warnings.extend(response.warnings)
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}: {exc}")
                logger.warning("%s failed for task_id=%s: %s", node_name, task.id, exc)

        if not checks:
            checks = _fallback_checks(state, task, slot)
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


def _requires_external_evidence(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _EXTERNAL_EVIDENCE_MARKERS)


def _evidence_requirements_for_check(check: ReviewCheck) -> List[str]:
    items: List[str] = []
    items.extend(str(item).strip() for item in check.required_evidence if str(item).strip())
    for item in list(check.suppress_criteria) + list(check.report_criteria):
        text = str(item).strip()
        if text and _requires_external_evidence(text):
            items.append(text)
    if _requires_external_evidence(check.affected_invariant):
        items.append(check.affected_invariant)
    return list(dict.fromkeys(items))


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


def _missing_evidence_for_weak_no_finding(check: ReviewCheck) -> List[str]:
    requirements = _evidence_requirements_for_check(check)
    if requirements:
        return requirements[:3]
    return list(check.required_evidence[:3])


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
        if (
            result.decision == "no_finding"
            and _check_budget_remaining(state, check)
            and (not result.evidence_refs or not result.suppressing_evidence)
        ):
            warnings.append(f"executor_weak_no_finding_downgraded:{check.check_id}")
            result = result.model_copy(
                update={
                    "decision": "unsupported",
                    "missing_evidence": _missing_evidence_for_weak_no_finding(check),
                    "warnings": list(result.warnings) + ["weak_no_finding_requires_more_evidence"],
                }
            )
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


def _check_batches(checks: Sequence[ReviewCheck]) -> List[List[ReviewCheck]]:
    return [list(checks[index : index + _EXECUTOR_BATCH_SIZE]) for index in range(0, len(checks), _EXECUTOR_BATCH_SIZE)]


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
                    batch_results, norm_warnings = _normalize_executor_results(
                        state=state,
                        task=task,
                        slot=slot,
                        checks=batch,
                        results=response.results,
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
