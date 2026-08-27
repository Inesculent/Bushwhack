"""Compiler support for review-check nodes."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from src.config import Settings, get_settings
from src.domain.schemas import (
    BehavioralSpec,
    ContractQuestion,
    ContractSourceRef,
    ReviewCheck,
    ReviewSurface,
    ReviewTask,
)
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
from src.orchestration.routing.claim_digest import owned_contract_scope_for_check
from src.orchestration.nodes.application.review_check_source_scope import (
    changed_task_files,
    compiled_check_is_source_local,
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

MAX_CHECKS_PER_TASK = 8
ADAPTIVE_MAX_CHECKS_PER_TASK = 10
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
    if not (surface_ok and dimension_ok):
        return False
    operation_markers = (
        [
            str(item).strip()
            for item in obligation.get("operation_markers", [])
            if str(item).strip()
        ]
        if isinstance(obligation.get("operation_markers"), list)
        else []
    )
    material = (
        [
            str(item).strip()
            for item in obligation.get("mental_model_contract_material", [])
            if str(item).strip()
        ]
        if isinstance(obligation.get("mental_model_contract_material"), list)
        else []
    )
    if operation_markers and not any(
        tokens_overlap(item, blob) or item.lower() in blob.lower()
        for item in operation_markers
    ):
        return False
    if material and not any(tokens_overlap(item, blob) for item in material):
        return False
    return True


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


def _origin_reason(origin_kind: str) -> str:
    return {
        "llm_compiled": "compiled_by_review_check_llm",
        "contract_question": "derived_from_behavioral_contract_question",
        "deterministic_fallback": "deterministic_fallback_from_task_evidence",
    }.get(origin_kind, origin_kind)


def cap_compiled_checks(
    *,
    state: GraphState,
    task: ReviewTask,
    checks: List[ReviewCheck],
    check_origins: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[List[ReviewCheck], Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Bound the compiled checks for one task and report what they leave uncovered.

    Nothing is added here. Obligations, primary surfaces, and changed files
    without a compiled check are recorded as diagnostics so misses stay
    observable without forcing speculative checks into the executor.
    """
    slot = pipeline_slot(state, task.id)
    obligations = ranked_coverage_obligations(task, slot)
    coverage_files = changed_task_files(state, task)
    ledger = surface_ledger_from_state(state)
    by_id = surface_by_id(ledger)
    task_surface_ids = [
        sid
        for sid in surface_ids_for_task(task, ledger)
        if sid in by_id and by_id[sid].confidence >= 0.75 and by_id[sid].kind != "file"
    ]
    evidence_omitted_files = omitted_prompt_files(slot, coverage_files)

    ranked = prioritize_compiled_checks(dedupe_checks(checks))
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
    capped, cap_diagnostics = surface_fair_cap_checks(
        ranked,
        task_surface_ids=task_surface_ids,
        by_id=by_id,
        slot=slot,
        task_files=coverage_files,
        max_checks=max_checks,
    )
    final_ids = {check.check_id for check in capped}
    trimmed_check_ids = [check.check_id for check in checks if check.check_id not in final_ids]

    uncovered_obligations = [
        obligation
        for obligation in obligations
        if not any(check_covers_obligation(check, obligation) for check in capped)
    ]
    covered_surface_ids = {sid for check in capped for sid in check.surface_ids if sid in by_id}
    missing_surface_ids = [sid for sid in task_surface_ids if sid not in covered_surface_ids]
    checked_files = {
        check.file_path.strip().replace("\\", "/")
        for check in capped
        if check.file_path.strip()
    }
    missed_files = [path for path in coverage_files if path not in checked_files]

    incoming_origins = {key: dict(value) for key, value in dict(check_origins or {}).items()}
    final_origins = {
        check.check_id: incoming_origins.get(check.check_id)
        or check_origin(check, "llm_compiled", _origin_reason("llm_compiled"))
        for check in capped
    }
    warnings: List[str] = []
    if trimmed_check_ids:
        warnings.append(f"compiler_check_cap_trimmed:{len(trimmed_check_ids)}")
    if missed_files:
        warnings.append(f"compiler_coverage_missed_files:{len(missed_files)}")
    if missing_surface_ids:
        warnings.append(f"compiler_coverage_missing_primary_surfaces:{len(missing_surface_ids)}")
    if uncovered_obligations:
        warnings.append(f"compiler_coverage_uncovered_obligations:{len(uncovered_obligations)}")
    return capped, {
        "coverage_files": coverage_files,
        "evidence_omitted_files": evidence_omitted_files,
        "missed_files": missed_files,
        "ranked_obligations": [dict(item) for item in obligations],
        "uncovered_obligations": [dict(item) for item in uncovered_obligations],
        "primary_surface_ids": task_surface_ids,
        "missing_primary_surface_ids": missing_surface_ids,
        "max_checks": max_checks,
        "adaptive_cap_reason": adaptive_reason,
        "owner_fair_cap": cap_diagnostics,
        "trimmed_check_ids": trimmed_check_ids,
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
    contract_source = None
    if question.contract_source_kind and question.contract_evidence.strip():
        contract_source = ContractSourceRef(
            kind=question.contract_source_kind,
            ref=f"{surface.file_path}:{line_start}-{line_end}",
            note=question.contract_evidence.strip()[:300],
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
        contract_source=contract_source,
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
    if dimension in {"data_preservation_cardinality", "serialization_type_closure"}:
        return True
    blob = " ".join(
        [
            question.dimension,
            question.expected_behavior,
            question.trigger_variant,
            question.operation,
            question.breach_question,
        ]
    ).lower()
    return any(
        marker in blob
        for marker in (
            "aggregate",
            "aggregation",
            "cardinality",
            "field",
            "element",
            "group",
            "nested",
            "projection",
            "select",
            "selection",
            "serialize",
            "serialization",
            "join",
            "returned value",
            "return shape",
            "output shape",
            "type closure",
            "type-closure",
        )
    )


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


def dedupe_checks(checks: Iterable[ReviewCheck]) -> List[ReviewCheck]:
    seen: set[str] = set()
    out: List[ReviewCheck] = []
    for check in checks:
        if check.check_id in seen:
            continue
        seen.add(check.check_id)
        out.append(check)
    return out


def prioritize_compiled_checks(checks: Iterable[ReviewCheck]) -> List[ReviewCheck]:
    """Keep executable checks ahead of audit-only checks; otherwise preserve compile order."""
    return [
        check
        for _index, check in sorted(
            enumerate(checks),
            key=lambda item: (1 if item[1].audit_only else 0, item[0]),
        )
    ]


def _check_signal_family(check: ReviewCheck) -> str:
    for raw in (check.diff_signal_family, check.issue_family, check.lens):
        normalized = raw.strip().lower().replace("-", "_").replace(":", "_").replace("/", "_")
        if normalized in _STRUCTURED_SIGNAL_FAMILY_ALIASES:
            return _STRUCTURED_SIGNAL_FAMILY_ALIASES[normalized]
        for key, family in _STRUCTURED_SIGNAL_FAMILY_ALIASES.items():
            if normalized.endswith(f"_{key}"):
                return family
    return "other"


def _eligible_for_owner_protection(
    check: ReviewCheck,
    *,
    slot: Mapping[str, Any],
    task_files: set[str],
) -> bool:
    return not check.audit_only and check_is_concrete_source_local_behavior(
        check,
        slot=slot,
        task_files=task_files,
    )


def _owner_candidate_priority(
    check: ReviewCheck,
    *,
    ranked_index: Mapping[str, int],
) -> tuple[int, int, int]:
    family = _check_signal_family(check)
    family_rank = (
        _HIGH_SIGNAL_FAMILY_ORDER.index(family)
        if family in _HIGH_SIGNAL_FAMILY_ORDER
        else len(_HIGH_SIGNAL_FAMILY_ORDER)
    )
    return (
        1 if check.diff_signal_family == "contract_question" else 0,
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
        current = check.model_copy(update=updates)
        if not current.owned_contract_scope.strip():
            current = current.model_copy(
                update={"owned_contract_scope": owned_contract_scope_for_check(current)}
            )
        if current.contract_source is None and not current.audit_only:
            # The compiler could not name the contract source; the executor may retrieve it.
            current = current.model_copy(
                update={
                    "allowed_retrieval": list(
                        dict.fromkeys([*current.allowed_retrieval, "focused_context"])
                    ),
                }
            )
        normalized.append(current)
    return normalized


def render_compiler_prompt(
    state: GraphState,
    task: ReviewTask,
    slot: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    ranked_obligations = ranked_coverage_obligations(task, slot)
    ledger = surface_ledger_from_state(state)
    task_surface_ids = surface_ids_for_task(task, ledger) if ledger else task.surface_ids
    spec = behavioral_spec_from_state(state, settings)
    contract_questions = []
    if spec is not None and spec.contract_questions:
        task_surface_id_set = set(task_surface_ids)
        contract_questions = [
            question.model_dump(mode="json")
            for question in spec.contract_questions
            if question.surface_id in task_surface_id_set
            and _task_owns_contract_question(state, task, question, ledger)
        ][:8]
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
        "Surface Ledger": compact_surface_ledger_json(ledger, max_records=20) if ledger else "[]",
        "Repository Code Evidence": str(slot.get("direct_context") or "")[:14000],
        "Prompt File Scope": json_for_prompt(
            {
                "primary_files": primary_files,
                "omitted_prompt_files": omitted_prompt_files,
                "omitted_handling": "Omitted changed files are reviewed through scoped focused-context checks.",
            },
            max_chars=1500,
        ),
        "Mental Model Excerpt": str(slot.get("mental_model_excerpt") or "")[:3000],
        "Mental Model Contract Questions": json_for_prompt(contract_questions, max_chars=5000),
        "Review KB Context": str(slot.get("review_kb_excerpt") or "")[:3000],
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
        "Ranked Coverage Obligations": json_for_prompt(ranked_obligations, max_chars=5000),
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
