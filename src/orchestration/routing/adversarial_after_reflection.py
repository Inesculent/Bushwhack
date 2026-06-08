"""Routing after adversarial_reflection (focused fetch vs verifier/critique bridge vs cleanup)."""

from __future__ import annotations

from src.domain.schemas import ReflectionReport
from src.domain.state import GraphState
from src.orchestration.nodes.application.critique_revision import _needs_revision_candidates


def final_adversarial_review_node() -> str:
    return "review_adjudicator"


def route_focused_after_reflection(state: GraphState) -> str:
    """Return next node name after reflection evidence routing."""
    reports = state.get("reflection_reports", []) or []
    for raw in reports:
        report: ReflectionReport | None
        if isinstance(raw, ReflectionReport):
            report = raw
        elif isinstance(raw, dict):
            try:
                report = ReflectionReport.model_validate(raw)
            except Exception:  # noqa: BLE001
                report = None
        else:
            report = None
        if report is not None and report.verdict == "needs_verification":
            return "post_reflection_evidence_pass"
    for raw in reports:
        report: ReflectionReport | None
        if isinstance(raw, ReflectionReport):
            report = raw
        elif isinstance(raw, dict):
            try:
                report = ReflectionReport.model_validate(raw)
            except Exception:  # noqa: BLE001
                report = None
        else:
            report = None
        if report is not None and report.verdict == "needs_context" and report.focused_request is not None:
            return "focused_context"
    if _needs_revision_candidates(state):
        return "post_reflection_evidence_pass"
    return final_adversarial_review_node()
