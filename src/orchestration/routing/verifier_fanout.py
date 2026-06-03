"""Routing helpers: fan out verifier Send branches after focused_context."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from langgraph.types import Send

from src.config import get_settings
from src.infrastructure.sandbox import sandbox_runtime_available
from src.domain.schemas import CandidateFinding
from src.domain.state import GraphState
from src.domain.verifier_schemas import VerifierReport
from src.orchestration.routing.send_payload import payload_for_send
from src.orchestration.nodes.application.critique_revision import (
    _candidate_ids_needs_verification,
    _has_focused_evidence,
    _needs_revision_candidates,
)
from src.orchestration.context.task_evidence import task_evidence_slot_from_state
from src.orchestration.nodes.verifier.source_only import source_only_verify_candidate
from src.orchestration.routing.finding_dedupe import candidate_with_behavioral_metadata

logger = logging.getLogger(__name__)


def _lint_advisory_from_report(report: VerifierReport) -> str:
    if not report.attempts:
        return ""
    last = report.attempts[-1]
    if not last.lint_runs:
        return ""
    parts: List[str] = []
    for lr in last.lint_runs:
        parts.append(
            f"[{lr.tool}] exit={lr.exit_code}\nstdout:\n{lr.stdout}\nstderr:\n{lr.stderr}".strip()
        )
    text = "\n\n---\n\n".join(parts)
    if len(text) > 8000:
        return text[:8000] + "\n... [truncated]"
    return text


def _coerce_candidate(raw: object) -> CandidateFinding | None:
    if isinstance(raw, CandidateFinding):
        return raw
    if isinstance(raw, dict):
        try:
            return CandidateFinding.model_validate(raw)
        except Exception:  # noqa: BLE001
            return None
    return None


_CONCRETE_VERIFIER_SYMPTOMS = frozenset(
    {
        "wrong_output",
        "data_loss",
        "crash",
        "missing_return",
        "uncaught_exception",
        "contract_mismatch",
    }
)


def _claim_type_eligible(
    candidate: CandidateFinding,
    settings,
    *,
    source_local_missing_test: bool = False,
) -> bool:
    ct = candidate.claim_type
    if ct == "defect" and settings.verifier_run_on_defect:
        return True
    if ct == "missing_test" and source_local_missing_test and settings.verifier_run_on_defect:
        return True
    if ct == "security_risk" and settings.verifier_run_on_security:
        return True
    if ct == "performance_regression" and settings.verifier_run_on_performance:
        return True
    return False


def focused_context_text_for_candidate(state: GraphState, candidate_id: str, *, max_chars: int | None = None) -> str:
    """Bounded focused snippets for one candidate (tier-2 tool results, not raw JSON dumps)."""
    from src.orchestration.context.context_packets import focused_snippets_for_candidate

    return focused_snippets_for_candidate(state, candidate_id, max_chars=max_chars)


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("/")


def _task_evidence_has_candidate_file(state: GraphState, candidate: CandidateFinding) -> bool:
    slot = task_evidence_slot_from_state(state, candidate.patch_task_id)
    fp = _norm(candidate.file_path)
    if not fp:
        return False
    for key in ("file_contents", "rendered_units"):
        files = slot.get(key) if isinstance(slot.get(key), dict) else {}
        for raw_path, body in files.items():
            path = _norm(str(raw_path))
            if body and (path == fp or path.endswith("/" + fp) or fp.endswith("/" + path)):
                return True
    return False


def _accepted_or_verification_requested_ids(state: GraphState) -> set[str]:
    out: set[str] = set()
    for raw in state.get("reflection_reports", []) or []:
        report = raw
        verdict = str(getattr(report, "verdict", "") or "")
        candidate_id = str(getattr(report, "candidate_id", "") or "")
        if isinstance(raw, dict):
            verdict = str(raw.get("verdict") or "")
            candidate_id = str(raw.get("candidate_id") or "")
        if verdict in {"accept", "reclassify", "needs_context", "needs_verification"} and candidate_id:
            out.add(candidate_id)
    return out


def _concrete_source_local_missing_test_ids(state: GraphState) -> set[str]:
    supported_ids = _accepted_or_verification_requested_ids(state)
    out: set[str] = set()
    for raw in state.get("candidate_findings", []) or []:
        cand = _coerce_candidate(raw)
        if cand is None or cand.claim_type != "missing_test" or cand.candidate_id not in supported_ids:
            continue
        normalized = candidate_with_behavioral_metadata(cand)
        if normalized.behavioral_symptom not in _CONCRETE_VERIFIER_SYMPTOMS:
            continue
        if _task_evidence_has_candidate_file(state, normalized):
            out.add(normalized.candidate_id)
    return out


def _existing_verifier_report_ids(state: GraphState) -> set[str]:
    out: set[str] = set()
    for raw in state.get("verifier_reports", []) or []:
        if isinstance(raw, VerifierReport):
            out.add(raw.candidate_id)
            continue
        if isinstance(raw, dict):
            candidate_id = str(raw.get("candidate_id") or "")
            if candidate_id:
                out.add(candidate_id)
    return out


def _source_only_candidate_ids(state: GraphState) -> set[str]:
    return (
        set(_needs_revision_candidates(state))
        | set(_candidate_ids_needs_verification(state))
        | _concrete_source_local_missing_test_ids(state)
    )


def _source_only_report_for_candidate(state: GraphState, candidate: CandidateFinding) -> VerifierReport | None:
    verdict, rationale, attempt = source_only_verify_candidate(state, candidate.model_dump(mode="json"))
    if verdict != "verified" or attempt is None:
        return None
    return VerifierReport(
        run_id=str(state.get("run_id") or ""),
        candidate_id=candidate.candidate_id,
        verdict="verified",
        verification_scope="concrete_behavior",
        final_rationale=rationale,
        updated_evidence_summary=f"Source-only verifier: {rationale}",
        attempts=[attempt],
        metadata={
            "harness_error": False,
            "product_verified": True,
            "source_only_static": True,
        },
    )


def collect_source_only_verifier_updates(state: GraphState) -> Dict[str, Any]:
    """Run static verifier proofs that do not require sandbox runtime."""
    settings = get_settings()
    if not settings.verifier_enabled or not getattr(settings, "verifier_source_only_static_enabled", True):
        return {}

    need_ids = _source_only_candidate_ids(state) - _existing_verifier_report_ids(state)
    if not need_ids:
        return {}

    reports: List[VerifierReport] = []
    source_local_missing_test_ids = _concrete_source_local_missing_test_ids(state)
    for raw in state.get("candidate_findings", []) or []:
        cand = _coerce_candidate(raw)
        if cand is None or cand.candidate_id not in need_ids:
            continue
        if not _claim_type_eligible(
            cand,
            settings,
            source_local_missing_test=cand.candidate_id in source_local_missing_test_ids,
        ):
            continue
        report = _source_only_report_for_candidate(state, cand)
        if report is not None:
            reports.append(report)
    if not reports:
        return {}

    metadata = dict(state.get("metadata") or {})
    hints = dict(metadata.get("verifier_hints") or {})
    verifier_meta = dict(metadata.get("verifier") or {})
    by_candidate = dict(verifier_meta.get("by_candidate") or {})
    failure_summary = dict(verifier_meta.get("failure_summary_by_candidate") or {})
    for report in reports:
        hints[report.candidate_id] = {
            "verdict": report.verdict,
            "verification_scope": report.verification_scope,
            "updated_evidence_summary": report.updated_evidence_summary,
            "final_rationale": report.final_rationale,
            "attempts": len(report.attempts),
            "skipped_reason": report.skipped_reason,
            "lint_advisory": _lint_advisory_from_report(report),
            "harness_error": False,
            "product_verified": True,
            "confidence": "product_verified",
            "failure_classes": [a.failure_class for a in report.attempts if a.failure_class],
            "top_missing_modules": [],
            "verifier_env_repair_hints_used": False,
            "verifier_repeated_harness_error_count": 0,
            "verifier_unrepaired_missing_modules": [],
            "source_only_static": True,
        }
        by_candidate[report.candidate_id] = report.model_dump(mode="json")
        failure_summary[report.candidate_id] = {
            "verdict": report.verdict,
            "attempt_count": len(report.attempts),
            "failure_classes": [a.failure_class or "unknown" for a in report.attempts],
            "product_verified": True,
            "harness_error": False,
            "source_only_static": True,
        }
    metadata["verifier_hints"] = hints
    verifier_meta["by_candidate"] = by_candidate
    verifier_meta["failure_summary_by_candidate"] = failure_summary
    metadata["verifier"] = verifier_meta
    return {
        "verifier_reports": reports,
        "metadata": metadata,
        "node_history": ["source_only_verifier"],
    }


def collect_verifier_send_payloads(state: GraphState) -> List[Send]:
    """Build Send targets for verifier_subgraph (possibly empty)."""
    settings = get_settings()
    if not settings.verifier_enabled:
        return []

    if settings.verifier_skip_if_no_sandbox and not sandbox_runtime_available(settings):
        logger.info(
            "Verifier skipped: sandbox runtime not available (backend=%s).",
            settings.sandbox_backend,
        )
        return []

    source_local_missing_test_ids = _concrete_source_local_missing_test_ids(state)
    need_ids = set(_needs_revision_candidates(state)) | source_local_missing_test_ids
    need_ids -= _existing_verifier_report_ids(state)
    if not need_ids:
        return []
    if settings.verifier_require_focused_evidence and not _has_focused_evidence(state, sorted(need_ids)):
        nv_ids = _candidate_ids_needs_verification(state)
        need_ids = need_ids & (nv_ids | source_local_missing_test_ids)
        if not need_ids:
            return []

    eligible: List[CandidateFinding] = []
    for raw in state.get("candidate_findings", []) or []:
        cand = _coerce_candidate(raw)
        if cand is None or cand.candidate_id not in need_ids:
            continue
        if not _claim_type_eligible(
            cand,
            settings,
            source_local_missing_test=cand.candidate_id in source_local_missing_test_ids,
        ):
            continue
        eligible.append(cand)

    eligible.sort(key=lambda c: c.candidate_id)
    budget = max(1, settings.verifier_total_budget_per_pr)
    eligible = eligible[:budget]

    sends: List[Send] = []
    for cand in eligible:
        # Isolate per-branch token accounting: do not copy parent cumulative token_usage.
        payload = payload_for_send(
            state,
            verifier_candidate=cand.model_dump(mode="json"),
            token_usage=0,
        )
        sends.append(Send("verifier_subgraph", payload))
    return sends


def make_verifier_subgraph_node():
    """Run verifier for the ``verifier_candidate`` carried on a Send branch using a multi-node subgraph."""
    from src.orchestration.verifier_graph import build_verifier_graph

    inner = build_verifier_graph()

    _PARENT_KEYS = frozenset(
        {
            "verifier_reports",
            "metadata",
            "llm_trace",
            "token_usage",
            "node_history",
        }
    )

    def verifier_subgraph_node(state: GraphState) -> Dict[str, Any]:
        result = inner.invoke(state)
        # Only return keys that should be merged back into the parent GraphState
        return {k: result[k] for k in _PARENT_KEYS if k in result}

    return verifier_subgraph_node
