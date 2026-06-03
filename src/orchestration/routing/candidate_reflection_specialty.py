"""Conservative specialty fallback for candidates before LLM triage."""

from __future__ import annotations

from typing import Literal

from src.domain.schemas import CandidateFinding

ReflectorSpecialty = Literal["security", "logic", "performance", "general"]

SPECIALTY_PRIORITY: tuple[ReflectorSpecialty, ...] = ("security", "logic", "performance", "general")
_SPECIALTY_RANK = {s: i for i, s in enumerate(SPECIALTY_PRIORITY)}


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
    if candidate.claim_type == "defect":
        return "logic"
    return "general"


def normalize_reflection_specialty_hardcap(candidate: CandidateFinding) -> ReflectorSpecialty:
    """Return exactly one fallback specialty without text-keyword claim inference."""
    raw = list(candidate.reflection_specialties)
    if len(raw) == 1 and raw[0] in _SPECIALTY_RANK:
        return raw[0]  # type: ignore[return-value]
    if len(raw) > 1:
        picked = _highest_priority_specialty(raw)
        if picked is not None:
            return picked
    return _infer_specialty_when_empty(candidate)


def correct_specialty_before_hardcap(candidate: CandidateFinding) -> tuple[CandidateFinding, str | None]:
    """Align only explicit claim_type categories; do not rewrite from text markers."""
    if candidate.claim_type == "security_risk" and candidate.reflection_specialties != ["security"]:
        return (
            candidate.model_copy(
                update={
                    "reflection_specialties": ["security"],
                    "suspected_category": "security",
                }
            ),
            "specialty_corrected:security_risk",
        )
    if candidate.claim_type == "performance_regression" and candidate.reflection_specialties != ["performance"]:
        return (
            candidate.model_copy(
                update={
                    "reflection_specialties": ["performance"],
                    "suspected_category": "performance",
                }
            ),
            "specialty_corrected:performance_regression",
        )
    return candidate, None


def with_single_reflection_specialty(candidate: CandidateFinding) -> CandidateFinding:
    """Copy candidate with ``reflection_specialties`` set to a single fallback specialty."""
    corrected, _ = correct_specialty_before_hardcap(candidate)
    specialty = normalize_reflection_specialty_hardcap(corrected)
    return corrected.model_copy(update={"reflection_specialties": [specialty]})
