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
)
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.local_status import (
    is_local_model,
    is_timeout_exception,
    local_llm_server_active,
    sleep_for_retry,
)
from src.infrastructure.llm.token_usage import extract_total_tokens_from_llm_result, parse_structured_output
from src.orchestration.prompts.renderer import render_reviewer_prompt

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

REFLECTOR_SPECIALTIES = ("security", "logic", "performance", "general")
REFLECTOR_SPECIALTY_SET = set(REFLECTOR_SPECIALTIES)


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _serialize_candidates(candidates: List[CandidateFinding]) -> str:
    lines: List[str] = []
    for cand in candidates:
        lines.append(cand.model_dump_json())
    return "\n".join(lines)


def _candidate_reflectors(candidate: CandidateFinding) -> List[str]:
    routed = [
        specialty
        for specialty in candidate.reflection_specialties
        if specialty in REFLECTOR_SPECIALTY_SET
    ]
    if routed:
        return sorted(set(routed), key=REFLECTOR_SPECIALTIES.index)
    if candidate.suspected_category in REFLECTOR_SPECIALTY_SET:
        return [candidate.suspected_category]
    return ["general"]


def _candidates_by_reflector(candidates: List[CandidateFinding]) -> Dict[str, List[CandidateFinding]]:
    grouped: Dict[str, List[CandidateFinding]] = {specialty: [] for specialty in REFLECTOR_SPECIALTIES}
    for candidate in candidates:
        for specialty in _candidate_reflectors(candidate):
            grouped[specialty].append(candidate)
    return grouped


def _render_reflection_prompt(state: GraphState, specialty: str, candidates: List[CandidateFinding]) -> str:
    rel_path = f"reflection/{specialty}.md"
    return render_reviewer_prompt(
        rel_path,
        {
            "Reflector Specialty": specialty,
            "Candidate Findings (JSON lines)": _serialize_candidates(candidates),
            "Git Diff Excerpt": (state.get("git_diff", "") or "")[:12000],
        },
    )


def _normalize_focus_request(
    report: ReflectionReport,
    specialty: str,
    index: int,
) -> FocusedContextRequest | None:
    if report.focused_request is None:
        return None
    req = report.focused_request
    rid = req.request_id.strip() or f"{report.candidate_id}:{specialty}:focus:{index}"
    cid = req.candidate_id.strip() or report.candidate_id
    return req.model_copy(
        update={
            "request_id": rid,
            "candidate_id": cid,
            "requested_by_specialty": specialty if specialty in REFLECTOR_SPECIALTIES else "general",
        }
    )


def _normalize_reports(batch: ReflectionBatchOutput, specialty: str) -> tuple[List[ReflectionReport], List[FocusedContextRequest]]:
    reports: List[ReflectionReport] = []
    requests: List[FocusedContextRequest] = []
    for index, raw in enumerate(batch.reports):
        report = raw.model_copy(update={"reflector_specialty": specialty})
        reports.append(report)
        # Only needs_context queues graph/search work; needs_verification uses the runtime verifier.
        if report.verdict == "needs_context" and report.focused_request is not None:
            normalized = _normalize_focus_request(report, specialty, index)
            if normalized is not None:
                requests.append(normalized)
                reports[-1] = report.model_copy(update={"focused_request": normalized})
    return reports, requests


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

        all_reports: List[ReflectionReport] = []
        all_requests: List[FocusedContextRequest] = []
        warnings: List[str] = []
        llm_tokens = 0

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE reflection_start run_id=%s candidates=%s",
                run_id,
                len(candidates),
            )

        if use_llm:
            selected_model = model_key or resolved_settings.reviewer_worker_model_key
            candidates_by_reflector = _candidates_by_reflector(candidates)
            for specialty in REFLECTOR_SPECIALTIES:
                specialty_candidates = candidates_by_reflector[specialty]
                if not specialty_candidates:
                    continue
                timeout_deadline = (
                    time.monotonic() + resolved_settings.reviewer_reflection_timeout_patience_seconds
                    if is_local_model(selected_model)
                    and resolved_settings.reviewer_reflection_timeout_patience_seconds > 0
                    else None
                )
                attempt = 0
                while True:
                    attempt += 1
                    try:
                        llm = Models.worker(ReflectionBatchOutput, model_key=selected_model)
                        invoke_result = llm.invoke(
                            _render_reflection_prompt(state, specialty, specialty_candidates)
                        )
                        response = parse_structured_output(invoke_result, ReflectionBatchOutput)
                        llm_tokens += extract_total_tokens_from_llm_result(invoke_result)
                        reps, reqs = _normalize_reports(response, specialty)
                        all_reports.extend(reps)
                        all_requests.extend(reqs)
                        warnings.extend(response.warnings)
                        break
                    except Exception as exc:  # noqa: BLE001
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
                                remaining = max(0, int(timeout_deadline - time.monotonic()))
                                logger.warning(
                                    "%s timeout but server active specialty=%s run_id=%s "
                                    "attempt=%s patience_remaining_seconds=%s status=%s err=%s",
                                    node_name,
                                    specialty,
                                    run_id,
                                    attempt,
                                    remaining,
                                    status_detail,
                                    exc,
                                )
                                sleep_for_retry(
                                    resolved_settings.reviewer_reflection_retry_backoff_seconds,
                                    attempt,
                                    timeout_deadline,
                                )
                                continue
                            logger.warning(
                                "%s timeout and server status inactive specialty=%s run_id=%s "
                                "attempt=%s status=%s err=%s",
                                node_name,
                                specialty,
                                run_id,
                                attempt,
                                status_detail,
                                exc,
                            )
                        warning = f"reflection_failed:{specialty}:{exc.__class__.__name__}: {exc}"
                        warnings.append(warning)
                        logger.warning(
                            "%s specialty=%s run_id=%s reason=%s: %s",
                            node_name,
                            specialty,
                            run_id,
                            exc.__class__.__name__,
                            exc,
                        )
                        break

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE reflection_done run_id=%s reports=%s focus_requests=%s",
                run_id,
                len(all_reports),
                len(all_requests),
            )

        metadata = dict(state.get("metadata", {}))
        routed_counts = {
            specialty: len(items)
            for specialty, items in _candidates_by_reflector(candidates).items()
        }
        metadata["adversarial_reflection"] = {
            "report_count": len(all_reports),
            "focused_request_count": len(all_requests),
            "routed_candidate_counts": routed_counts,
            "total_routed_candidate_reviews": sum(routed_counts.values()),
            "warnings": warnings,
        }

        return {
            "reflection_reports": all_reports,
            "focused_context_requests": all_requests,
            "metadata": metadata,
            "node_history": [node_name],
            "token_usage": llm_tokens,
        }

    return adversarial_reflection_node
