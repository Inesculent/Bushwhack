"""Orchestration routing helpers (adversarial reflection, task dispatch)."""

from __future__ import annotations

from src.orchestration.routing.candidate_reflection_specialty import (
    normalize_reflection_specialty_hardcap,
    with_single_reflection_specialty,
)
from src.orchestration.routing.critiquer_focus import auto_focus_requests, candidate_needs_auto_context
from src.orchestration.routing.normalize_critiquer_candidates import normalize_critiquer_candidates

__all__ = [
    "auto_focus_requests",
    "candidate_needs_auto_context",
    "normalize_critiquer_candidates",
    "normalize_reflection_specialty_hardcap",
    "with_single_reflection_specialty",
]
