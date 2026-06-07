"""Owner-isolated mental-model agents for contract-question synthesis."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Sequence

from pydantic import BaseModel, Field

from src.config import Settings
from src.domain.schemas import ContractQuestion
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import append_trace, trace_from_exception, trace_llm_call
from src.orchestration.context.mandate_loop_context import build_repository_contract_context
from src.orchestration.context.owner_contract_scaffold import build_owner_contract_scaffold_payload
from src.orchestration.prompts.renderer import render_reviewer_prompt

logger = logging.getLogger(__name__)

_MAX_PARTITIONS = 16
_MAX_RETRY_OWNERS = 3
_SIMPLE_OWNER_CHARS = 4_000
_COMPLEX_OWNER_CHARS = 10_000


class OwnerPartition(BaseModel):
    owner_group_id: str = Field(default="")
    primary_owners: List[str] = Field(default_factory=list)
    companion_owners: List[str] = Field(default_factory=list)
    reason: str = Field(default="")
    complexity: str = Field(default="simple")


class OwnerPartitionOutput(BaseModel):
    partitions: List[OwnerPartition] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class OwnerQuestionOutput(BaseModel):
    contract_questions: List[ContractQuestion] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class OwnerQuestionCritiqueOutput(BaseModel):
    contract_questions: List[ContractQuestion] = Field(default_factory=list)
    missing_owner_names: List[str] = Field(default_factory=list)
    retry_owner_names: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


def synthesize_owner_isolated_contract_questions(
    state: GraphState,
    *,
    settings: Settings,
    context_provider: Any | None,
    intent_summary: str,
    existing_questions: Sequence[ContractQuestion] = (),
    stage_label: str = "mandate_owner_agents",
    use_llm: bool = True,
) -> tuple[List[ContractQuestion], Dict[str, Any], int, List[Dict[str, Any]], List[str]]:
    """Generate owner-local contract questions without changing public schemas."""

    warnings: List[str] = []
    llm_tokens = 0
    llm_trace: List[Dict[str, Any]] = []
    payload, scaffold_diag = build_owner_contract_scaffold_payload(
        state,
        context_provider=context_provider,
        max_primary_owners=16,
        owner_soft_chars=1_600,
        owner_hard_chars=7_000,
    )
    owners = [row for row in payload.get("owners", []) if isinstance(row, Mapping)]
    if not owners or not use_llm:
        diagnostics = _diagnostics(
            scaffold_diag=scaffold_diag,
            partitions=[],
            questions=[],
            warnings=warnings,
            critique=None,
        )
        return [], diagnostics, llm_tokens, llm_trace, warnings

    partitions, part_tokens, part_trace, part_warnings = _partition_owners(
        state,
        owners,
        settings=settings,
        stage_label=stage_label,
        intent_summary=intent_summary,
    )
    llm_tokens += part_tokens
    llm_trace.extend(part_trace)
    warnings.extend(part_warnings)
    if not partitions:
        partitions = _default_partitions(owners)
        warnings.append("owner_partition_llm_empty_defaulted")

    questions: List[ContractQuestion] = []
    partition_question_counts: Dict[str, int] = {}
    retry_prompts: Dict[str, OwnerPartition] = {}
    for partition in partitions[:_MAX_PARTITIONS]:
        generated, tokens, trace, q_warnings = _questions_for_partition(
            state,
            owners,
            partition,
            settings=settings,
            stage_label=stage_label,
            intent_summary=intent_summary,
        )
        llm_tokens += tokens
        llm_trace.extend(trace)
        warnings.extend(q_warnings)
        partition_question_counts[partition.owner_group_id or ",".join(partition.primary_owners)] = len(generated)
        questions.extend(generated)
        for owner in partition.primary_owners:
            retry_prompts[owner.strip().lower()] = partition

    critique, tokens, trace, critique_warnings = _critique_questions(
        state,
        owners,
        questions,
        existing_questions=existing_questions,
        settings=settings,
        stage_label=stage_label,
        intent_summary=intent_summary,
    )
    llm_tokens += tokens
    llm_trace.extend(trace)
    warnings.extend(critique_warnings)
    if critique is not None:
        if critique.contract_questions:
            questions = list(critique.contract_questions)
        warnings.extend([f"owner_question_critique:{item}" for item in critique.warnings])
        retried: set[str] = set()
        for owner_name in critique.retry_owner_names[:_MAX_RETRY_OWNERS]:
            key = owner_name.strip().lower()
            partition = retry_prompts.get(key)
            if partition is None or key in retried:
                continue
            retried.add(key)
            retry_questions, retry_tokens, retry_trace, retry_warnings = _questions_for_partition(
                state,
                owners,
                partition,
                settings=settings,
                stage_label=f"{stage_label}_retry",
                intent_summary=intent_summary,
                retry_feedback="Question critique reported this owner lacks a central action contract.",
            )
            llm_tokens += retry_tokens
            llm_trace.extend(retry_trace)
            warnings.extend(retry_warnings)
            questions.extend(retry_questions)

    diagnostics = _diagnostics(
        scaffold_diag=scaffold_diag,
        partitions=partitions,
        questions=questions,
        warnings=warnings,
        critique=critique,
        partition_question_counts=partition_question_counts,
    )
    return questions, diagnostics, llm_tokens, llm_trace, warnings


def _partition_owners(
    state: GraphState,
    owners: Sequence[Mapping[str, Any]],
    *,
    settings: Settings,
    stage_label: str,
    intent_summary: str,
) -> tuple[List[OwnerPartition], int, List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    llm_trace: List[Dict[str, Any]] = []
    try:
        prompt = render_reviewer_prompt(
            "mental_model/owner_partition.md",
            {
                "Intent Summary": intent_summary[:1500],
                "Owner Cards": _json_for_prompt(_owner_cards(owners), max_chars=8000),
            },
        )
        llm = Models.worker(OwnerPartitionOutput, model_key=settings.reviewer_worker_model_key)
        traced = trace_llm_call(
            llm,
            prompt,
            state=state,
            node_name=f"{stage_label}_partition",
            model_key=settings.reviewer_worker_model_key,
            schema_name="OwnerPartitionOutput",
            input_summary={"owner_count": len(owners)},
        )
        out = parse_structured_output(traced.result, OwnerPartitionOutput)
        llm_trace = append_trace(llm_trace, traced)
        partitions = _normalize_partitions(out.partitions, owners)
        warnings.extend([f"owner_partition:{item}" for item in out.warnings])
        return partitions, traced.tokens, llm_trace, warnings
    except Exception as exc:  # noqa: BLE001
        llm_trace.extend(trace_from_exception(exc))
        warnings.append(f"owner_partition_llm_failed:{exc.__class__.__name__}")
        logger.warning("owner partition LLM failed: %s", exc)
        return _default_partitions(owners), 0, llm_trace, warnings


def _questions_for_partition(
    state: GraphState,
    owners: Sequence[Mapping[str, Any]],
    partition: OwnerPartition,
    *,
    settings: Settings,
    stage_label: str,
    intent_summary: str,
    retry_feedback: str = "",
) -> tuple[List[ContractQuestion], int, List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    llm_trace: List[Dict[str, Any]] = []
    owner_rows = _rows_for_partition(owners, partition)
    if not owner_rows:
        return [], 0, [], [f"owner_question_empty_partition:{partition.owner_group_id}"]
    max_chars = _COMPLEX_OWNER_CHARS if _partition_is_complex(partition, owner_rows) else _SIMPLE_OWNER_CHARS
    try:
        prompt = render_reviewer_prompt(
            "mental_model/owner_contract_questions.md",
            {
                "Intent Summary": intent_summary[:1500],
                "Owner Partition": partition.model_dump_json(indent=2),
                "Owner Local Scaffold": _json_for_prompt(owner_rows, max_chars=max_chars),
                "Repository Contract Hints": build_repository_contract_context(state, max_chars=900),
                "Retry Feedback": retry_feedback,
            },
        )
        llm = Models.worker(OwnerQuestionOutput, model_key=settings.reviewer_worker_model_key)
        traced = trace_llm_call(
            llm,
            prompt,
            state=state,
            node_name=f"{stage_label}_questions",
            model_key=settings.reviewer_worker_model_key,
            schema_name="OwnerQuestionOutput",
            request_label=partition.owner_group_id or ",".join(partition.primary_owners),
            input_summary={
                "owners": partition.primary_owners,
                "owner_local_chars": min(len(json.dumps(owner_rows, ensure_ascii=False)), max_chars),
            },
        )
        out = parse_structured_output(traced.result, OwnerQuestionOutput)
        llm_trace = append_trace(llm_trace, traced)
        valid_owners = {str(row.get("owner") or "").strip().lower() for row in owner_rows}
        questions = [
            question
            for question in out.contract_questions
            if question.owner.strip().lower() in valid_owners
        ]
        warnings.extend([f"owner_question:{item}" for item in out.warnings])
        if len(questions) < len(out.contract_questions):
            warnings.append(f"owner_question_cross_owner_filtered:{partition.owner_group_id}")
        return questions, traced.tokens, llm_trace, warnings
    except Exception as exc:  # noqa: BLE001
        llm_trace.extend(trace_from_exception(exc))
        warnings.append(f"owner_question_llm_failed:{partition.owner_group_id}:{exc.__class__.__name__}")
        logger.warning("owner question LLM failed for %s: %s", partition.owner_group_id, exc)
        return [], 0, llm_trace, warnings


def _critique_questions(
    state: GraphState,
    owners: Sequence[Mapping[str, Any]],
    questions: Sequence[ContractQuestion],
    *,
    existing_questions: Sequence[ContractQuestion],
    settings: Settings,
    stage_label: str,
    intent_summary: str,
) -> tuple[OwnerQuestionCritiqueOutput | None, int, List[Dict[str, Any]], List[str]]:
    if not questions:
        return None, 0, [], []
    warnings: List[str] = []
    llm_trace: List[Dict[str, Any]] = []
    try:
        prompt = render_reviewer_prompt(
            "mental_model/owner_question_critic.md",
            {
                "Intent Summary": intent_summary[:1500],
                "Owner Cards": _json_for_prompt(_owner_cards(owners), max_chars=6000),
                "Existing Questions": _json_for_prompt(
                    [question.model_dump(mode="json") for question in existing_questions],
                    max_chars=5000,
                ),
                "Generated Questions": _json_for_prompt(
                    [question.model_dump(mode="json") for question in questions],
                    max_chars=12000,
                ),
            },
        )
        llm = Models.worker(OwnerQuestionCritiqueOutput, model_key=settings.reviewer_worker_model_key)
        traced = trace_llm_call(
            llm,
            prompt,
            state=state,
            node_name=f"{stage_label}_question_critic",
            model_key=settings.reviewer_worker_model_key,
            schema_name="OwnerQuestionCritiqueOutput",
            input_summary={"question_count": len(questions), "owner_count": len(owners)},
        )
        out = parse_structured_output(traced.result, OwnerQuestionCritiqueOutput)
        llm_trace = append_trace(llm_trace, traced)
        return out, traced.tokens, llm_trace, warnings
    except Exception as exc:  # noqa: BLE001
        llm_trace.extend(trace_from_exception(exc))
        warnings.append(f"owner_question_critic_llm_failed:{exc.__class__.__name__}")
        logger.warning("owner question critique failed: %s", exc)
        return None, 0, llm_trace, warnings


def _normalize_partitions(
    raw_partitions: Sequence[OwnerPartition],
    owners: Sequence[Mapping[str, Any]],
) -> List[OwnerPartition]:
    owner_names = [str(row.get("owner") or "").strip() for row in owners if str(row.get("owner") or "").strip()]
    owner_set = {name.lower() for name in owner_names}
    out: List[OwnerPartition] = []
    covered: set[str] = set()
    for index, partition in enumerate(raw_partitions[:_MAX_PARTITIONS], start=1):
        primary = [
            name
            for name in partition.primary_owners
            if name.strip().lower() in owner_set and name.strip().lower() not in covered
        ]
        if not primary:
            continue
        key = tuple(sorted(name.lower() for name in primary))
        if any(set(key) == {owner.lower() for owner in item.primary_owners} for item in out):
            continue
        covered.update(name.lower() for name in primary)
        out.append(
            partition.model_copy(
                update={
                    "owner_group_id": partition.owner_group_id.strip() or f"owner-group-{index}",
                    "primary_owners": primary,
                    "companion_owners": [
                        name for name in partition.companion_owners if name.strip().lower() in owner_set
                    ],
                    "complexity": partition.complexity.strip().lower() or "simple",
                }
            )
        )
    for name in owner_names:
        if name.lower() not in covered:
            out.append(_default_partition(name, len(out) + 1))
    return out[:_MAX_PARTITIONS]


def _default_partitions(owners: Sequence[Mapping[str, Any]]) -> List[OwnerPartition]:
    return [
        _default_partition(str(row.get("owner") or "").strip(), index)
        for index, row in enumerate(owners, start=1)
        if str(row.get("owner") or "").strip()
    ][:_MAX_PARTITIONS]


def _default_partition(owner: str, index: int) -> OwnerPartition:
    return OwnerPartition(
        owner_group_id=f"owner-group-{index}",
        primary_owners=[owner],
        companion_owners=[],
        reason="Default owner-local partition.",
        complexity="simple",
    )


def _owner_cards(owners: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    for owner in owners:
        companions = owner.get("companion_surfaces")
        companion_names: List[str] = []
        if isinstance(companions, list):
            companion_names = [
                str(item.get("owner") or "")
                for item in companions
                if isinstance(item, Mapping)
            ]
        cards.append(
            {
                "owner": owner.get("owner"),
                "surface_id": owner.get("surface_id"),
                "kind": owner.get("kind"),
                "file_path": owner.get("file_path"),
                "line_start": owner.get("line_start"),
                "line_end": owner.get("line_end"),
                "snippet_status": owner.get("snippet_status"),
                "changed_lines": owner.get("changed_lines"),
                "declaration_facts": owner.get("declaration_facts", [])[:6],
                "companion_owners": companion_names[:6],
                "structural_hints": owner.get("structural_hints", [])[:6],
            }
        )
    return cards


def _rows_for_partition(
    owners: Sequence[Mapping[str, Any]],
    partition: OwnerPartition,
) -> List[Dict[str, Any]]:
    wanted = {name.strip().lower() for name in [*partition.primary_owners, *partition.companion_owners]}
    rows = []
    for owner in owners:
        name = str(owner.get("owner") or "").strip().lower()
        if name in wanted:
            rows.append(dict(owner))
    return rows


def _partition_is_complex(partition: OwnerPartition, owner_rows: Sequence[Mapping[str, Any]]) -> bool:
    if partition.complexity.lower() in {"complex", "transformation", "coupled"}:
        return True
    blob = json.dumps(owner_rows, ensure_ascii=False).lower()
    return any(token in blob for token in ("findall", "join(", "group(", "serialize", "projection", "match"))


def _diagnostics(
    *,
    scaffold_diag: Mapping[str, Any],
    partitions: Sequence[OwnerPartition],
    questions: Sequence[ContractQuestion],
    warnings: Sequence[str],
    critique: OwnerQuestionCritiqueOutput | None,
    partition_question_counts: Mapping[str, int] | None = None,
) -> Dict[str, Any]:
    question_counts: Dict[str, int] = {}
    fallback_counts: Dict[str, int] = {}
    for question in questions:
        owner = question.owner.strip().lower()
        if not owner:
            continue
        question_counts[owner] = question_counts.get(owner, 0) + 1
        if question.source_confidence <= 0.4:
            fallback_counts[owner] = fallback_counts.get(owner, 0) + 1
    return {
        "status": "ok" if scaffold_diag.get("status") == "ok" else scaffold_diag.get("status", "unknown"),
        "partition_count": len(partitions),
        "partitions": [partition.model_dump(mode="json") for partition in partitions],
        "authored_question_count": len(questions),
        "authored_question_count_by_owner": question_counts,
        "fallback_question_count_by_owner": fallback_counts,
        "partition_question_counts": dict(partition_question_counts or {}),
        "critique_warnings": list(critique.warnings if critique else []),
        "critique_missing_owner_names": list(critique.missing_owner_names if critique else []),
        "critique_retry_owner_names": list(critique.retry_owner_names if critique else []),
        "warnings": list(warnings),
        "scaffold": {
            "status": scaffold_diag.get("status"),
            "primary_owner_count": scaffold_diag.get("primary_owner_count"),
            "omitted_primary_owners": scaffold_diag.get("omitted_primary_owners", []),
        },
    }


def _json_for_prompt(value: Any, *, max_chars: int) -> str:
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."
