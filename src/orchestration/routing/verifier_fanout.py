"""Routing helpers: fan out verifier Send branches after focused_context."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from langgraph.types import Send

from src.config import get_settings
from src.infrastructure.sandbox import sandbox_runtime_available
from src.domain.schemas import CandidateFinding, ReviewEvidenceTriageItem, SourceFact
from src.domain.state import GraphState
from src.domain.verifier_schemas import VerifierReport
from src.orchestration.routing.send_payload import payload_for_send
from src.orchestration.nodes.application.critique_revision import (
    _candidate_ids_needs_verification,
    _has_focused_evidence,
    _needs_revision_candidates,
)
from src.orchestration.context.task_evidence import task_evidence_slot_from_state
from src.orchestration.nodes.verifier.source_only import extract_source_facts_for_candidate

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
        if _task_evidence_has_candidate_file(state, cand):
            out.add(cand.candidate_id)
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


def _existing_source_fact_ids(state: GraphState) -> set[str]:
    out: set[str] = set()
    for raw in state.get("source_facts") or []:
        fact = raw
        if isinstance(raw, dict):
            try:
                fact = SourceFact.model_validate(raw)
            except Exception:
                continue
        if isinstance(fact, SourceFact) and fact.candidate_id:
            out.add(fact.candidate_id)
    metadata = state.get("metadata") or {}
    verifier_meta = metadata.get("verifier") if isinstance(metadata.get("verifier"), dict) else {}
    by_candidate = verifier_meta.get("source_facts_by_candidate") if isinstance(verifier_meta, dict) else {}
    if isinstance(by_candidate, dict):
        out.update(str(candidate_id) for candidate_id in by_candidate if str(candidate_id))
    return out


def _triage_by_candidate(state: GraphState) -> dict[str, ReviewEvidenceTriageItem]:
    metadata = state.get("metadata") or {}
    triage = metadata.get("review_evidence_triage") if isinstance(metadata, dict) else {}
    items = triage.get("items") if isinstance(triage, dict) else []
    out: dict[str, ReviewEvidenceTriageItem] = {}
    for raw in items or []:
        item = raw
        if isinstance(raw, dict):
            try:
                item = ReviewEvidenceTriageItem.model_validate(raw)
            except Exception:
                continue
        if isinstance(item, ReviewEvidenceTriageItem) and item.candidate_id:
            out[item.candidate_id] = item
    return out


def _triage_source_fact_requested_ids(state: GraphState) -> set[str]:
    return {
        candidate_id
        for candidate_id, item in _triage_by_candidate(state).items()
        if item.source_fact_requests
    }


def _runtime_verification_allowed(state: GraphState, candidate_id: str) -> bool:
    item = _triage_by_candidate(state).get(candidate_id)
    if item is None:
        return True
    return item.runtime_verification_usefulness != "not_useful"


def _source_only_candidate_ids(state: GraphState) -> set[str]:
    return (
        set(_needs_revision_candidates(state))
        | set(_candidate_ids_needs_verification(state))
        | _concrete_source_local_missing_test_ids(state)
        | _triage_source_fact_requested_ids(state)
    )


def collect_source_only_verifier_updates(state: GraphState) -> Dict[str, Any]:
    """Collect static source facts that do not require sandbox runtime."""
    settings = get_settings()
    if not settings.verifier_enabled or not getattr(settings, "verifier_source_only_static_enabled", True):
        return {}

    need_ids = _source_only_candidate_ids(state) - _existing_verifier_report_ids(state) - _existing_source_fact_ids(state)
    if not need_ids:
        return {}

    facts = []
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
        facts.extend(extract_source_facts_for_candidate(state, cand.model_dump(mode="json")))
    if not facts:
        return {}

    metadata = dict(state.get("metadata") or {})
    verifier_meta = dict(metadata.get("verifier") or {})
    by_candidate = dict(verifier_meta.get("source_facts_by_candidate") or {})
    for fact in facts:
        by_candidate.setdefault(fact.candidate_id, [])
        by_candidate[fact.candidate_id].append(fact.model_dump(mode="json"))
    verifier_meta["source_facts_by_candidate"] = by_candidate
    metadata["verifier"] = verifier_meta
    return {
        "source_facts": facts,
        "metadata": metadata,
        "node_history": ["source_fact_extractor"],
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
        if not _runtime_verification_allowed(state, cand.candidate_id):
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
        from src.orchestration.routing.send_payload import subgraph_parent_updates

        result = inner.invoke(state)
        return subgraph_parent_updates(
            state,
            result,
            keys=_PARENT_KEYS,
            additive_lists={"verifier_reports", "llm_trace", "node_history"},
            additive_ints={"token_usage"},
        )

    return verifier_subgraph_node
