"""Batch specialist reflection over all candidate findings."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from src.config import Settings, get_settings
from src.domain.schemas import (
    CandidateFinding,
    FocusedContextRequest,
    ReflectionBatchOutput,
    ReflectionReport,
    ReviewEvidenceTriageItem,
)
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.local_status import (
    is_local_model,
    is_timeout_exception,
    local_llm_server_active,
    sleep_for_retry,
)
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import append_trace, trace_from_exception, trace_llm_call
from src.orchestration.context.focus_request_scope import (
    allowed_review_paths,
    clamp_focused_context_request,
)
from src.orchestration.context.context_packets import (
    build_reflection_packet,
    packet_to_prompt_sections,
)
from src.orchestration.prompts.renderer import render_reviewer_prompt
from src.orchestration.nodes.application.critiquer import _is_length_finish_error
from src.orchestration.routing.finding_dedupe import dedupe_candidates_by_signature
from src.orchestration.routing.reflection_consolidation import dedupe_batch_reports_per_candidate

logger = logging.getLogger(__name__)

_REFLECTION_OUTPUT_BUDGET = (
    "\n\n## OUTPUT BUDGET (required)\n"
    "Emit exactly one ReflectionReport per candidate line in the input (no extra reports). "
    "Keep each rationale under 1200 characters: cite repository paths and line numbers or symbols; "
    "do not paste code blocks (code is already in code_evidence). "
    "One short self-check line is allowed (e.g. Rationale supports verdict: yes/no). "
    "Set support_scope to local, needs_context, runtime_dependent, or unclear based on the evidence needed "
    "to judge this candidate. "
    "Use focused_request only when verdict is needs_context; keep reason under 300 characters. "
    "Keep warnings to a few short strings—do not put full rationales in warnings."
)

_REFLECTION_COMPACT_RETRY_APPENDIX = (
    "\n\n## OUTPUT BUDGET (retry — required)\n"
    "Your previous response exceeded the length limit. Return exactly one report per input candidate. "
    "Keep each rationale under 500 characters with path/line citations only—no code pastes. "
    "Omit focused_request unless essential. No prose outside schema fields."
)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

REFLECTOR_SPECIALTIES = ("security", "logic", "performance", "general")
REFLECTOR_SPECIALTY_SET = set(REFLECTOR_SPECIALTIES)

def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _triage_items_by_candidate(state: GraphState) -> Dict[str, ReviewEvidenceTriageItem]:
    metadata = state.get("metadata", {}) or {}
    triage = metadata.get("review_evidence_triage") if isinstance(metadata, dict) else {}
    rows = triage.get("items") if isinstance(triage, dict) else []
    out: Dict[str, ReviewEvidenceTriageItem] = {}
    for raw in rows or []:
        try:
            item = raw if isinstance(raw, ReviewEvidenceTriageItem) else ReviewEvidenceTriageItem.model_validate(raw)
        except Exception:
            continue
        out[item.candidate_id] = item
    return out


def _fallback_candidate_reflectors(candidate: CandidateFinding) -> List[str]:
    routed = [
        specialty
        for specialty in candidate.reflection_specialties
        if specialty in REFLECTOR_SPECIALTY_SET
    ]
    if routed:
        return sorted(set(routed), key=REFLECTOR_SPECIALTIES.index)
    if candidate.suspected_category in REFLECTOR_SPECIALTY_SET:
        return [candidate.suspected_category]
    if candidate.claim_type == "security_risk":
        return ["security"]
    if candidate.claim_type == "performance_regression":
        return ["performance"]
    if candidate.claim_type == "missing_test":
        return ["general"]
    if candidate.claim_type == "defect":
        return ["logic"]
    return ["general"]


def _candidate_reflectors(
    candidate: CandidateFinding,
    triage_by_candidate: Dict[str, ReviewEvidenceTriageItem],
) -> List[str]:
    triage = triage_by_candidate.get(candidate.candidate_id)
    if triage is not None:
        routed = [
            specialty
            for specialty in triage.suggested_reflection_specialties
            if specialty in REFLECTOR_SPECIALTY_SET
        ]
        if routed:
            return sorted(set(routed), key=REFLECTOR_SPECIALTIES.index)
    return _fallback_candidate_reflectors(candidate)


def _candidates_by_reflector(
    candidates: List[CandidateFinding],
    triage_by_candidate: Dict[str, ReviewEvidenceTriageItem] | None = None,
) -> Dict[str, List[CandidateFinding]]:
    triage_by_candidate = triage_by_candidate or {}
    grouped: Dict[str, List[CandidateFinding]] = {specialty: [] for specialty in REFLECTOR_SPECIALTIES}
    for candidate in candidates:
        for specialty in _candidate_reflectors(candidate, triage_by_candidate):
            grouped[specialty].append(candidate)
    return grouped


def _render_reflection_prompt(
    state: GraphState,
    specialty: str,
    candidates: List[CandidateFinding],
    *,
    mental_model_ledger_snippet: str = "",
    compact: bool = False,
) -> str:
    packet = build_reflection_packet(
        state,
        specialty,
        candidates,
        mental_model_ledger_snippet=mental_model_ledger_snippet,
    )
    rel_path = f"reflection/{specialty}.md"
    prompt = render_reviewer_prompt(rel_path, packet_to_prompt_sections(packet))
    prompt = f"{prompt}{_REFLECTION_OUTPUT_BUDGET}"
    if compact:
        prompt = f"{prompt}{_REFLECTION_COMPACT_RETRY_APPENDIX}"
    return prompt


def _normalize_focus_request(
    state: GraphState,
    report: ReflectionReport,
    specialty: str,
    index: int,
    *,
    candidate_file_path: str | None = None,
) -> FocusedContextRequest | None:
    if report.focused_request is None:
        return None
    req = report.focused_request
    rid = req.request_id.strip() or f"{report.candidate_id}:{specialty}:focus:{index}"
    cid = req.candidate_id.strip() or report.candidate_id
    scope = allowed_review_paths(state, candidate_file_path=candidate_file_path)
    scoped = clamp_focused_context_request(
        req,
        scope,
        fallback_path=candidate_file_path,
    )
    return scoped.model_copy(
        update={
            "request_id": rid,
            "candidate_id": cid,
            "requested_by_specialty": specialty if specialty in REFLECTOR_SPECIALTIES else "general",
        }
    )



def _enforce_rationale_consistency(report: ReflectionReport) -> tuple[ReflectionReport, str | None]:
    return report, None


def _chunk_candidates(
    candidates: List[CandidateFinding],
    batch_size: int,
) -> List[List[CandidateFinding]]:
    if batch_size < 1:
        batch_size = 1
    return [candidates[i : i + batch_size] for i in range(0, len(candidates), batch_size)]


def _reflect_specialty_batches(
    *,
    state: GraphState,
    specialty: str,
    specialty_candidates: List[CandidateFinding],
    selected_model: str | None,
    resolved_settings: Settings,
    mental_model_ledger_snippet: str,
    use_llm: bool,
) -> tuple[List[ReflectionReport], List[FocusedContextRequest], List[str], int, List[Dict[str, Any]]]:
    """Invoke reflection in bounded batches; retry singletons for missing candidate ids."""
    if not use_llm or not specialty_candidates:
        return [], [], [], 0

    batch_size = max(1, int(resolved_settings.verifier_reflection_batch_size))
    all_reports: List[ReflectionReport] = []
    all_requests: List[FocusedContextRequest] = []
    warnings: List[str] = []
    llm_tokens = 0
    llm_trace: List[Dict[str, Any]] = []

    def _invoke_batch(batch: List[CandidateFinding]) -> None:
        nonlocal llm_tokens
        timeout_deadline = (
            time.monotonic() + resolved_settings.reviewer_reflection_timeout_patience_seconds
            if is_local_model(selected_model)
            and resolved_settings.reviewer_reflection_timeout_patience_seconds > 0
            else None
        )
        attempt = 0
        compact = False
        while True:
            attempt += 1
            try:
                llm = Models.worker(ReflectionBatchOutput, model_key=selected_model)
                prompt = _render_reflection_prompt(
                    state,
                    specialty,
                    batch,
                    mental_model_ledger_snippet=mental_model_ledger_snippet,
                    compact=compact,
                )
                traced = trace_llm_call(
                    llm,
                    prompt,
                    state=state,
                    node_name="adversarial_reflection",
                    model_key=selected_model,
                    schema_name="ReflectionBatchOutput",
                    request_label=f"{specialty}:{'compact' if compact else 'primary'}",
                    input_summary={
                        "specialty": specialty,
                        "candidate_ids": [c.candidate_id for c in batch],
                    },
                )
                invoke_result = traced.result
                response = parse_structured_output(invoke_result, ReflectionBatchOutput)
                llm_tokens += traced.tokens
                llm_trace.extend(traced.trace_records)
                reps, reqs, norm_warnings = _normalize_reports(
                    state, response, specialty, batch_candidates=batch
                )
                all_reports.extend(reps)
                all_requests.extend(reqs)
                warnings.extend(response.warnings)
                warnings.extend(norm_warnings)
                return
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                timeout_with_patience_left = (
                    is_timeout_exception(exc)
                    and timeout_deadline is not None
                    and time.monotonic() < timeout_deadline
                )
                if timeout_with_patience_left:
                    server_active, status_detail = local_llm_server_active(resolved_settings)
                    if server_active:
                        warning = f"reflection_timeout_server_active:{specialty}"
                        if warning not in warnings:
                            warnings.append(warning)
                        sleep_for_retry(
                            resolved_settings.reviewer_reflection_retry_backoff_seconds,
                            attempt,
                            timeout_deadline,
                        )
                        continue
                if not compact and _is_length_finish_error(exc):
                    compact = True
                    if "reflection_llm_retry:reason=length" not in warnings:
                        warnings.append("reflection_llm_retry:reason=length")
                    continue
                warnings.append(f"reflection_failed:{specialty}:{exc.__class__.__name__}: {exc}")
                logger.warning(
                    "adversarial_reflection specialty=%s batch_size=%s reason=%s: %s",
                    specialty,
                    len(batch),
                    exc.__class__.__name__,
                    exc,
                )
                return

    for batch in _chunk_candidates(specialty_candidates, batch_size):
        _invoke_batch(batch)

    expected_ids = {c.candidate_id for c in specialty_candidates}
    got_ids = {r.candidate_id for r in all_reports}
    missing = sorted(expected_ids - got_ids)
    for cid in missing:
        cand = next(c for c in specialty_candidates if c.candidate_id == cid)
        _invoke_batch([cand])
        got_ids = {r.candidate_id for r in all_reports}

    still_missing = sorted(expected_ids - {r.candidate_id for r in all_reports})
    for cid in still_missing:
        warnings.append(f"reflection_integrity_stub:{specialty}:{cid}")
        all_reports.append(
            ReflectionReport(
                candidate_id=cid,
                reflector_specialty=specialty,  # type: ignore[arg-type]
                verdict="needs_context",
                rationale=(
                    "reflection_integrity: no ReflectionReport returned for this candidate "
                    f"after batched {specialty} reflection; treat as needing further review."
                ),
            )
        )

    return all_reports, all_requests, warnings, llm_tokens, llm_trace


def _normalize_reports(
    state: GraphState,
    batch: ReflectionBatchOutput,
    specialty: str,
    *,
    batch_candidates: List[CandidateFinding],
) -> tuple[List[ReflectionReport], List[FocusedContextRequest], List[str]]:
    file_by_id = {c.candidate_id: c.file_path for c in batch_candidates}
    reports: List[ReflectionReport] = []
    requests: List[FocusedContextRequest] = []
    warnings: List[str] = []
    for index, raw in enumerate(batch.reports):
        report = raw.model_copy(update={"reflector_specialty": specialty})
        report, warn = _enforce_rationale_consistency(report)
        if warn:
            warnings.append(warn)
        reports.append(report)
        # Only needs_context queues graph/search work; needs_verification uses the runtime verifier.
        if report.verdict == "needs_context" and report.focused_request is not None:
            normalized = _normalize_focus_request(
                state,
                report,
                specialty,
                index,
                candidate_file_path=file_by_id.get(report.candidate_id),
            )
            if normalized is not None:
                requests.append(normalized)
                reports[-1] = report.model_copy(update={"focused_request": normalized})
    reports, dedupe_warnings = dedupe_batch_reports_per_candidate(reports)
    warnings.extend(dedupe_warnings)
    return reports, requests, warnings


def _dedupe_candidates(
    candidates: List[CandidateFinding],
    *,
    git_diff: str = "",
) -> tuple[List[CandidateFinding], Dict[str, List[str]]]:
    return dedupe_candidates_by_signature(candidates, git_diff=git_diff)


def make_adversarial_reflection_node(
    model_key: str | None = None,
    use_llm: bool = True,
    settings: Settings | None = None,
):
    node_name = "adversarial_reflection"

    def adversarial_reflection_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        resolved_settings = settings or get_settings()
        candidates: List[CandidateFinding] = []
        for raw in state.get("candidate_findings", []) or []:
            if isinstance(raw, CandidateFinding):
                candidates.append(raw)
            elif isinstance(raw, dict):
                candidates.append(CandidateFinding.model_validate(raw))
        if not candidates:
            return {"node_history": [f"{node_name}:skipped"]}

        candidates, duplicate_map = _dedupe_candidates(
            candidates,
            git_diff=(state.get("git_diff", "") or ""),
        )

        all_reports: List[ReflectionReport] = []
        all_requests: List[FocusedContextRequest] = []
        warnings: List[str] = []
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE reflection_start run_id=%s candidates=%s",
                run_id,
                len(candidates),
            )

        triage_by_candidate = _triage_items_by_candidate(state)
        if use_llm:
            selected_model = model_key or resolved_settings.reviewer_worker_model_key
            candidates_by_reflector = _candidates_by_reflector(candidates, triage_by_candidate)
            for specialty in REFLECTOR_SPECIALTIES:
                specialty_candidates = candidates_by_reflector[specialty]
                if not specialty_candidates:
                    continue
                c_ids = [c.candidate_id for c in specialty_candidates]
                from src.orchestration.prompts.ledger_formatter import format_exploration_ledger_for_prompt

                snippet, stats = format_exploration_ledger_for_prompt(
                    state.get("exploration_ledger") or [],
                    candidate_ids=c_ids,
                    max_entries=5,
                    max_chars=2000,
                )
                metadata_merged = dict(state.get("metadata", {}) or {})
                mm = dict(metadata_merged.get("mental_model_metrics") or {})
                mm["reflection_ledger_formatter_rendered"] = int(mm.get("reflection_ledger_formatter_rendered", 0)) + stats.rendered
                metadata_merged["mental_model_metrics"] = mm
                state = {**state, "metadata": metadata_merged}
                reps, reqs, batch_warnings, batch_tokens, batch_trace = _reflect_specialty_batches(
                    state=state,
                    specialty=specialty,
                    specialty_candidates=specialty_candidates,
                    selected_model=selected_model,
                    resolved_settings=resolved_settings,
                    mental_model_ledger_snippet=snippet,
                    use_llm=use_llm,
                )
                all_reports.extend(reps)
                all_requests.extend(reqs)
                warnings.extend(batch_warnings)
                llm_tokens += batch_tokens
                llm_trace.extend(batch_trace)

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE reflection_done run_id=%s reports=%s focus_requests=%s",
                run_id,
                len(all_reports),
                len(all_requests),
            )

        metadata = dict(state.get("metadata", {}))
        integrity_missing: List[str] = []
        integrity_expected: List[str] = []
        integrity_block = metadata.get("candidate_integrity", {})
        by_task = integrity_block.get("by_task", {}) if isinstance(integrity_block, dict) else {}
        if isinstance(by_task, dict):
            for entry in by_task.values():
                if isinstance(entry, dict):
                    integrity_expected.extend(entry.get("candidate_ids", []) or [])
        observed_ids = {c.candidate_id for c in candidates}
        if integrity_expected:
            deduped_ids = {cid for ids in duplicate_map.values() for cid in ids}
            missing = sorted(
                {cid for cid in integrity_expected if cid not in observed_ids and cid not in deduped_ids}
            )
            integrity_missing = missing
            if integrity_missing:
                warnings.append(f"candidate_integrity_missing:{len(integrity_missing)}")
        routed_counts = {
            specialty: len(items)
            for specialty, items in _candidates_by_reflector(candidates, triage_by_candidate).items()
        }
        metadata["adversarial_reflection"] = {
            "report_count": len(all_reports),
            "focused_request_count": len(all_requests),
            "routed_candidate_counts": routed_counts,
            "total_routed_candidate_reviews": sum(routed_counts.values()),
            "deduped_candidate_count": len(candidates),
            "dedupe_duplicates": duplicate_map,
            "integrity_expected_count": len(set(integrity_expected)),
            "integrity_missing_candidate_ids": integrity_missing,
            "warnings": warnings,
        }

        return {
            "reflection_reports": all_reports,
            "focused_context_requests": all_requests,
            "metadata": metadata,
            "node_history": [node_name],
            "token_usage": llm_tokens,
            "llm_trace": llm_trace,
        }

    return adversarial_reflection_node
