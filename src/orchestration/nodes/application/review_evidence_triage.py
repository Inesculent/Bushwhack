"""LLM triage for evidence routing before reflection/verifier fanout."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Sequence

from src.config import Settings, get_settings
from src.domain.schemas import (
    CandidateFinding,
    ReviewCheck,
    ReviewCheckResult,
    ReviewEvidenceTriageItem,
    ReviewEvidenceTriageOutput,
)
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import append_trace, trace_from_exception, trace_llm_call
from src.orchestration.prompts.renderer import render_reviewer_prompt

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _coerce_candidate(raw: Any) -> CandidateFinding | None:
    if isinstance(raw, CandidateFinding):
        return raw
    if isinstance(raw, dict):
        try:
            return CandidateFinding.model_validate(raw)
        except Exception:
            return None
    return None


def _coerce_check_result(raw: Any) -> ReviewCheckResult | None:
    if isinstance(raw, ReviewCheckResult):
        return raw
    if isinstance(raw, dict):
        try:
            return ReviewCheckResult.model_validate(raw)
        except Exception:
            return None
    return None


def _candidate_sort_key(candidate: CandidateFinding) -> tuple[str, int, str]:
    return (candidate.file_path, int(candidate.line_start or 0), candidate.candidate_id)


def _fallback_specialties(candidate: CandidateFinding) -> list[str]:
    valid = {"security", "performance", "logic", "general"}
    declared = [item for item in candidate.reflection_specialties if item in valid]
    if declared:
        return list(dict.fromkeys(declared))
    if candidate.claim_type == "security_risk":
        return ["security"]
    if candidate.claim_type == "performance_regression":
        return ["performance"]
    if candidate.claim_type == "missing_test":
        return ["general"]
    if candidate.suspected_category in valid:
        return [candidate.suspected_category]
    return ["logic"]


def _fallback_runtime_usefulness(candidate: CandidateFinding) -> str:
    if candidate.claim_type in {"defect", "security_risk", "performance_regression"} and candidate.file_path:
        return "advisory"
    return "unclear"


def _fallback_item(candidate: CandidateFinding) -> ReviewEvidenceTriageItem:
    return ReviewEvidenceTriageItem(
        candidate_id=candidate.candidate_id,
        claim_summary=(candidate.failure_mode or candidate.content)[:700],
        claim_family=candidate.claim_type,
        suggested_reflection_specialties=_fallback_specialties(candidate),  # type: ignore[arg-type]
        source_fact_requests=[],
        runtime_verification_usefulness=_fallback_runtime_usefulness(candidate),  # type: ignore[arg-type]
        needed_context=list(candidate.required_context[:8]),
        rationale="Fallback triage from declared candidate fields; no keyword claim inference applied.",
    )


def _check_results_by_candidate(state: GraphState) -> Dict[str, List[ReviewCheckResult]]:
    out: Dict[str, List[ReviewCheckResult]] = {}
    for raw in state.get("review_check_results", []) or []:
        result = _coerce_check_result(raw)
        if result is None or result.candidate is None:
            continue
        out.setdefault(result.candidate.candidate_id, []).append(result)
    return out


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 18)] + "\n... [truncated]"


def _compact_candidate(candidate: CandidateFinding, state: GraphState) -> Dict[str, Any]:
    packet: Dict[str, Any] = {
        "candidate_id": candidate.candidate_id,
        "file_path": candidate.file_path,
        "line_start": candidate.line_start,
        "line_end": candidate.line_end,
        "claim_type": candidate.claim_type,
        "suspected_category": candidate.suspected_category,
        "reflection_specialties": list(candidate.reflection_specialties),
        "content": _truncate(candidate.content, 700),
        "expected_behavior": _truncate(candidate.expected_behavior, 500),
        "evidence_for_contract": _truncate(candidate.evidence_for_contract, 400),
        "counterexample": _truncate(candidate.counterexample, 400),
        "rejection_check": _truncate(candidate.rejection_check, 400),
        "failure_mode": _truncate(candidate.failure_mode, 500),
        "evidence_summary": _truncate(candidate.evidence_summary, 500),
        "required_context": list(candidate.required_context[:8]),
    }
    local_text = " ".join(
        [candidate.content, candidate.failure_mode, candidate.evidence_summary]
    ).strip()
    if len(local_text) < 80:
        excerpt = _source_excerpt_for_candidate(state, candidate)
        if excerpt:
            packet["source_excerpt"] = excerpt
    return packet


def _coerce_check(raw: Any) -> ReviewCheck | None:
    if isinstance(raw, ReviewCheck):
        return raw
    if isinstance(raw, dict):
        try:
            return ReviewCheck.model_validate(raw)
        except Exception:
            return None
    return None


def _checks_by_id(state: GraphState) -> Dict[str, ReviewCheck]:
    out: Dict[str, ReviewCheck] = {}
    for raw in state.get("review_checks", []) or []:
        check = _coerce_check(raw)
        if check is not None:
            out[check.check_id] = check
    return out


def _compact_check_result(result: ReviewCheckResult, check: ReviewCheck | None) -> Dict[str, Any]:
    return {
        "check_id": result.check_id,
        "behavioral_question": _truncate(check.behavioral_question if check else "", 400),
        "affected_invariant": _truncate(check.affected_invariant if check else "", 400),
        "expected_behavior": _truncate(
            result.expected_behavior or (check.expected_behavior if check else ""),
            500,
        ),
        "decision": result.decision,
        "reportable_reason": _truncate(result.reportable_reason, 500),
    }


def _source_excerpt_for_candidate(state: GraphState, candidate: CandidateFinding) -> str:
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    pipe = metadata.get("critique_pipeline") if isinstance(metadata, dict) else {}
    by_task = pipe.get("by_task") if isinstance(pipe, dict) else {}
    slot = by_task.get(candidate.patch_task_id) if isinstance(by_task, dict) else {}
    task_evidence = slot.get("task_evidence") if isinstance(slot, dict) else {}
    file_contents = task_evidence.get("file_contents") if isinstance(task_evidence, dict) else {}
    body = file_contents.get(candidate.file_path) if isinstance(file_contents, dict) else None
    if not isinstance(body, str) or not body.strip():
        return ""
    lines = body.splitlines()
    if not lines:
        return ""
    start = max(1, int(candidate.line_start or 1))
    end = max(start, int(candidate.line_end or start))
    lo = max(1, start - 8)
    hi = min(len(lines), end + 8)
    excerpt = "\n".join(f"{index}: {lines[index - 1]}" for index in range(lo, hi + 1))
    return _truncate(excerpt, 1200)


def build_review_evidence_triage_packets(state: GraphState) -> List[Dict[str, Any]]:
    checks = _check_results_by_candidate(state)
    checks_by_id = _checks_by_id(state)
    candidates = [
        candidate
        for raw in state.get("candidate_findings", []) or []
        if (candidate := _coerce_candidate(raw)) is not None
    ]
    packets: List[Dict[str, Any]] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        packets.append(
            {
                "candidate": _compact_candidate(candidate, state),
                "originating_checks": [
                    _compact_check_result(result, checks_by_id.get(result.check_id))
                    for result in checks.get(candidate.candidate_id, [])[:2]
                ],
            }
        )
    return packets


def _render_packet_batch(packets: Sequence[Mapping[str, Any]], max_candidate_chars: int) -> str:
    parts: List[str] = []
    for packet in packets:
        raw = json.dumps(packet, indent=2, ensure_ascii=False)
        parts.append(_truncate(raw, max_candidate_chars))
    return "\n\n".join(parts)


def _partition_packets(
    packets: Sequence[Mapping[str, Any]],
    *,
    max_batch_chars: int,
    max_candidate_chars: int,
) -> List[List[Mapping[str, Any]]]:
    batches: List[List[Mapping[str, Any]]] = []
    current: List[Mapping[str, Any]] = []
    current_size = 0
    for packet in packets:
        size = len(_truncate(json.dumps(packet, ensure_ascii=False), max_candidate_chars))
        if current and current_size + size > max_batch_chars:
            batches.append(current)
            current = []
            current_size = 0
        current.append(packet)
        current_size += size
    if current:
        batches.append(current)
    return batches


def _render_prompt(
    state: GraphState,
    packets: Sequence[Mapping[str, Any]],
    *,
    max_candidate_chars: int,
) -> str:
    return render_reviewer_prompt(
        "review_evidence_triage.md",
        {
            "Candidate Packets": _render_packet_batch(packets, max_candidate_chars),
        },
    )


def _normalize_items(
    candidates: Sequence[CandidateFinding],
    outputs: Sequence[ReviewEvidenceTriageOutput],
) -> tuple[list[ReviewEvidenceTriageItem], list[str], list[str]]:
    by_candidate = {candidate.candidate_id: candidate for candidate in candidates}
    ordered: Dict[str, ReviewEvidenceTriageItem] = {}
    warnings: List[str] = []
    for output in outputs:
        warnings.extend(output.warnings)
        for item in output.items:
            if item.candidate_id not in by_candidate:
                warnings.append(f"triage_unknown_candidate:{item.candidate_id}")
                continue
            if item.candidate_id in ordered:
                warnings.append(f"triage_duplicate_candidate:{item.candidate_id}")
            ordered[item.candidate_id] = item
    for candidate in candidates:
        if candidate.candidate_id not in ordered:
            warnings.append(f"triage_missing_candidate:{candidate.candidate_id}")
            ordered[candidate.candidate_id] = _fallback_item(candidate)
    fallback_ids = [
        candidate.candidate_id
        for candidate in candidates
        if candidate.candidate_id not in {
            item.candidate_id
            for output in outputs
            for item in output.items
        }
    ]
    return [ordered[candidate.candidate_id] for candidate in candidates], warnings, fallback_ids


def _is_length_finish_error(exc: Exception) -> bool:
    if "LengthFinish" in exc.__class__.__name__:
        return True
    text = str(exc).lower()
    return "length limit" in text or "length_finish" in text


def _packet_candidate_ids(packets: Sequence[Mapping[str, Any]]) -> list[str]:
    ids: list[str] = []
    for packet in packets:
        candidate = packet.get("candidate") if isinstance(packet, Mapping) else None
        cid = candidate.get("candidate_id") if isinstance(candidate, Mapping) else None
        if cid:
            ids.append(str(cid))
    return ids


def make_review_evidence_triage_node(
    model_key: str | None = None,
    use_llm: bool = True,
    settings: Settings | None = None,
):
    node_name = "review_evidence_triage"

    def review_evidence_triage_node(state: GraphState) -> Dict[str, Any]:
        resolved = settings or get_settings()
        candidates = [
            candidate
            for raw in state.get("candidate_findings", []) or []
            if (candidate := _coerce_candidate(raw)) is not None
        ]
        candidates.sort(key=_candidate_sort_key)
        metadata = dict(state.get("metadata") or {})
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []
        warnings: List[str] = []
        failed_batch_candidate_ids: List[str] = []
        retried_batch_count = 0
        retry_success_count = 0

        if not candidates:
            metadata[node_name] = {
                "items": [],
                "candidate_count": 0,
                "warnings": [],
            }
            return {"metadata": metadata, "node_history": [f"{node_name}:skipped"]}

        packets = build_review_evidence_triage_packets(state)
        batches = _partition_packets(
            packets,
            max_batch_chars=int(resolved.reviewer_triage_max_batch_chars),
            max_candidate_chars=int(resolved.reviewer_triage_max_candidate_chars),
        )
        outputs: List[ReviewEvidenceTriageOutput] = []
        if use_llm:
            selected_model = model_key or resolved.reviewer_worker_model_key
            def _invoke_batch(
                batch: Sequence[Mapping[str, Any]],
                *,
                request_label: str,
                input_summary: Mapping[str, Any],
            ) -> ReviewEvidenceTriageOutput:
                nonlocal llm_tokens, llm_trace
                prompt = _render_prompt(
                    state,
                    batch,
                    max_candidate_chars=int(resolved.reviewer_triage_max_candidate_chars),
                )
                llm = Models.worker(
                    ReviewEvidenceTriageOutput,
                    model_key=selected_model,
                    max_completion_tokens=int(resolved.reviewer_triage_max_completion_tokens),
                )
                traced = trace_llm_call(
                    llm,
                    prompt,
                    state=state,
                    node_name=node_name,
                    model_key=selected_model,
                    schema_name="ReviewEvidenceTriageOutput",
                    request_label=request_label,
                    input_summary=dict(input_summary),
                )
                llm_tokens += traced.tokens
                llm_trace.extend(append_trace([], traced))
                return parse_structured_output(traced.result, ReviewEvidenceTriageOutput)

            for index, batch in enumerate(batches):
                try:
                    outputs.append(
                        _invoke_batch(
                            batch,
                            request_label=f"batch_{index + 1}_of_{len(batches)}",
                            input_summary={"batch_index": index, "batch_size": len(batch)},
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    llm_trace.extend(trace_from_exception(exc))
                    batch_ids = _packet_candidate_ids(batch)
                    failed_batch_candidate_ids.extend(batch_ids)
                    warnings.append(f"review_evidence_triage_failed:{index}:{exc.__class__.__name__}: {exc}")
                    logger.warning("%s failed batch=%s reason=%s", node_name, index, exc)
                    if not _is_length_finish_error(exc) or len(batch) <= 1:
                        continue
                    retry_outputs = 0
                    for retry_index, packet in enumerate(batch):
                        retried_batch_count += 1
                        try:
                            outputs.append(
                                _invoke_batch(
                                    [packet],
                                    request_label=f"length_retry_{index + 1}_{retry_index + 1}",
                                    input_summary={
                                        "batch_index": index,
                                        "retry_index": retry_index,
                                        "candidate_ids": _packet_candidate_ids([packet]),
                                    },
                                )
                            )
                            retry_outputs += 1
                        except Exception as retry_exc:  # noqa: BLE001
                            llm_trace.extend(trace_from_exception(retry_exc))
                            warnings.append(
                                "review_evidence_triage_retry_failed:"
                                f"{index}:{retry_index}:{retry_exc.__class__.__name__}: {retry_exc}"
                            )
                    retry_success_count += retry_outputs
        else:
            warnings.append("review_evidence_triage_llm_disabled")

        items, norm_warnings, fallback_candidate_ids = _normalize_items(candidates, outputs)
        warnings.extend(norm_warnings)
        metadata[node_name] = {
            "items": [item.model_dump(mode="json") for item in items],
            "candidate_count": len(candidates),
            "batch_count": len(batches),
            "failed_batch_candidate_ids": list(dict.fromkeys(failed_batch_candidate_ids)),
            "retried_batch_count": retried_batch_count,
            "retry_success_count": retry_success_count,
            "fallback_candidate_ids": list(dict.fromkeys(fallback_candidate_ids)),
            "warnings": warnings,
        }

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE review_evidence_triage run_id=%s candidates=%s batches=%s warnings=%s",
                state.get("run_id", "unknown"),
                len(candidates),
                len(batches),
                len(warnings),
            )

        return {
            "metadata": metadata,
            "node_history": [node_name],
            "token_usage": llm_tokens,
            "llm_trace": llm_trace,
        }

    return review_evidence_triage_node
