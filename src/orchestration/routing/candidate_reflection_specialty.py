"""Deterministic single-specialty routing for adversarial reflection (hardcap).

Hierarchy when multiple domains appear: security > logic > performance > general.
"""

from __future__ import annotations

from typing import Literal

from src.domain.schemas import CandidateFinding

ReflectorSpecialty = Literal["security", "logic", "performance", "general"]

# Lower index = higher priority in tie-breaks.
SPECIALTY_PRIORITY: tuple[ReflectorSpecialty, ...] = ("security", "logic", "performance", "general")
_SPECIALTY_RANK = {s: i for i, s in enumerate(SPECIALTY_PRIORITY)}

_SECURITY_HINTS = (
    "redos",
    "backtrack",
    "catastrophic backtracking",
    "injection",
    "xss",
    "csrf",
    "ssrf",
    "path traversal",
    "command injection",
    "eval(",
    "exec(",
    "pickle",
    "deserialize",
    "credential",
    "password",
    "secret ",
    "token leak",
    "auth",
    "authorize",
    "permission",
    "tenant isolation",
    "sql injection",
)

_PERFORMANCE_HINTS = (
    "o(n^2)",
    "o(n²)",
    "quadratic",
    "n+1",
    "memory leak",
    "unbounded loop",
    "scalability",
)


def _highest_priority_specialty(tags: list[str]) -> ReflectorSpecialty | None:
    valid = [t for t in tags if t in _SPECIALTY_RANK]
    if not valid:
        return None
    return min(valid, key=lambda t: _SPECIALTY_RANK[t])  # type: ignore[arg-type, return-value]


def _infer_specialty_when_empty(candidate: CandidateFinding) -> ReflectorSpecialty:
    if candidate.claim_type == "security_risk":
        return "security"
    if candidate.claim_type == "performance_regression":
        return "performance"
    if candidate.claim_type == "missing_test":
        return "general"
    if candidate.suspected_category in _SPECIALTY_RANK:
        return candidate.suspected_category  # type: ignore[return-value]

    blob = " ".join(
        [
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
        ]
    ).lower()

    if any(h in blob for h in _SECURITY_HINTS):
        return "security"
    if any(h in blob for h in _PERFORMANCE_HINTS):
        return "performance"
    if candidate.claim_type == "defect":
        return "logic"
    return "general"


def normalize_reflection_specialty_hardcap(candidate: CandidateFinding) -> ReflectorSpecialty:
    """Return exactly one specialty using hierarchy tie-breaks."""
    raw = list(candidate.reflection_specialties)
    if len(raw) == 1 and raw[0] in _SPECIALTY_RANK:
        return raw[0]  # type: ignore[return-value]
    if len(raw) > 1:
        picked = _highest_priority_specialty(raw)
        if picked is not None:
            return picked
    return _infer_specialty_when_empty(candidate)


def with_single_reflection_specialty(candidate: CandidateFinding) -> CandidateFinding:
    """Copy candidate with ``reflection_specialties`` set to a single canonical specialty."""
    specialty = normalize_reflection_specialty_hardcap(candidate)
    return candidate.model_copy(update={"reflection_specialties": [specialty]})
