"""Compiler and coverage-floor support for review-check nodes."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from src.config import Settings, get_settings
from src.domain.schemas import BehavioralSpec, ContractQuestion, ReviewCheck, ReviewSurface, ReviewTask
from src.domain.state import GraphState
from src.infrastructure.behavioral_spec_store import BehavioralSpecStore
from src.orchestration.context.contract_vocabulary import (
    COMPLETENESS_CONTRACT_TERMS,
    has_any_contract_term,
)
from src.orchestration.context.lens_cards import (
    LensCard,
    format_lens_cards,
    lens_card_selection_diagnostics,
    select_lens_cards,
)
from src.orchestration.context.surface_ledger import (
    compact_surface_ledger_json,
    surface_by_id,
    surface_ids_for_task,
    surface_ids_for_text,
    surface_ledger_from_state,
)
from src.orchestration.context.task_evidence import changed_lines_for_file
from src.orchestration.routing.claim_digest import owned_contract_scope_for_check
from src.orchestration.nodes.application.review_check_source_scope import (
    changed_task_files,
    compiled_check_is_source_local,
    coverage_meta_relevance,
    evidence_requirements_for_check,
    meaningful_tokens,
    requires_external_evidence,
    task_evidence_text,
    tokens_overlap,
)
from src.orchestration.prompts.renderer import render_reviewer_prompt

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

MAX_CHECKS_PER_TASK = 12
ADAPTIVE_MAX_CHECKS_PER_TASK = 16
CONTRACT_QUESTION_CHECK_GUARD = 32
_OWNER_FAIR_CAP_OWNER_THRESHOLD = 3

_PRIMARY_OWNER_SUFFIXES = {"execute", "run", "handle", "process", "call", "__call__"}
_HIGH_SIGNAL_FAMILY_ORDER = (
    "return_totality",
    "variant_completeness",
    "data_cardinality",
    "serialization_type",
    "error_boundary",
    "index_bounds",
    "aggregation",
)
_HIGH_SIGNAL_SWAP_FAMILIES = {
    "index_bounds",
    "data_cardinality",
    "serialization_type",
    "aggregation",
}
_STRUCTURED_SIGNAL_FAMILY_ALIASES = {
    "return_output_totality": "return_totality",
    "return_totality": "return_totality",
    "variant_completeness": "variant_completeness",
    "data_preservation_cardinality": "data_cardinality",
    "data_cardinality": "data_cardinality",
    "serialization_type_closure": "serialization_type",
    "serialization_type": "serialization_type",
    "error_boundary": "error_boundary",
    "index_bounds": "index_bounds",
    "aggregation": "aggregation",
    "data_shape_consistency": "data_cardinality",
    "api_compatibility": "return_totality",
    "state_transition": "variant_completeness",
    "error_propagation": "error_boundary",
}


QUESTION_DIMENSION_TO_LENS = {
    "variant_completeness": "state_transition",
    "return_output_totality": "api_compatibility",
    "data_preservation_cardinality": "data_shape_consistency",
    "serialization_type_closure": "data_shape_consistency",
    "error_boundary": "error_propagation",
    "lifecycle_state_ordering": "state_transition",
    "integration_compatibility": "api_compatibility",
    "resource_work_amplification": "resource_lifecycle",
    "other": "other",
}


def _question_scope_key(question: ContractQuestion) -> tuple[str, str, str, str, str]:
    return (
        question.owner.strip().lower(),
        question.dimension,
        question.trigger_variant.strip().lower(),
        question.operation.strip().lower(),
        question.breach_question.strip().lower(),
    )


def _question_preferred_specialty(question: ContractQuestion) -> str:
    if question.dimension == "resource_work_amplification":
        return "performance"
    if question.dimension == "integration_compatibility":
        return "general"
    if question.dimension == "other":
        return "general"
    return "logic"


def _task_registry_tasks(state: GraphState) -> List[ReviewTask]:
    raw = state.get("task_registry")
    if not isinstance(raw, Mapping):
        return []
    out: List[ReviewTask] = []
    for item in raw.values():
        if isinstance(item, ReviewTask):
            out.append(item)
        elif isinstance(item, Mapping):
            try:
                out.append(ReviewTask.model_validate(item))
            except Exception:  # noqa: BLE001
                continue
    return out


def _surface_fill_task_id(task_id: str) -> bool:
    return "surface-fill" in task_id


def _surface_owner_base(surface: ReviewSurface) -> str:
    return (surface.name or surface.surface_id).split(".", 1)[0].strip().lower()


def _primary_owner_maps(
    by_id: Mapping[str, ReviewSurface],
) -> tuple[Dict[tuple[str, str], ReviewSurface], Dict[str, str]]:
    grouped: Dict[tuple[str, str], List[ReviewSurface]] = {}
    for surface in by_id.values():
        if surface.kind == "file":
            continue
        key = (surface.file_path, _surface_owner_base(surface))
        grouped.setdefault(key, []).append(surface)

    primary_by_group: Dict[tuple[str, str], ReviewSurface] = {}
    label_by_key: Dict[str, str] = {}
    for key, surfaces in grouped.items():
        executable = [
            surface for surface in surfaces
            if surface.name.rsplit(".", 1)[-1].lower() in _PRIMARY_OWNER_SUFFIXES
        ]
        non_helper = [surface for surface in surfaces if "input_types" not in surface.name.lower()]
        primary = sorted(
            executable or non_helper or surfaces,
            key=lambda surface: (surface.line_start or 10**9, surface.name),
        )[0]
        primary_by_group[key] = primary
        label_by_key[primary.surface_id] = primary.name
    return primary_by_group, label_by_key


def _primary_owner_key_for_surface(
    surface: ReviewSurface,
    primary_by_group: Mapping[tuple[str, str], ReviewSurface],
) -> str:
    primary = primary_by_group.get((surface.file_path, _surface_owner_base(surface)))
    return primary.surface_id if primary is not None else surface.surface_id


def _primary_owner_keys_for_check(
    check: ReviewCheck,
    *,
    by_id: Mapping[str, ReviewSurface],
    primary_by_group: Mapping[tuple[str, str], ReviewSurface],
) -> List[str]:
    keys: List[str] = []
    seen: set[str] = set()
    for sid in check.surface_ids:
        surface = by_id.get(sid)
        if surface is None:
            continue
        key = _primary_owner_key_for_surface(surface, primary_by_group)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    if not keys and check.changed_code_anchor.strip():
        keys.append(check.changed_code_anchor.strip().lower())
    return keys


def _task_primary_owner_keys(
    task_surface_ids: Sequence[str],
    *,
    by_id: Mapping[str, ReviewSurface],
    primary_by_group: Mapping[tuple[str, str], ReviewSurface],
) -> List[str]:
    keys: List[str] = []
    seen: set[str] = set()
    for sid in task_surface_ids:
        surface = by_id.get(sid)
        if surface is None:
            continue
        key = _primary_owner_key_for_surface(surface, primary_by_group)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _task_owns_contract_question(
    state: GraphState,
    task: ReviewTask,
    question: ContractQuestion,
    ledger: Sequence[ReviewSurface],
) -> bool:
    if not question.surface_id:
        return True
    task_surface_ids = set(surface_ids_for_task(task, ledger))
    if question.surface_id not in task_surface_ids:
        return False
    registry_tasks = _task_registry_tasks(state)
    if not registry_tasks:
        return True
    preferred_specialty = _question_preferred_specialty(question)
    candidates = [
        candidate
        for candidate in registry_tasks
        if candidate.specialty == preferred_specialty
        and question.surface_id in set(surface_ids_for_task(candidate, ledger))
    ]
    if not candidates:
        candidates = [
            candidate
            for candidate in registry_tasks
            if question.surface_id in set(surface_ids_for_task(candidate, ledger))
        ]
    if not candidates:
        return True

    by_id = surface_by_id(ledger)

    def rank(candidate: ReviewTask) -> tuple[int, int, int, int, str]:
        surface_count = len([sid for sid in surface_ids_for_task(candidate, ledger) if sid in by_id])
        explicit_surface_owner = (
            0
            if _surface_fill_task_id(candidate.id) and question.surface_id in set(candidate.surface_ids)
            else 1
        )
        exact_surface = 0 if surface_count == 1 else 1
        specialty_match = 0 if candidate.specialty == preferred_specialty else 1
        baseline = 0 if candidate.id == f"review-{preferred_specialty}" else 1
        return (explicit_surface_owner, exact_surface, specialty_match, baseline, candidate.id)

    return min(candidates, key=rank).id == task.id


def _contract_question_surface_ids(spec: BehavioralSpec | None) -> set[str]:
    if spec is None:
        return set()
    return {
        question.surface_id
        for question in spec.contract_questions
        if question.surface_id
        and question.expected_behavior.strip()
        and question.breach_question.strip()
    }


def check_origin(
    check: ReviewCheck,
    origin_kind: str,
    origin_reason: str,
    meta: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    raw = dict(meta or {})
    out: Dict[str, Any] = {
        "origin_kind": origin_kind,
        "origin_reason": origin_reason,
        "check_id": check.check_id,
        "file_path": str(raw.get("file_path") or check.file_path),
        "surface": str(raw.get("surface") or check.changed_code_anchor),
        "dimension": str(raw.get("dimension") or check.affected_invariant),
    }
    if raw.get("relevance_score") is not None:
        out["relevance_score"] = raw.get("relevance_score")
    if raw.get("evidence") is not None:
        out["evidence"] = raw.get("evidence")
    return out


def origins_for_checks(
    checks: Iterable[ReviewCheck],
    origin_kind: str,
    origin_reason: str,
) -> Dict[str, Dict[str, Any]]:
    return {
        check.check_id: check_origin(check, origin_kind, origin_reason)
        for check in checks
    }


def json_for_prompt(value: Any, *, max_chars: int = 8000) -> str:
    try:
        text = json.dumps(value, indent=2, ensure_ascii=False)
    except TypeError:
        text = json.dumps(str(value), ensure_ascii=False)
    if len(text) > max_chars:
        return text[: max_chars - 24].rstrip() + "\n... [truncated]"
    return text


def _lens_text_from_slot(
    slot: Mapping[str, Any],
    contract_questions: Sequence[Mapping[str, Any]] = (),
) -> str:
    question_text = "\n".join(
        " ".join(
            str(question.get(key) or "")
            for key in (
                "owner",
                "dimension",
                "expected_behavior",
                "trigger_variant",
                "operation",
                "breach_question",
                "direct_suppressor",
            )
        )
        for question in contract_questions
        if isinstance(question, Mapping)
    )
    return "\n".join(
        part
        for part in (
            str(slot.get("direct_context") or ""),
            str(slot.get("mental_model_excerpt") or ""),
            str(slot.get("review_kb_excerpt") or ""),
            question_text,
        )
        if part.strip()
    )


def _lens_metadata(cards: Sequence[LensCard]) -> List[Dict[str, Any]]:
    return [
        {
            "key": card.key,
            "question": card.question,
            "contract_question_count": len(card.contract_questions),
            "counterexample_families": list(card.counterexample_families),
        }
        for card in cards
    ]


def checks_per_selected_lens(
    checks: Sequence[ReviewCheck],
    selected_keys: Sequence[str],
) -> Dict[str, int]:
    counts = {str(key): 0 for key in selected_keys if str(key).strip()}
    if not counts:
        return {}
    for check in checks:
        blob = " ".join(
            [
                check.owned_contract_scope,
                check.issue_family,
                check.diff_signal_family,
                check.diff_signal,
            ]
        ).lower()
        for key in counts:
            if key.lower() in blob:
                counts[key] += 1
    return counts


def dimension_to_lens(dimension: str) -> str:
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
    if (
        "structured" in dim
        or "index" in dim
        or "aggregation" in dim
        or "serialization" in dim
        or "cardinality" in dim
        or "completeness" in dim
        or "field" in dim
        or "element" in dim
    ):
        return "data_shape_consistency"
    if "concurrency" in dim or "ordering" in dim:
        return "concurrency_ordering"
    if "test" in dim:
        return "test_oracle_strength"
    return "other"


def surface_ids_for_check_context(
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


def surface_anchor_update(surface_ids: List[str], state: GraphState) -> Dict[str, Any]:
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


def narrow_surface_ids_to_anchor(
    surface_ids: List[str],
    *,
    anchor: str,
    question: str,
    by_id: Mapping[str, ReviewSurface],
) -> List[str]:
    if len(surface_ids) <= 1:
        return surface_ids
    valid = [sid for sid in surface_ids if sid in by_id]
    if len(valid) <= 1:
        return valid
    blob = f"{anchor} {question}".lower()
    concrete = [
        sid for sid in valid
        if by_id[sid].kind != "file" and by_id[sid].name.lower() not in {"execute", "input_types"}
    ]
    mentioned_concrete = [
        sid for sid in concrete
        if by_id[sid].name.lower() in blob
    ]
    if len(mentioned_concrete) == 1:
        return [mentioned_concrete[0]]
    if len(mentioned_concrete) > 1:
        return valid
    anchor_lower = anchor.strip().lower()
    exact = [sid for sid in concrete if by_id[sid].name.lower() == anchor_lower]
    if len(exact) == 1:
        return [exact[0]]
    if len(concrete) == 1:
        return [concrete[0]]
    return valid


def _check_text_is_cross_surface(check: ReviewCheck) -> bool:
    blob = f"{check.changed_code_anchor} {check.behavioral_question} {check.affected_invariant}".lower()
    return any(marker in blob for marker in ("cross-surface", "cross surface", "integration", "call path"))


def fallback_checks(state: GraphState, task: ReviewTask, slot: Mapping[str, Any]) -> List[ReviewCheck]:
    obligations = rotate_tied_obligations(ranked_coverage_obligations(task, slot))
    checks: List[ReviewCheck] = []
    for index, raw in enumerate(obligations[:6], start=1):
        row = raw if isinstance(raw, Mapping) else {}
        file_path = str(row.get("file_path") or (task.target_files[0] if task.target_files else ""))
        dimension = str(row.get("dimension") or "task contract")
        surface = str(row.get("surface") or file_path or "changed code")
        surface_ids = surface_ids_for_check_context(
            task=task,
            file_path=file_path,
            anchor=surface,
            state=state,
        )[:1]
        anchor_update = surface_anchor_update(surface_ids, state)
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
                lens=dimension_to_lens(dimension),  # type: ignore[arg-type]
                file_path=file_path,
                line_start=int(anchor_update.get("line_start") or 1),
                line_end=int(anchor_update.get("line_end") or anchor_update.get("line_start") or 1),
                changed_code_anchor=surface,
                behavioral_question=f"Does the changed {surface} preserve {dimension}?",
                affected_invariant=dimension,
                expected_behavior=str(row.get("expected_behavior") or dimension)[:500],
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
    surface_ids = surface_ids_for_check_context(
        task=task,
        file_path=file_path,
        anchor=task.title,
        state=state,
    )[:1]
    anchor_update = surface_anchor_update(surface_ids, state)
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
            expected_behavior=task.description[:500],
            required_evidence=["changed-code behavior at the task anchor"],
            suppress_criteria=["Task evidence shows the changed behavior is preserved."],
            report_criteria=["Changed code creates a concrete reachable regression."],
            allowed_retrieval=["task_evidence"],
            budget=1,
        )
    ]


def surface_coverage_check(state: GraphState, task: ReviewTask, surface: ReviewSurface, index: int) -> ReviewCheck:
    line_start = surface.line_start or 1
    line_end = surface.line_end or line_start
    return ReviewCheck(
        check_id=f"{task.id}:surface-coverage:{index}",
        patch_task_id=task.id,
        surface_ids=[surface.surface_id],
        lens="api_compatibility" if task.specialty == "general" else dimension_to_lens(task.review_dimension),
        file_path=surface.file_path,
        line_start=line_start,
        line_end=line_end,
        changed_code_anchor=surface.name,
        owned_contract_scope=f"{surface.name}:audit_surface_coverage",
        issue_family="surface_fallback",
        diff_signal_family="surface_fallback",
        diff_signal="broad surface coverage fallback",
        audit_only=True,
        behavioral_question=f"Does the changed {surface.name} preserve its assigned surface behavior?",
        affected_invariant=(
            f"{surface.name} in {surface.file_path} preserves the behavior targeted by {task.title}."
        ),
        expected_behavior=f"{surface.name} preserves the behavior targeted by {task.title}.",
        required_evidence=[
            f"changed implementation for {surface.name}",
            "repository contract or local caller evidence when the local code is insufficient",
        ],
        suppress_criteria=[
            f"Repository evidence shows {surface.name} preserves the assigned behavior."
        ],
        report_criteria=[
            f"The changed {surface.name} violates the assigned behavior on a reachable path."
        ],
        allowed_retrieval=["task_evidence", "focused_context"],
        budget=2,
    )


def coverage_check_for_file(state: GraphState, task: ReviewTask, file_path: str, index: int) -> ReviewCheck:
    surface_ids = surface_ids_for_check_context(
        task=task,
        file_path=file_path,
        anchor=file_path,
        state=state,
    )[:1]
    anchor_update = surface_anchor_update(surface_ids, state)
    return ReviewCheck(
        check_id=f"{task.id}:coverage:{index}",
        patch_task_id=task.id,
        surface_ids=surface_ids,
        lens="other",
        file_path=str(anchor_update.get("file_path") or file_path),
        line_start=int(anchor_update.get("line_start") or 1),
        line_end=int(anchor_update.get("line_end") or anchor_update.get("line_start") or 1),
        changed_code_anchor=str(anchor_update.get("changed_code_anchor") or file_path),
        owned_contract_scope=f"{file_path}:audit_file_coverage",
        issue_family="file_fallback",
        diff_signal_family="file_fallback",
        diff_signal="broad file coverage fallback",
        audit_only=True,
        behavioral_question=(
            f"Does the changed code in {file_path} preserve the task-specific behavior for {task.title}?"
        ),
        affected_invariant=task.description[:400] or task.title,
        expected_behavior=(task.description or task.title)[:500],
        required_evidence=[
            f"changed behavior in {file_path}",
            "caller, contract, or runtime path needed to decide the changed behavior",
        ],
        suppress_criteria=[f"Repository evidence shows the changed behavior in {file_path} is preserved."],
        report_criteria=[f"The changed behavior in {file_path} creates a concrete reachable regression."],
        allowed_retrieval=["task_evidence", "focused_context"],
        budget=2,
    )


def coverage_obligations(slot: Mapping[str, Any]) -> List[Dict[str, Any]]:
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
        family = str(raw.get("diff_signal_family") or raw.get("issue_family") or "").strip()
        row["issue_family"] = family
        row["diff_signal_family"] = family
        row["diff_signal"] = str(raw.get("diff_signal") or "").strip()
        row["operation_markers"] = [
            str(item).strip() for item in raw.get("operation_markers", []) if str(item).strip()
        ] if isinstance(raw.get("operation_markers"), list) else []
        if raw.get("line_start"):
            row["line_start"] = raw.get("line_start")
            row["line_end"] = raw.get("line_end") or raw.get("line_start")
        obligations.append(row)
    return obligations


def mental_model_contract_lines(slot: Mapping[str, Any]) -> List[str]:
    text = "\n".join(
        str(slot.get(key) or "")
        for key in ("mental_model_excerpt", "review_kb_excerpt")
    )
    lines: List[str] = []
    for raw in text.splitlines():
        line = re.sub(r"^[-*#\s]+", "", raw).strip()
        if not line:
            continue
        if line.lower() in {"intent", "behavior", "contracts", "risks", "guidance", "uncertainties"}:
            continue
        lines.append(line[:300])
        if len(lines) >= 8:
            break
    return lines


def _has_completeness_contract_signal(text: str) -> bool:
    return has_any_contract_term(" ".join(meaningful_tokens(text)), COMPLETENESS_CONTRACT_TERMS)


def completeness_contract_lines(slot: Mapping[str, Any]) -> List[str]:
    lines = [
        line for line in mental_model_contract_lines(slot)
        if _has_completeness_contract_signal(line)
    ]
    return lines[:3]


def _check_accepts_completeness_material(check: ReviewCheck) -> bool:
    blob = " ".join(
        [
            check.lens,
            check.behavioral_question,
            check.affected_invariant,
            " ".join(check.required_evidence),
            " ".join(check.report_criteria),
        ]
    )
    if check.lens in {"data_shape_consistency", "api_compatibility"}:
        return True
    return _has_completeness_contract_signal(blob) or any(
        marker in blob.lower()
        for marker in ("aggregate", "aggregation", "join", "serialize", "template", "format")
    )


def enrich_checks_with_completeness_contracts(
    checks: Iterable[ReviewCheck],
    *,
    slot: Mapping[str, Any],
) -> List[ReviewCheck]:
    material = completeness_contract_lines(slot)
    if not material:
        return list(checks)

    enriched: List[ReviewCheck] = []
    for check in checks:
        matching_lines = [
            line for line in material
            if (
                tokens_overlap(check.changed_code_anchor, line)
                or tokens_overlap(check.affected_invariant, line)
                or tokens_overlap(check.behavioral_question, line)
                or (check.file_path and check.file_path in line)
            )
        ]
        if not matching_lines or not _check_accepts_completeness_material(check):
            enriched.append(check)
            continue
        line = matching_lines[0]
        required = [
            *check.required_evidence,
            f"mental-model completeness/cardinality contract: {line}",
        ]
        suppress = [
            *check.suppress_criteria,
            "Concrete evidence shows the relevant elements, fields, paths, or cardinality are preserved or intentionally narrowed.",
        ]
        report = [
            *check.report_criteria,
            "Report if the changed behavior selects, skips, drops, replaces, or serializes only part of the relevant structured data where the contract requires preserving it.",
        ]
        enriched.append(
            check.model_copy(
                update={
                    "required_evidence": list(dict.fromkeys(required)),
                    "suppress_criteria": list(dict.fromkeys(suppress)),
                    "report_criteria": list(dict.fromkeys(report)),
                }
            )
        )
    return enriched


def relevance_context(task: ReviewTask, slot: Mapping[str, Any]) -> Dict[str, str]:
    return {
        "task": f"{task.title} {task.description} {task.specialty}",
        "mental_model": str(slot.get("mental_model_excerpt") or ""),
        "review_kb": str(slot.get("review_kb_excerpt") or ""),
    }


def score_obligation_relevance(
    task: ReviewTask,
    slot: Mapping[str, Any],
    obligation: Mapping[str, Any],
) -> tuple[int, List[str], List[str]]:
    surface = str(obligation.get("surface") or "")
    dimension = str(obligation.get("dimension") or "")
    evidence = str(obligation.get("evidence") or "")
    contexts = relevance_context(task, slot)
    score = 1
    reasons: List[str] = ["baseline_changed_surface"]

    if tokens_overlap(surface, contexts["task"]):
        score += 3
        reasons.append("surface_in_task")
    if tokens_overlap(surface, contexts["mental_model"]):
        score += 4
        reasons.append("surface_in_mental_model")
    if tokens_overlap(surface, contexts["review_kb"]):
        score += 3
        reasons.append("surface_in_review_kb")
    if tokens_overlap(dimension, contexts["mental_model"]):
        score += 4
        reasons.append("dimension_text_in_mental_model")
    if tokens_overlap(dimension, contexts["review_kb"]):
        score += 3
        reasons.append("dimension_text_in_review_kb")
    if tokens_overlap(dimension, contexts["task"]):
        score += 2
        reasons.append("dimension_text_in_task")
    if tokens_overlap(evidence, contexts["task"]):
        score += 1
        reasons.append("evidence_matches_task")
    if tokens_overlap(evidence, contexts["mental_model"]) or tokens_overlap(evidence, contexts["review_kb"]):
        score += 1
        reasons.append("evidence_matches_context")

    material: List[str] = []
    for line in mental_model_contract_lines(slot):
        if (
            tokens_overlap(surface, line)
            or tokens_overlap(dimension, line)
            or tokens_overlap(evidence, line)
        ):
            material.append(line)
        if len(material) >= 2:
            break
    return score, reasons, material


def ranked_coverage_obligations(task: ReviewTask, slot: Mapping[str, Any]) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    for index, obligation in enumerate(coverage_obligations(slot)):
        row = dict(obligation)
        score, reasons, material = score_obligation_relevance(task, slot, row)
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


def surface_key(obligation: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(obligation.get("file_path") or "").replace("\\", "/"),
        str(obligation.get("surface") or ""),
    )


def rotate_tied_obligations(obligations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered: List[Dict[str, Any]] = []
    scores = sorted(
        {int(row.get("relevance_score") or 0) for row in obligations},
        reverse=True,
    )
    for score in scores:
        bucket = [row for row in obligations if int(row.get("relevance_score") or 0) == score]
        groups: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
        for row in bucket:
            groups.setdefault(surface_key(row), []).append(row)
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


def check_covers_obligation(check: ReviewCheck, obligation: Mapping[str, Any]) -> bool:
    file_path = str(obligation.get("file_path") or "").strip().replace("\\", "/")
    if check.file_path.strip().replace("\\", "/") != file_path:
        return False
    surface = str(obligation.get("surface") or "")
    dimension = str(obligation.get("dimension") or "")
    if check_is_broad_surface_invariant(check):
        return False
    blob = " ".join(
        [
            check.changed_code_anchor,
            check.behavioral_question,
            check.affected_invariant,
            " ".join(check.required_evidence),
            " ".join(check.report_criteria),
        ]
    )
    surface_ok = not surface.strip() or tokens_overlap(surface, blob) or surface.lower() in blob.lower()
    dimension_ok = tokens_overlap(dimension, blob) or dimension.lower() in blob.lower()
    return surface_ok and dimension_ok


def coverage_obligation_is_concrete(obligation: Mapping[str, Any]) -> bool:
    file_path = str(obligation.get("file_path") or "").strip()
    surface = str(obligation.get("surface") or "").strip()
    dimension = str(obligation.get("dimension") or "").strip()
    evidence = str(obligation.get("evidence") or "").strip()
    operation_markers = obligation.get("operation_markers")
    material = obligation.get("mental_model_contract_material")
    has_contract_material = (
        isinstance(operation_markers, list) and any(str(item).strip() for item in operation_markers)
    ) or (
        isinstance(material, list) and any(str(item).strip() for item in material)
    )
    signal = " ".join(
        str(obligation.get(key) or "")
        for key in ("diff_signal_family", "issue_family", "diff_signal")
    )
    blob = " ".join([dimension, evidence, signal, surface]).lower()
    concrete_terms = (
        "declared",
        "schema",
        "mode",
        "option",
        "return",
        "output",
        "field",
        "element",
        "group",
        "serialize",
        "caller",
        "contract",
        "branch",
        "type",
    )
    if obligation.get("files_complete") and file_path and surface and dimension and evidence:
        return True
    return bool(file_path and surface and dimension and evidence and (has_contract_material or signal.strip())) and any(
        term in blob for term in concrete_terms
    )


def coverage_check_for_obligation(
    state: GraphState,
    task: ReviewTask,
    obligation: Mapping[str, Any],
    index: int,
) -> ReviewCheck:
    file_path = str(obligation.get("file_path") or (task.target_files[0] if task.target_files else ""))
    surface = str(obligation.get("surface") or file_path or "changed code")
    dimension = str(obligation.get("dimension") or "task contract")
    evidence = str(obligation.get("evidence") or f"repository evidence for {dimension}")
    signal_family = str(obligation.get("diff_signal_family") or obligation.get("issue_family") or "").strip()
    issue_family = signal_family
    diff_signal = str(obligation.get("diff_signal") or evidence).strip()
    operation_markers = [
        str(item).strip()
        for item in obligation.get("operation_markers", [])
        if str(item).strip()
    ] if isinstance(obligation.get("operation_markers"), list) else []
    surface_ids = surface_ids_for_check_context(
        task=task,
        file_path=file_path,
        anchor=surface,
        state=state,
    )[:1]
    anchor_update = surface_anchor_update(surface_ids, state)
    file_path = str(anchor_update.get("file_path") or file_path)
    surface = str(anchor_update.get("changed_code_anchor") or surface)
    line_start = int(
        anchor_update.get("line_start")
        or obligation.get("line_start")
        or 1
    )
    line_end = int(
        anchor_update.get("line_end")
        or obligation.get("line_end")
        or line_start
    )
    contract_material = [
        f"mental model/KB contract hypothesis to verify: {item}"
        for item in obligation.get("mental_model_contract_material", [])
        if str(item).strip()
    ][:2]
    cardinality = signal_family == "aggregation_cardinality" or "structured" in dimension.lower()
    if cardinality:
        expected_behavior = (
            f"{surface} preserves each intended field, element, group, nested value, and cardinality "
            "for this aggregation/cardinality path unless the changed contract intentionally narrows it."
        )
        question = (
            f"Does the changed {surface} preserve each intended field, element, group, or nested value "
            "for this aggregation/cardinality path?"
        )
        report = (
            f"The changed {surface} selects, skips, drops, truncates, or serializes only part of the "
            "intended structured value without an intentional narrowing contract."
        )
        suppress = (
            f"Concrete evidence shows {surface} preserves the relevant fields/elements/groups, "
            "or documents an intentional narrowing at the changed contract."
        )
    else:
        expected_behavior = f"{surface} preserves {dimension}."
        question = f"Does the changed {surface} preserve {dimension}?"
        report = f"The changed {surface} violates {dimension} on a concrete reachable path."
        suppress = f"Concrete repository evidence shows {surface} preserves {dimension}."
    return ReviewCheck(
        check_id=f"{task.id}:coverage:{index}",
        patch_task_id=task.id,
        surface_ids=surface_ids,
        lens=dimension_to_lens(dimension),  # type: ignore[arg-type]
        file_path=file_path,
        line_start=max(1, line_start),
        line_end=max(max(1, line_start), line_end),
        changed_code_anchor=surface,
        owned_contract_scope=f"{surface}:{issue_family or dimension}:{diff_signal}"[:240],
        issue_family=issue_family,
        diff_signal_family=signal_family,
        diff_signal=diff_signal[:240],
        audit_only=not coverage_obligation_is_concrete(obligation),
        behavioral_question=question,
        affected_invariant=dimension,
        expected_behavior=expected_behavior,
        required_evidence=[
            evidence,
            *(operation_markers[:4] if operation_markers else []),
            *contract_material,
            f"changed behavior of {surface} in {file_path}",
            "caller, contract, framework, or repository-convention evidence if the local code is not enough",
        ],
        suppress_criteria=[suppress],
        report_criteria=[report],
        allowed_retrieval=["task_evidence", "focused_context"],
        budget=2,
    )


def migration_context_present(state: GraphState, task: ReviewTask, slot: Mapping[str, Any]) -> bool:
    git_diff = str(state.get("git_diff") or "")
    if not _diff_has_removed_or_renamed_evidence(git_diff):
        return False
    blob = "\n".join(
        [
            git_diff,
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


def _diff_has_removed_or_renamed_evidence(git_diff: str) -> bool:
    for raw in (git_diff or "").splitlines():
        line = raw.rstrip()
        if line.startswith(("rename from ", "rename to ", "deleted file mode ")):
            return True
        if line.startswith("-") and not line.startswith("---") and line[1:].strip():
            return True
    return False


def check_covers_dimension(check: ReviewCheck, dimension: str) -> bool:
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
    tokens = sorted(meaningful_tokens(dimension))
    return bool(tokens) and all(token in blob for token in tokens[:2])


def surface_check_for_dimension(
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
        lens=dimension_to_lens(dimension),  # type: ignore[arg-type]
        file_path=surface.file_path,
        line_start=line_start,
        line_end=line_end,
        changed_code_anchor=surface.name,
        behavioral_question=question,
        affected_invariant=dimension,
        expected_behavior=f"{surface.name} preserves {dimension}.",
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


def migration_floor_checks(
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    checks: Sequence[ReviewCheck],
    start_index: int,
) -> List[ReviewCheck]:
    if not migration_context_present(state, task, slot):
        return []
    if any(check_covers_dimension(check, "migration caller-reliance contract") for check in checks):
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
            surface_check_for_dimension(
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
                surface_check_for_dimension(
                    state=state,
                    task=task,
                    surface=surface,
                    dimension="state/cache lifecycle migration contract",
                    index=start_index + len(added),
                )
            )
    return added


def maintainability_floor_checks(
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    checks: Sequence[ReviewCheck],
    start_index: int,
) -> List[ReviewCheck]:
    if any(check_covers_dimension(check, "maintainability contract") for check in checks):
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
            expected_behavior=f"{surface.name} keeps changed docs/comments/text correct, consistent, and non-misleading.",
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


_BROAD_SURFACE_INVARIANTS = (
    "preserves its existing observable contract",
    "preserve changed-surface behavior",
    "preserve api contract",
    "preserves caller-visible inputs, outputs, and exception behavior",
    "assigned surface behavior",
)


_IMPLEMENTATION_EXPECTATION_RE = re.compile(
    r"(`[^`]+`|\bif\b|\belif\b|\belse\b|\bfor\b|\bwhile\b|[A-Za-z_][A-Za-z0-9_]*\s*\(|[=!<>]=|:=|\[[^\]]*\])"
)


def _invariant_check_is_audit_only(invariant: Any) -> bool:
    dimension = str(getattr(invariant, "dimension", "") or "").strip().lower()
    expected = str(getattr(invariant, "expected_behavior", "") or "").strip().lower()
    return dimension in {"changed-surface behavior", "api contract"} or any(
        marker in expected for marker in _BROAD_SURFACE_INVARIANTS
    )


def _expected_behavior_is_implementation_shaped(check: ReviewCheck) -> bool:
    if check.diff_signal_family == "contract_question":
        return False
    text = check.expected_behavior.strip()
    if not text:
        return False
    return bool(_IMPLEMENTATION_EXPECTATION_RE.search(text))


def check_is_broad_surface_invariant(check: ReviewCheck) -> bool:
    blob = " ".join(
        [
            check.check_id,
            check.behavioral_question,
            check.affected_invariant,
            " ".join(check.suppress_criteria),
            " ".join(check.report_criteria),
        ]
    ).lower()
    return any(marker in blob for marker in _BROAD_SURFACE_INVARIANTS)


def check_is_concrete_source_local_behavior(
    check: ReviewCheck,
    *,
    slot: Mapping[str, Any],
    task_files: set[str],
) -> bool:
    if not compiled_check_is_source_local(
        check,
        None,
        task_evidence_text(slot),
        task_files,
        evidence_requirements_for_check(check),
    ):
        return False
    return check_has_concrete_behavior_terms(check)


def check_has_concrete_behavior_terms(check: ReviewCheck) -> bool:
    cid = check.check_id.lower()
    if ":surface:" in cid or ":surface-coverage:" in cid:
        return False
    blob = " ".join(
        [
            check.behavioral_question,
            check.affected_invariant,
            " ".join(check.required_evidence),
            " ".join(check.report_criteria),
        ]
    ).lower()
    if any(marker in blob for marker in _BROAD_SURFACE_INVARIANTS):
        return False
    return any(
        marker in blob
        for marker in (
            "declared",
            "option",
            "branch",
            "return",
            "shape",
            "type",
            "data",
            "side effect",
            "state",
            "input",
            "output",
            "exception",
            "wrong",
            "crash",
        )
    )


def uncovered_surface_behavior_checks(
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    checks: Sequence[ReviewCheck],
    start_index: int,
    task_surface_ids: Sequence[str],
    by_id: Mapping[str, ReviewSurface],
) -> List[ReviewCheck]:
    task_files = {path.replace("\\", "/") for path in changed_task_files(state, task)}
    concrete_surface_ids: set[str] = set()
    for check in checks:
        source_local = check_is_concrete_source_local_behavior(check, slot=slot, task_files=task_files)
        if not source_local and not check_has_concrete_behavior_terms(check):
            continue
        if source_local:
            concrete_surface_ids.update(sid for sid in check.surface_ids if sid in by_id)
        check_file = check.file_path.strip().replace("\\", "/")
        check_anchor = check.changed_code_anchor.lower()
        for sid, surface in by_id.items():
            if surface.file_path.strip().replace("\\", "/") != check_file:
                continue
            surface_name = surface.name.lower()
            line_start = surface.line_start or 1
            line_end = surface.line_end or line_start
            lines_overlap = check.line_start <= line_end and check.line_end >= line_start
            names_overlap = bool(surface_name) and (
                surface_name in check_anchor or check_anchor in surface_name
            )
            if lines_overlap or names_overlap:
                concrete_surface_ids.add(sid)
    added: List[ReviewCheck] = []
    for sid in task_surface_ids:
        if sid in concrete_surface_ids:
            continue
        surface = by_id.get(sid)
        if surface is None:
            continue
        line_start = surface.line_start or 1
        line_end = surface.line_end or line_start
        added.append(
            ReviewCheck(
                check_id=f"{task.id}:uncovered-behavior:{start_index + len(added)}",
                patch_task_id=task.id,
                surface_ids=[surface.surface_id],
                lens="data_shape_consistency",
                file_path=surface.file_path,
                line_start=line_start,
                line_end=line_end,
                changed_code_anchor=surface.name,
                audit_only=True,
                behavioral_question=(
                    f"Does the changed {surface.name} have any reachable mismatch between declared "
                    "inputs/options and branch behavior, return shape, data shape, or local side effects?"
                ),
                affected_invariant="source-local changed behavior consistency",
                expected_behavior=(
                    f"{surface.name} keeps declared inputs/options, reachable branch behavior, "
                    "return shape, data shape, and local side effects internally consistent."
                ),
                required_evidence=[
                    f"changed implementation for {surface.name}",
                    "declared inputs/options, branch bodies, return shape, data shape, and local side effects",
                ],
                suppress_criteria=[
                    "Concrete source evidence shows declared inputs/options and reachable behavior are consistent."
                ],
                report_criteria=[
                    "Concrete source evidence shows a reachable wrong output, crash, data loss, or contract mismatch."
                ],
                allowed_retrieval=["task_evidence", "focused_context"],
                budget=2,
            )
        )
        if len(added) >= 2:
            break
    return added


def omitted_prompt_files(slot: Mapping[str, Any], coverage_files: Sequence[str]) -> List[str]:
    te = slot.get("task_evidence") if isinstance(slot.get("task_evidence"), dict) else {}
    raw = te.get("omitted_prompt_files") if isinstance(te.get("omitted_prompt_files"), list) else []
    coverage = {path.replace("\\", "/") for path in coverage_files}
    out: List[str] = []
    seen: set[str] = set()
    for item in raw:
        path = str(item or "").strip().replace("\\", "/")
        if not path or path in seen:
            continue
        if coverage and path not in coverage:
            continue
        seen.add(path)
        out.append(path)
    return out


def surface_intersects_changed_lines(surface: ReviewSurface, changed_lines: set[int]) -> bool:
    if not changed_lines:
        return False
    line_start = surface.line_start or 0
    line_end = surface.line_end or line_start
    if line_start < 1:
        return False
    return any(line_start <= line <= line_end for line in changed_lines)


def omitted_file_surface_check(
    task: ReviewTask,
    surface: ReviewSurface,
    index: int,
) -> ReviewCheck:
    line_start = surface.line_start or 1
    line_end = surface.line_end or line_start
    return ReviewCheck(
        check_id=f"{task.id}:omitted-surface:{index}",
        patch_task_id=task.id,
        surface_ids=[surface.surface_id],
        lens="data_shape_consistency",
        file_path=surface.file_path,
        line_start=line_start,
        line_end=line_end,
        changed_code_anchor=surface.name,
        behavioral_question=(
            f"Does the changed {surface.name} in omitted prompt file {surface.file_path} "
            "have any reachable mismatch in inputs, branch behavior, return shape, data shape, or local side effects?"
        ),
        affected_invariant="source-local changed behavior consistency for omitted prompt file",
        expected_behavior=(
            f"{surface.name} keeps declared inputs/options, reachable branch behavior, "
            "return shape, data shape, and local side effects internally consistent."
        ),
        required_evidence=[
            f"focused changed implementation for {surface.name}",
            "declared inputs/options, branch bodies, return shape, data shape, and local side effects",
        ],
        suppress_criteria=[
            "Focused source evidence shows the omitted prompt surface preserves reachable local behavior."
        ],
        report_criteria=[
            "Focused source evidence shows a reachable wrong output, crash, data loss, or contract mismatch."
        ],
        allowed_retrieval=["focused_context", "task_evidence"],
        budget=2,
    )


def omitted_file_behavior_check(
    state: GraphState,
    task: ReviewTask,
    file_path: str,
    index: int,
) -> ReviewCheck:
    check = coverage_check_for_file(state, task, file_path, index)
    return check.model_copy(
        update={
            "check_id": f"{task.id}:omitted-file:{index}",
            "lens": "data_shape_consistency",
            "changed_code_anchor": file_path,
            "behavioral_question": (
                f"Does the changed code in omitted prompt file {file_path} have any reachable mismatch "
                "in inputs, branch behavior, return shape, data shape, or local side effects?"
            ),
            "affected_invariant": "source-local changed behavior consistency for omitted prompt file",
            "expected_behavior": (
                f"{file_path} keeps changed prompt-file behavior, return shape, data shape, "
                "and local side effects internally consistent."
            ),
            "required_evidence": [
                f"focused changed implementation in {file_path}",
                "declared inputs/options, branch bodies, return shape, data shape, and local side effects",
            ],
            "suppress_criteria": [
                f"Focused source evidence shows changed code in {file_path} preserves reachable local behavior."
            ],
            "report_criteria": [
                f"Focused source evidence shows changed code in {file_path} creates a reachable regression."
            ],
            "allowed_retrieval": ["focused_context", "task_evidence"],
            "budget": 2,
        }
    )


def mandatory_omitted_file_checks(
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    coverage_files: Sequence[str],
    by_id: Mapping[str, ReviewSurface],
    start_index: int,
) -> List[ReviewCheck]:
    omitted_files = omitted_prompt_files(slot, coverage_files)
    if not omitted_files:
        return []
    git_diff = str(state.get("git_diff") or "")
    added: List[ReviewCheck] = []
    for file_path in omitted_files:
        changed_lines = changed_lines_for_file(git_diff, file_path)
        surfaces = [
            surface for surface in by_id.values()
            if surface.file_path.replace("\\", "/") == file_path
            and surface.confidence >= 0.75
            and surface.kind != "file"
            and surface_intersects_changed_lines(surface, changed_lines)
        ]
        if surfaces:
            for surface in sorted(surfaces, key=lambda item: (item.line_start or 10**9, item.name)):
                added.append(omitted_file_surface_check(task, surface, start_index + len(added)))
            continue
        added.append(omitted_file_behavior_check(state, task, file_path, start_index + len(added)))
    return added


def _origin_reason(origin_kind: str) -> str:
    return {
        "llm_compiled": "compiled_by_review_check_llm",
        "contract_question": "derived_from_behavioral_contract_question",
        "surface_invariant": "derived_from_behavioral_surface_invariant",
        "deterministic_fallback": "deterministic_fallback_from_task_evidence",
        "coverage_obligation": "added_for_uncovered_coverage_obligation",
        "deterministic_floor": "added_by_deterministic_review_floor",
        "mandatory_omitted_file": "added_for_changed_file_omitted_from_prompt",
        "uncovered_surface_behavior": "added_for_uncovered_source_local_surface_behavior",
        "surface_coverage": "added_for_missing_primary_surface_coverage",
        "file_coverage": "added_for_changed_file_without_check",
    }.get(origin_kind, origin_kind)


def ensure_compiler_coverage_floor(
    *,
    state: GraphState,
    task: ReviewTask,
    checks: List[ReviewCheck],
    check_origins: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[List[ReviewCheck], Dict[str, Any], Dict[str, Dict[str, Any]]]:
    slot = pipeline_slot(state, task.id)
    obligations = ranked_coverage_obligations(task, slot)
    uncovered_obligations = [
        obligation
        for obligation in obligations
        if not any(check_covers_obligation(check, obligation) for check in checks)
    ]
    uncovered_for_floor = rotate_tied_obligations(uncovered_obligations)
    added_candidates: List[tuple[ReviewCheck, Dict[str, Any], str]] = []
    for obligation in uncovered_for_floor:
        check = coverage_check_for_obligation(
            state,
            task,
            obligation,
            len(checks) + len(added_candidates) + 1,
        )
        added_candidates.append((check, dict(obligation), "coverage_obligation"))

    deterministic_floor = [
        *migration_floor_checks(
            state,
            task,
            slot,
            [*checks, *(check for check, _meta, _kind in added_candidates)],
            len(checks) + len(added_candidates) + 1,
        ),
        *maintainability_floor_checks(
            state,
            task,
            slot,
            [*checks, *(check for check, _meta, _kind in added_candidates)],
            len(checks) + len(added_candidates) + 1,
        ),
    ]
    for check in deterministic_floor:
        if any(
            existing.check_id == check.check_id
            for existing in [*checks, *(candidate for candidate, _meta, _kind in added_candidates)]
        ):
            check = check.model_copy(update={"check_id": f"{check.check_id}:{len(added_candidates) + 1}"})
        added_candidates.append(
            (
                check,
                {
                    "file_path": check.file_path,
                    "surface": check.changed_code_anchor,
                    "dimension": check.affected_invariant,
                },
                "deterministic_floor",
            )
        )

    coverage_files = changed_task_files(state, task)
    ledger = surface_ledger_from_state(state)
    by_id = surface_by_id(ledger)
    task_surface_ids = [
        sid for sid in surface_ids_for_task(task, ledger)
        if sid in by_id and by_id[sid].confidence >= 0.75 and by_id[sid].kind != "file"
    ]
    mandatory_omitted = mandatory_omitted_file_checks(
        state,
        task,
        slot,
        coverage_files,
        by_id,
        len(checks) + len(added_candidates) + 1,
    )
    for check in mandatory_omitted:
        added_candidates.append(
            (
                check,
                {"file_path": check.file_path, "surface": check.changed_code_anchor, "dimension": "omitted prompt file"},
                "mandatory_omitted_file",
            )
        )
    behavior_floor = uncovered_surface_behavior_checks(
        state,
        task,
        slot,
        [*checks, *(candidate for candidate, _meta, _kind in added_candidates)],
        len(checks) + len(added_candidates) + 1,
        task_surface_ids,
        by_id,
    )
    for check in behavior_floor:
        added_candidates.append(
            (
                check,
                {"file_path": check.file_path, "surface": check.changed_code_anchor, "dimension": "uncovered behavior"},
                "uncovered_surface_behavior",
            )
        )
    covered_surface_ids = {
        sid
        for check in [*checks, *(candidate for candidate, _meta, _kind in added_candidates)]
        for sid in check.surface_ids
        if sid in by_id
    }
    missing_surface_ids = [sid for sid in task_surface_ids if sid not in covered_surface_ids]
    for sid in missing_surface_ids:
        surface = by_id[sid]
        check = surface_coverage_check(state, task, surface, len(checks) + len(added_candidates) + 1)
        added_candidates.append(
            (
                check,
                {"file_path": surface.file_path, "surface": surface.name, "dimension": "surface coverage"},
                "surface_coverage",
            )
        )

    checked_files = {
        check.file_path.strip().replace("\\", "/")
        for check in [*checks, *(candidate for candidate, _meta, _kind in added_candidates)]
        if check.file_path.strip()
    }
    missing_files = [path for path in coverage_files if path not in checked_files]
    for file_path in missing_files:
        check = coverage_check_for_file(state, task, file_path, len(checks) + len(added_candidates) + 1)
        added_candidates.append(
            (
                check,
                {"file_path": file_path, "surface": file_path, "dimension": "file coverage"},
                "file_coverage",
            )
        )

    original_ids = {check.check_id for check in checks}
    added_origin_by_id = {
        check.check_id: check_origin(check, kind, _origin_reason(kind), meta)
        for check, meta, kind in added_candidates
    }
    added_by_id = {
        check.check_id: {**meta, "origin_kind": kind, "origin_reason": _origin_reason(kind)}
        for check, meta, kind in added_candidates
    }
    ranking_meta_by_id = {
        check.check_id: {**meta, "_floor_kind": kind, "origin_kind": kind}
        for check, meta, kind in added_candidates
    }
    ranked = prioritize_compiled_checks(
        dedupe_checks([*checks, *(check for check, _meta, _kind in added_candidates)]),
        task=task,
        slot=slot,
        coverage_meta_by_id=ranking_meta_by_id,
        task_files=coverage_files,
    )
    primary_by_group, _label_by_key = _primary_owner_maps(by_id)
    primary_owner_keys = _task_primary_owner_keys(
        task_surface_ids,
        by_id=by_id,
        primary_by_group=primary_by_group,
    )
    max_checks, adaptive_reason = adaptive_check_cap(
        ranked,
        primary_owner_count=len(primary_owner_keys),
    )
    mandatory_ids = {
        check.check_id
        for check, _meta, kind in added_candidates
        if kind == "mandatory_omitted_file"
    }
    mandatory_ranked = [check for check in ranked if check.check_id in mandatory_ids]
    capped, cap_diagnostics = surface_fair_cap_checks(
        ranked,
        task_surface_ids=task_surface_ids,
        by_id=by_id,
        slot=slot,
        task_files=coverage_files,
        max_checks=max_checks,
    )
    capped, high_signal_swaps = preserve_trimmed_high_signal_checks(
        capped,
        ranked,
        original_ids=original_ids,
        mandatory_ids=mandatory_ids,
        by_id=by_id,
        slot=slot,
        task_files=coverage_files,
    )
    if high_signal_swaps:
        cap_diagnostics["high_signal_swaps"] = high_signal_swaps
    cap_diagnostics["protected_existing_check_ids"] = [
        check_id
        for check_id in cap_diagnostics.get("protected_existing_check_ids", [])
        if check_id in original_ids
    ]
    capped_ids = {check.check_id for check in capped}
    final_checks = [*capped, *(check for check in mandatory_ranked if check.check_id not in capped_ids)]
    final_ids = {check.check_id for check in final_checks}
    selected_surface_ids = {
        sid
        for check in final_checks
        for sid in check.surface_ids
        if sid in by_id
    }
    added_check_by_id = {check.check_id: check for check, _meta, _kind in added_candidates}
    added = [check for check in final_checks if check.check_id not in original_ids]
    trimmed_existing: List[str] = [
        check.check_id for check in checks if check.check_id not in final_ids
    ]
    origin_lookup: Dict[str, Dict[str, Any]] = {
        **{key: dict(value) for key, value in dict(check_origins or {}).items()},
        **added_origin_by_id,
    }
    check_by_id = {
        check.check_id: check
        for check in [*checks, *(check for check, _meta, _kind in added_candidates)]
    }
    trimmed_by_origin_family: Dict[str, List[str]] = {}
    for check_id in trimmed_existing:
        check = check_by_id.get(check_id)
        origin = origin_lookup.get(check_id, {})
        origin_kind = str(origin.get("origin_kind") or "unknown")
        family = _check_signal_family(check) if check is not None else "unknown"
        trimmed_by_origin_family.setdefault(f"{origin_kind}:{family}", []).append(check_id)
    skipped_due_to_cap: List[Dict[str, Any]] = [
        {
            **dict(added_by_id[check_id]),
            "check_id": check_id,
            "surface_already_selected": any(
                sid in selected_surface_ids
                for sid in (added_check_by_id.get(check_id).surface_ids if added_check_by_id.get(check_id) else [])
            ),
        }
        for check_id in added_by_id
        if check_id not in final_ids
    ]

    final_origins: Dict[str, Dict[str, Any]] = {}
    incoming_origins = {key: dict(value) for key, value in dict(check_origins or {}).items()}
    for check in final_checks:
        if check.check_id in added_origin_by_id:
            final_origins[check.check_id] = dict(added_origin_by_id[check.check_id])
        elif check.check_id in incoming_origins:
            final_origins[check.check_id] = incoming_origins[check.check_id]
        else:
            final_origins[check.check_id] = check_origin(check, "llm_compiled", _origin_reason("llm_compiled"))

    warnings = [f"compiler_coverage_floor_added:{check.check_id}" for check in added]
    if skipped_due_to_cap:
        warnings.append(f"compiler_coverage_floor_cap_reached:{len(skipped_due_to_cap)}")
    return final_checks, {
        "coverage_files": coverage_files,
        "missed_files": missing_files,
        "ranked_obligations": [dict(item) for item in obligations],
        "uncovered_obligations": [dict(item) for item in uncovered_obligations],
        "primary_surface_ids": task_surface_ids,
        "missing_primary_surface_ids": missing_surface_ids,
        "added_checks": [check.model_dump(mode="json") for check in added],
        "added_coverage_checks": [check.check_id for check in added],
        "added_check_origins": {
            check.check_id: added_origin_by_id[check.check_id]
            for check in added
            if check.check_id in added_origin_by_id
        },
        "adaptive_max_checks": max_checks,
        "adaptive_cap_reason": adaptive_reason,
        "owner_fair_cap": cap_diagnostics,
        "skipped_due_to_cap": skipped_due_to_cap,
        "trimmed_existing_check_ids": trimmed_existing,
        "trimmed_existing_by_origin_family": trimmed_by_origin_family,
        "max_checks": max_checks,
        "warnings": warnings,
    }, final_origins


def pipeline_slot(state: GraphState, task_id: str) -> Dict[str, Any]:
    meta = state.get("metadata", {}) or {}
    pipe = meta.get("critique_pipeline", {}) if isinstance(meta, dict) else {}
    by_task = pipe.get("by_task", {}) if isinstance(pipe, dict) else {}
    slot = by_task.get(task_id, {}) if isinstance(by_task, dict) else {}
    return dict(slot) if isinstance(slot, dict) else {}


def behavioral_spec_from_state(state: GraphState, settings: Settings) -> BehavioralSpec | None:
    ref = state.get("behavioral_spec_ref")
    if not isinstance(ref, str) or not ref.strip():
        return None
    try:
        return BehavioralSpecStore(settings).read(ref)
    except Exception:  # noqa: BLE001
        return None


def _check_from_contract_question(
    *,
    task: ReviewTask,
    question: ContractQuestion,
    surface: ReviewSurface,
    index: int,
) -> ReviewCheck:
    line_start = surface.line_start or 1
    line_end = surface.line_end or line_start
    required = [item for item in question.required_evidence if str(item).strip()]
    if question.contract_evidence.strip():
        required.insert(0, question.contract_evidence.strip())
    if question.trigger_variant.strip():
        required.append(f"trigger/variant to inspect: {question.trigger_variant.strip()}")
    required.append(f"changed behavior of {question.owner or surface.name}")
    suppress = question.direct_suppressor.strip() or (
        "Concrete evidence answers this exact contract question and proves the alleged breach cannot occur."
    )
    report = question.breach_question.strip() or (
        "The changed code violates the expected behavior on a reachable path."
    )
    value_flow_required = _contract_question_requires_value_flow(question)
    if value_flow_required:
        required.extend(
            [
                "produced value shape before the operation",
                "selected or transformed value shape at the operation",
                "returned, consumed, joined, or serialized value shape after the operation",
            ]
        )
    owned_parts = [
        question.owner or surface.name,
        question.dimension,
        question.trigger_variant,
        question.operation,
    ]
    return ReviewCheck(
        check_id=f"{task.id}:contract-question:{index}",
        patch_task_id=task.id,
        surface_ids=[surface.surface_id],
        lens=QUESTION_DIMENSION_TO_LENS.get(question.dimension, "other"),  # type: ignore[arg-type]
        file_path=surface.file_path,
        line_start=line_start,
        line_end=line_end,
        changed_code_anchor=question.owner or surface.name,
        owned_contract_scope=":".join(part.strip() for part in owned_parts if part.strip())[:240],
        issue_family=question.dimension,
        diff_signal_family="contract_question",
        diff_signal=(question.breach_question or question.expected_behavior)[:240],
        behavioral_question=report[:400],
        affected_invariant=question.breach_question[:400] or question.expected_behavior[:400],
        expected_behavior=question.expected_behavior[:500],
        required_evidence=list(dict.fromkeys(required))[:8],
        suppress_criteria=(
            [
                suppress,
                (
                    "Suppress only with evidence that the produced, selected/transformed, and "
                    "returned/consumed value shapes satisfy this same action contract or are "
                    "intentionally narrowed by the changed contract."
                ),
            ]
            if value_flow_required
            else [suppress]
        ),
        report_criteria=[report],
        allowed_retrieval=["task_evidence", "focused_context"],
        budget=2,
    )


def _contract_question_requires_value_flow(question: ContractQuestion) -> bool:
    dimension = question.dimension.strip().lower()
    return dimension in {"data_preservation_cardinality", "serialization_type_closure"}


def checks_from_contract_questions(
    state: GraphState,
    task: ReviewTask,
    *,
    settings: Settings,
) -> List[ReviewCheck]:
    spec = behavioral_spec_from_state(state, settings)
    if spec is None or not spec.contract_questions:
        return []
    ledger = spec.surfaces or surface_ledger_from_state(state)
    by_id = surface_by_id(ledger)
    primary_by_group, _labels = _primary_owner_maps(by_id)
    task_surface_ids = set(surface_ids_for_task(task, ledger))
    eligible_by_owner: Dict[str, List[tuple[ContractQuestion, ReviewSurface]]] = {}
    owner_order: List[str] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for question in spec.contract_questions:
        if question.surface_id not in task_surface_ids:
            continue
        if not _task_owns_contract_question(state, task, question, ledger):
            continue
        surface = by_id.get(question.surface_id)
        if surface is None:
            continue
        key = _question_scope_key(question)
        if key in seen:
            continue
        seen.add(key)
        owner_key = _primary_owner_key_for_surface(surface, primary_by_group)
        if owner_key not in eligible_by_owner:
            eligible_by_owner[owner_key] = []
            owner_order.append(owner_key)
        eligible_by_owner[owner_key].append((question, surface))

    ordered: List[tuple[ContractQuestion, ReviewSurface]] = []
    depth = 0
    while len(ordered) < CONTRACT_QUESTION_CHECK_GUARD:
        added_this_round = False
        for owner_key in owner_order:
            owner_items = eligible_by_owner.get(owner_key, [])
            if depth >= len(owner_items):
                continue
            ordered.append(owner_items[depth])
            added_this_round = True
            if len(ordered) >= CONTRACT_QUESTION_CHECK_GUARD:
                break
        if not added_this_round:
            break
        depth += 1

    checks: List[ReviewCheck] = []
    for index, (question, surface) in enumerate(ordered, start=1):
        checks.append(
            _check_from_contract_question(
                task=task,
                question=question,
                surface=surface,
                index=index,
            )
        )
    return checks


def checks_from_surface_invariants(
    state: GraphState,
    task: ReviewTask,
    *,
    settings: Settings,
    exclude_surface_ids: Iterable[str] = (),
) -> List[ReviewCheck]:
    spec = behavioral_spec_from_state(state, settings)
    if spec is None or not spec.surface_invariants:
        return []
    ledger = spec.surfaces or surface_ledger_from_state(state)
    by_id = surface_by_id(ledger)
    task_surface_ids = surface_ids_for_task(task, ledger)
    excluded = set(exclude_surface_ids) | _contract_question_surface_ids(spec)
    checks: List[ReviewCheck] = []
    for invariant in spec.surface_invariants:
        if invariant.surface_id in excluded:
            continue
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
                lens=dimension_to_lens(invariant.dimension),  # type: ignore[arg-type]
                file_path=surface.file_path,
                line_start=line_start,
                line_end=line_end,
                changed_code_anchor=surface.name,
                audit_only=_invariant_check_is_audit_only(invariant),
                behavioral_question=(
                    f"Does the changed {surface.name} preserve {invariant.dimension}?"
                ),
                affected_invariant=invariant.expected_behavior[:400] or invariant.dimension,
                expected_behavior=invariant.expected_behavior[:500] or invariant.dimension,
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
        if len(checks) >= MAX_CHECKS_PER_TASK:
            break
    return checks


def dedupe_checks(checks: Iterable[ReviewCheck]) -> List[ReviewCheck]:
    seen: set[str] = set()
    out: List[ReviewCheck] = []
    for check in checks:
        if check.check_id in seen:
            continue
        seen.add(check.check_id)
        out.append(check)
    return out


def prioritize_compiled_checks(
    checks: Iterable[ReviewCheck],
    *,
    task: ReviewTask | None = None,
    slot: Mapping[str, Any] | None = None,
    coverage_meta_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    task_files: Iterable[str] = (),
) -> List[ReviewCheck]:
    """Keep focused/task-local checks ahead of broad deterministic coverage checks."""

    meta_by_id = coverage_meta_by_id or {}
    local_task_files = {path.strip().replace("\\", "/") for path in task_files if path and path.strip()}
    if task is not None:
        local_task_files.update(
            path.strip().replace("\\", "/") for path in task.target_files if path and path.strip()
        )
    evidence_blob = task_evidence_text(slot or {}) if slot is not None else None

    def rank(check: ReviewCheck) -> tuple[int, int, int, int]:
        cid = check.check_id
        meta = meta_by_id.get(cid)
        added = meta is not None
        if check.audit_only:
            return (9, 0, 1, 1)
        source_local = compiled_check_is_source_local(
            check,
            meta,
            evidence_blob,
            local_task_files,
            evidence_requirements_for_check(check),
        )
        relevance = coverage_meta_relevance(meta)
        if added and source_local:
            return (1, -relevance, 0 if check.surface_ids else 1, 0)
        if ":surface:" in cid:
            return (2 if source_local else 4, -relevance, 0 if check.surface_ids else 1, 1)
        if added:
            return (3 if source_local else 5, -relevance, 0 if check.surface_ids else 1, 1)
        return (0, -relevance, 0 if check.surface_ids else 1, 0)

    return [check for _, check in sorted(enumerate(checks), key=lambda item: (*rank(item[1]), item[0]))]


def _check_signal_family(check: ReviewCheck) -> str:
    for raw in (check.diff_signal_family, check.issue_family, check.lens):
        normalized = raw.strip().lower().replace("-", "_").replace(":", "_").replace("/", "_")
        if normalized in _STRUCTURED_SIGNAL_FAMILY_ALIASES:
            return _STRUCTURED_SIGNAL_FAMILY_ALIASES[normalized]
        for key, family in _STRUCTURED_SIGNAL_FAMILY_ALIASES.items():
            if normalized.endswith(f"_{key}"):
                return family
    return "other"


def _check_is_broad_floor(check: ReviewCheck) -> bool:
    cid = check.check_id.lower()
    return any(
        marker in cid
        for marker in (
            ":surface-coverage:",
            ":file-coverage:",
            ":uncovered-behavior:",
            ":coverage:",
            ":surface:",
        )
    )


def _eligible_for_owner_protection(
    check: ReviewCheck,
    *,
    slot: Mapping[str, Any],
    task_files: set[str],
) -> bool:
    return (
        not check.audit_only
        and not _check_is_broad_floor(check)
        and check_is_concrete_source_local_behavior(check, slot=slot, task_files=task_files)
    )


def _owner_candidate_priority(
    check: ReviewCheck,
    *,
    ranked_index: Mapping[str, int],
) -> tuple[int, int, int, int]:
    family = _check_signal_family(check)
    family_rank = (
        _HIGH_SIGNAL_FAMILY_ORDER.index(family)
        if family in _HIGH_SIGNAL_FAMILY_ORDER
        else len(_HIGH_SIGNAL_FAMILY_ORDER)
    )
    return (
        1 if check.diff_signal_family == "contract_question" else 0,
        1 if _check_is_broad_floor(check) else 0,
        family_rank,
        ranked_index.get(check.check_id, 10**9),
    )


def adaptive_check_cap(
    ranked: Sequence[ReviewCheck],
    *,
    primary_owner_count: int,
) -> tuple[int, str]:
    eligible_non_audit = [check for check in ranked if not check.audit_only]
    if primary_owner_count > _OWNER_FAIR_CAP_OWNER_THRESHOLD:
        return ADAPTIVE_MAX_CHECKS_PER_TASK, "many_primary_owners"
    if len(eligible_non_audit) > MAX_CHECKS_PER_TASK:
        return ADAPTIVE_MAX_CHECKS_PER_TASK, "eligible_non_audit_over_base_cap"
    return MAX_CHECKS_PER_TASK, "base"


def preserve_trimmed_high_signal_checks(
    selected: Sequence[ReviewCheck],
    ranked: Sequence[ReviewCheck],
    *,
    original_ids: set[str],
    mandatory_ids: set[str],
    by_id: Mapping[str, ReviewSurface],
    slot: Mapping[str, Any],
    task_files: Sequence[str],
) -> tuple[List[ReviewCheck], List[Dict[str, Any]]]:
    if not selected:
        return list(selected), []
    out = list(selected)
    selected_ids = {check.check_id for check in out}
    local_task_files = {path.strip().replace("\\", "/") for path in task_files if path and path.strip()}
    primary_by_group, label_by_key = _primary_owner_maps(by_id)

    def owner_keys(check: ReviewCheck) -> List[str]:
        return _primary_owner_keys_for_check(check, by_id=by_id, primary_by_group=primary_by_group)

    swaps: List[Dict[str, Any]] = []
    for incoming in ranked:
        if incoming.check_id in selected_ids or incoming.check_id not in original_ids:
            continue
        incoming_family = _check_signal_family(incoming)
        if incoming_family not in _HIGH_SIGNAL_SWAP_FAMILIES:
            continue
        if not _eligible_for_owner_protection(incoming, slot=slot, task_files=local_task_files):
            continue
        incoming_owners = set(owner_keys(incoming))
        if not incoming_owners:
            continue
        replacement_index: int | None = None
        for index in range(len(out) - 1, -1, -1):
            current = out[index]
            if current.check_id in mandatory_ids:
                continue
            if not incoming_owners.intersection(owner_keys(current)):
                continue
            if _check_is_broad_floor(current) or current.audit_only or current.check_id not in original_ids:
                replacement_index = index
                break
        if replacement_index is None:
            continue
        replaced = out[replacement_index]
        out[replacement_index] = incoming
        selected_ids.remove(replaced.check_id)
        selected_ids.add(incoming.check_id)
        swaps.append(
            {
                "incoming_check_id": incoming.check_id,
                "replaced_check_id": replaced.check_id,
                "family": incoming_family,
                "primary_owner_labels": [
                    label_by_key.get(owner, owner)
                    for owner in owner_keys(incoming)
                ],
            }
        )
    return out, swaps


def surface_fair_cap_checks(
    ranked: Sequence[ReviewCheck],
    *,
    task_surface_ids: Sequence[str],
    by_id: Mapping[str, ReviewSurface],
    slot: Mapping[str, Any],
    task_files: Sequence[str],
    max_checks: int,
) -> tuple[List[ReviewCheck], Dict[str, Any]]:
    if max_checks <= 0:
        return [], {}
    selected: List[ReviewCheck] = []
    selected_ids: set[str] = set()
    local_task_files = {path.strip().replace("\\", "/") for path in task_files if path and path.strip()}
    primary_by_group, label_by_key = _primary_owner_maps(by_id)
    owner_keys = _task_primary_owner_keys(task_surface_ids, by_id=by_id, primary_by_group=primary_by_group)
    ranked_index = {check.check_id: index for index, check in enumerate(ranked)}
    protected_existing_ids: List[str] = []
    selected_by_owner: Dict[str, List[str]] = {label_by_key.get(key, key): [] for key in owner_keys}
    selected_families_by_owner: Dict[str, set[str]] = {key: set() for key in owner_keys}

    def check_owner_keys(check: ReviewCheck) -> List[str]:
        return _primary_owner_keys_for_check(check, by_id=by_id, primary_by_group=primary_by_group)

    def add_check(check: ReviewCheck, *, owner_key: str | None = None, protected: bool = False) -> bool:
        if len(selected) >= max_checks or check.check_id in selected_ids:
            return False
        selected.append(check)
        selected_ids.add(check.check_id)
        owners = [owner_key] if owner_key else check_owner_keys(check)
        family = _check_signal_family(check)
        for key in owners:
            selected_families_by_owner.setdefault(key, set()).add(family)
            selected_by_owner.setdefault(label_by_key.get(key, key), []).append(check.check_id)
        if protected:
            protected_existing_ids.append(check.check_id)
        return True

    for owner_key in owner_keys:
        if len(selected) >= max_checks:
            break
        candidates = sorted(
            [
                check for check in ranked
                if check.check_id not in selected_ids
                and owner_key in check_owner_keys(check)
                and _eligible_for_owner_protection(check, slot=slot, task_files=local_task_files)
            ],
            key=lambda check: _owner_candidate_priority(check, ranked_index=ranked_index),
        )
        if candidates:
            add_check(candidates[0], owner_key=owner_key, protected=True)

    for family in _HIGH_SIGNAL_FAMILY_ORDER:
        for owner_key in owner_keys:
            if len(selected) >= max_checks:
                break
            if family in selected_families_by_owner.get(owner_key, set()):
                continue
            candidates = sorted(
                [
                    check for check in ranked
                    if check.check_id not in selected_ids
                    and owner_key in check_owner_keys(check)
                    and _check_signal_family(check) == family
                    and _eligible_for_owner_protection(check, slot=slot, task_files=local_task_files)
                ],
                key=lambda check: _owner_candidate_priority(check, ranked_index=ranked_index),
            )
            if candidates:
                add_check(candidates[0], owner_key=owner_key, protected=True)

    for check in ranked:
        if len(selected) >= max_checks:
            break
        if check.check_id in selected_ids:
            continue
        add_check(check)
    return selected, {
        "primary_owner_count": len(owner_keys),
        "primary_owner_labels": [label_by_key.get(key, key) for key in owner_keys],
        "selected_checks_by_primary_owner": selected_by_owner,
        "protected_existing_check_ids": protected_existing_ids,
    }


def evidence_paths_for_check(check: ReviewCheck, task: ReviewTask) -> List[str]:
    """Resolve the changed files needed to execute one check.

    The check anchor remains primary. Integration/cross-file checks receive all
    task targets so the executor can inspect the repository evidence that made
    the obligation executable.
    """
    task_paths = [
        str(path).strip().replace("\\", "/")
        for path in task.target_files
        if str(path).strip()
    ]
    anchor_path = check.file_path.strip().replace("\\", "/")
    requested = [
        str(path).strip().replace("\\", "/")
        for path in check.evidence_paths
        if str(path).strip()
    ]
    blob = " ".join(
        [
            check.changed_code_anchor,
            check.owned_contract_scope,
            check.behavioral_question,
            check.affected_invariant,
            *check.required_evidence,
            *check.suppress_criteria,
            *check.report_criteria,
        ]
    ).lower()
    cross_file = len(task_paths) > 1 and any(
        marker in blob
        for marker in (
            "integration",
            "cross-file",
            "cross file",
            "cross-surface",
            "cross surface",
            "across",
            "registration",
            "caller",
            "call site",
        )
    )

    candidates = [anchor_path, *requested]
    for path in task_paths:
        basename = path.rsplit("/", 1)[-1].lower()
        if cross_file or path.lower() in blob or basename in blob:
            candidates.append(path)

    allowed = set(task_paths)
    out: List[str] = []
    for path in candidates:
        if not path or path in out:
            continue
        if allowed and path not in allowed:
            continue
        out.append(path)
    return out or ([anchor_path] if anchor_path else task_paths[:1])


def normalize_compiled_checks(
    state: GraphState,
    task: ReviewTask,
    checks: Iterable[ReviewCheck],
) -> List[ReviewCheck]:
    normalized: List[ReviewCheck] = []
    seen: set[str] = set()
    fallback_path = task.target_files[0] if task.target_files else ""
    ledger = surface_ledger_from_state(state)
    by_id = surface_by_id(ledger)
    for index, check in enumerate(checks, start=1):
        cid = check.check_id.strip() or f"{task.id}:check:{index}"
        if not cid.startswith(task.id):
            cid = f"{task.id}:{cid}"
        if cid in seen:
            cid = f"{cid}:{index}"
        seen.add(cid)
        path = check.file_path.strip().replace("\\", "/") or fallback_path
        surface_ids = [sid for sid in check.surface_ids if sid in by_id]
        if ledger and not surface_ids:
            surface_ids = surface_ids_for_check_context(
                task=task,
                file_path=path,
                anchor=f"{check.changed_code_anchor} {check.behavioral_question}",
                state=state,
            )
        surface_ids = narrow_surface_ids_to_anchor(
            surface_ids,
            anchor=check.changed_code_anchor,
            question=check.behavioral_question,
            by_id=by_id,
        )
        if len(surface_ids) > 1 and not _check_text_is_cross_surface(check):
            surface_ids = surface_ids[:1]
        anchor_update = surface_anchor_update(surface_ids[:1], state)
        if anchor_update:
            path = str(anchor_update.get("file_path") or path)
        line_start = max(1, check.line_start)
        line_end = max(line_start, check.line_end)
        if anchor_update.get("line_start") and line_start == 1 and line_end == 1:
            line_start = int(anchor_update["line_start"])
            line_end = int(anchor_update.get("line_end") or line_start)
        updates = {
            "check_id": cid,
            "patch_task_id": task.id,
            "surface_ids": surface_ids,
            "file_path": path,
            "evidence_paths": evidence_paths_for_check(
                check.model_copy(update={"file_path": path}),
                task,
            ),
            "line_start": line_start,
            "line_end": line_end,
            "changed_code_anchor": str(
                anchor_update.get("changed_code_anchor") or check.changed_code_anchor
            ),
        }
        if _expected_behavior_is_implementation_shaped(check):
            updates["audit_only"] = True
        normalized.append(check.model_copy(update=updates))
        if not normalized[-1].owned_contract_scope.strip():
            normalized[-1] = normalized[-1].model_copy(
                update={"owned_contract_scope": owned_contract_scope_for_check(normalized[-1])}
            )
    return normalized


def render_compiler_prompt(
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> str:
    ranked_obligations = ranked_coverage_obligations(task, slot)
    ledger = surface_ledger_from_state(state)
    task_surface_ids = surface_ids_for_task(task, ledger) if ledger else task.surface_ids
    spec = behavioral_spec_from_state(state, settings or get_settings())
    contract_questions = []
    if spec is not None and spec.contract_questions:
        task_surface_id_set = set(task_surface_ids)
        contract_questions = [
            question.model_dump(mode="json")
            for question in spec.contract_questions
            if question.surface_id in task_surface_id_set
            and _task_owns_contract_question(state, task, question, ledger)
        ][:12]
    lens_text = _lens_text_from_slot(slot, contract_questions)
    selected_lens_cards = select_lens_cards(
        task=task,
        text=lens_text,
        obligations=ranked_obligations,
        max_cards=4,
    )
    te = slot.get("task_evidence") if isinstance(slot.get("task_evidence"), dict) else {}
    omitted_prompt_files = te.get("omitted_prompt_files") if isinstance(te.get("omitted_prompt_files"), list) else []
    primary_files = te.get("primary_files") if isinstance(te.get("primary_files"), list) else []
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
        "Repository Code Evidence": str(slot.get("direct_context") or "")[:60000],
        "Prompt File Scope": json_for_prompt(
            {
                "primary_files": primary_files,
                "omitted_prompt_files": omitted_prompt_files,
                "omitted_handling": "Omitted changed files are reviewed through scoped focused-context checks.",
            },
            max_chars=2000,
        ),
        "Mental Model Excerpt": str(slot.get("mental_model_excerpt") or "")[:12000],
        "Mental Model Contract Questions": json_for_prompt(contract_questions, max_chars=20000),
        "Review KB Context": str(slot.get("review_kb_excerpt") or "")[:12000],
        "Mental Model Contract Material": "\n".join(
            f"- {line}" for line in mental_model_contract_lines(slot)
        ),
        "Selected Contract Lens Cards": format_lens_cards(
            selected_lens_cards
        ),
        "Selected Contract Lens Metadata": json_for_prompt(
            _lens_metadata(selected_lens_cards),
            max_chars=5000,
        ),
        "Ranked Coverage Obligations": json_for_prompt(ranked_obligations, max_chars=15000),
        "Available Lenses": ", ".join(REVIEW_CHECK_LENSES),
    }
    return render_reviewer_prompt("review_check_compiler.md", sections)


def compiler_lens_selection_diagnostics(
    task: ReviewTask,
    slot: Mapping[str, Any],
    *,
    state: GraphState | None = None,
    settings: Settings | None = None,
) -> Dict[str, Any]:
    ranked_obligations = ranked_coverage_obligations(task, slot)
    contract_questions: list[dict[str, Any]] = []
    if state is not None:
        ledger = surface_ledger_from_state(state)
        task_surface_ids = surface_ids_for_task(task, ledger) if ledger else task.surface_ids
        spec = behavioral_spec_from_state(state, settings or get_settings())
        if spec is not None and spec.contract_questions:
            task_surface_id_set = set(task_surface_ids)
            contract_questions = [
                question.model_dump(mode="json")
                for question in spec.contract_questions
                if question.surface_id in task_surface_id_set
                and _task_owns_contract_question(state, task, question, ledger)
            ][:12]
    text = _lens_text_from_slot(slot, contract_questions)
    return lens_card_selection_diagnostics(
        task=task,
        text=text,
        obligations=ranked_obligations,
        max_cards=4,
    )
