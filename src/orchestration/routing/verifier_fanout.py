"""Routing helpers: fan out verifier Send branches after focused_context."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import docker
from langgraph.types import Send

from src.config import get_settings
from src.domain.schemas import CandidateFinding, FocusedContextResult
from src.domain.state import GraphState
from src.domain.verifier_schemas import VerifierReport
from src.orchestration.routing.send_payload import payload_for_send
from src.orchestration.nodes.application.critique_revision import (
    _candidate_ids_needs_verification,
    _has_focused_evidence,
    _needs_revision_candidates,
)
from src.orchestration.nodes.verifier.verifier_runner import invoke_verifier_for_candidate

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


def _docker_available() -> bool:
    try:
        return bool(docker.from_env().ping())
    except Exception:  # noqa: BLE001
        return False


def _claim_type_eligible(candidate: CandidateFinding, settings) -> bool:
    ct = candidate.claim_type
    if ct == "defect" and settings.verifier_run_on_defect:
        return True
    if ct == "security_risk" and settings.verifier_run_on_security:
        return True
    if ct == "performance_regression" and settings.verifier_run_on_performance:
        return True
    return False


def focused_context_text_for_candidate(state: GraphState, candidate_id: str, *, max_chars: int | None = None) -> str:
    """Concat focused JSON blobs for one candidate (bounded)."""
    settings = get_settings()
    if max_chars is None:
        max_chars = min(120_000, max(24_000, int(settings.review_full_file_max_total_chars)))
    chunks: List[str] = []
    for raw in (state.get("focused_context_results", {}) or {}).values():
        res: FocusedContextResult | None
        if isinstance(raw, FocusedContextResult):
            res = raw
        elif isinstance(raw, dict):
            try:
                res = FocusedContextResult.model_validate(raw)
            except Exception:  # noqa: BLE001
                res = None
        else:
            res = None
        if res is None or res.candidate_id != candidate_id:
            continue
        chunks.append(res.model_dump_json())
    blob = "\n\n".join(chunks)
    if len(blob) > max_chars:
        return blob[:max_chars] + "\n... [truncated]"
    return blob


def collect_verifier_send_payloads(state: GraphState) -> List[Send]:
    """Build Send targets for verifier_subgraph (possibly empty)."""
    settings = get_settings()
    if not settings.verifier_enabled:
        return []

    if settings.verifier_skip_if_no_docker and not _docker_available():
        logger.info("Verifier skipped: Docker not available.")
        return []

    need_ids = set(_needs_revision_candidates(state))
    if not need_ids:
        return []
    if settings.verifier_require_focused_evidence and not _has_focused_evidence(state, sorted(need_ids)):
        nv_ids = _candidate_ids_needs_verification(state)
        need_ids = need_ids & nv_ids
        if not need_ids:
            return []

    eligible: List[CandidateFinding] = []
    for raw in state.get("candidate_findings", []) or []:
        cand = _coerce_candidate(raw)
        if cand is None or cand.candidate_id not in need_ids:
            continue
        if not _claim_type_eligible(cand, settings):
            continue
        eligible.append(cand)

    eligible.sort(key=lambda c: c.candidate_id)
    budget = max(1, settings.verifier_total_budget_per_pr)
    eligible = eligible[:budget]

    sends: List[Send] = []
    for cand in eligible:
        payload = payload_for_send(state, verifier_candidate=cand.model_dump(mode="json"))
        sends.append(Send("verifier_subgraph", payload))
    return sends


def make_verifier_subgraph_node():
    """Run verifier for the ``verifier_candidate`` carried on a Send branch."""

    node_name = "verifier_subgraph"

    def verifier_subgraph_node(state: GraphState) -> Dict[str, Any]:
        raw = state.get("verifier_candidate")
        if not raw:
            return {"node_history": [f"{node_name}:skipped"]}

        try:
            cand = CandidateFinding.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            return {
                "node_history": [f"{node_name}:invalid_candidate:{exc.__class__.__name__}"],
            }

        fc = focused_context_text_for_candidate(state, cand.candidate_id)
        git_excerpt = (state.get("git_diff", "") or "")[:8000]
        report = invoke_verifier_for_candidate(
            run_id=str(state.get("run_id", "")),
            repo_path=str(state.get("repo_path", "")),
            candidate=cand,
            focused_context_snippets=fc,
            git_diff_excerpt=git_excerpt,
            graph_state=state,
        )

        meta = dict(state.get("metadata") or {})
        hints = dict(meta.get("verifier_hints") or {})
        hints[report.candidate_id] = {
            "verdict": report.verdict,
            "verification_scope": report.verification_scope,
            "updated_evidence_summary": report.updated_evidence_summary,
            "final_rationale": report.final_rationale,
            "attempts": len(report.attempts),
            "skipped_reason": report.skipped_reason,
            "lint_advisory": _lint_advisory_from_report(report),
        }
        meta["verifier_hints"] = hints
        vrun = dict(meta.get("verifier") or {})
        by_c = dict(vrun.get("by_candidate") or {})
        by_c[report.candidate_id] = report.model_dump(mode="json")
        vrun["by_candidate"] = by_c
        meta["verifier"] = vrun

        tokens = 0
        if isinstance(report.metadata, dict):
            tokens = int(report.metadata.get("llm_tokens") or 0)

        return {
            "verifier_reports": [report],
            "metadata": meta,
            "token_usage": tokens,
            "node_history": [node_name],
        }

    return verifier_subgraph_node
