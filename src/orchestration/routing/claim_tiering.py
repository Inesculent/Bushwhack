"""Heuristic claim-quality tiers for cleanup and revision routing.

The tier is deliberately advisory. It prevents generic checklist claims from
getting the same treatment as source-local regressions without making the
runtime verifier a hard dependency.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Literal, Mapping

from src.domain.schemas import CandidateFinding

ClaimTier = Literal[
    "direct_regression",
    "contract_regression",
    "runtime_probe",
    "coverage_gap",
    "speculative_guard",
]

_DIRECT_MARKERS = re.compile(
    r"\b("
    r"undefined|nameerror|syntaxerror|unboundlocal|overwrites?|inverted|unreachable|"
    r"missing_return|implicit none|falls? through|terminal else|"
    r"wrong\s+(?:output|value|branch|index|slot)|data loss|drops?|discard|"
    r"missing import|removed import|still uses|injection|escape|escaping|"
    r"division by zero|off-by-one|attributeerror|typeerror|keyerror"
    r")\b",
    re.IGNORECASE,
)
_CONTRACT_MARKERS = re.compile(
    r"\b(contract|return_types|return type|api|schema|alias|default|compat|"
    r"backward compatibility|public interface|serialization|configuration)\b",
    re.IGNORECASE,
)
_RUNTIME_MARKERS = re.compile(
    r"\b(runtime|crash|hang|timeout|deadlock|race|concurrent|async|subprocess|"
    r"network|socket|browser|http|websocket|gpu|device mismatch|unbounded|"
    r"expensive|excessive|memory|resource|external request|public request)\b",
    re.IGNORECASE,
)
_COVERAGE_MARKERS = re.compile(r"\b(test|coverage|missing test|add tests?)\b", re.IGNORECASE)
_GENERIC_GUARD_MARKERS = re.compile(
    r"\b(validate|guard|sanitize|null|none|check\s+(?:for|that|whether))\b",
    re.IGNORECASE,
)
_CONCRETE_HARM_MARKERS = re.compile(
    r"\b(causes?|will|prevents?|breaks?|fails?|crashes?|leaks?|exposes?|"
    r"incorrect|wrong|data loss|security|attacker|injection|unauthorized)\b",
    re.IGNORECASE,
)
_SECURITY_BOUNDARY_MARKERS = re.compile(
    r"\b(attacker|untrusted|user-controlled|auth|permission|tenant|sql|command|"
    r"injection|secret|token|credential|cookie|session|deserialization)\b",
    re.IGNORECASE,
)


def _blob(candidate: CandidateFinding) -> str:
    return " ".join(
        [
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            candidate.recommendation or "",
            " ".join(candidate.required_context),
        ]
    )


def security_boundary_is_concrete(candidate: CandidateFinding) -> bool:
    """True when a security-looking claim names an actual trust boundary or sink."""
    return bool(_SECURITY_BOUNDARY_MARKERS.search(_blob(candidate)))


def classify_claim_tier(
    candidate: CandidateFinding,
    *,
    review_kb_context: str = "",
) -> ClaimTier:
    """Classify candidate support quality without requiring verifier success."""
    text = _blob(candidate)
    lower = text.lower()

    if candidate.claim_type == "missing_test" or _COVERAGE_MARKERS.search(text):
        if _DIRECT_MARKERS.search(text) or _CONCRETE_HARM_MARKERS.search(text):
            return "coverage_gap"
        return "speculative_guard"

    if candidate.claim_type == "security_risk" and security_boundary_is_concrete(candidate):
        return "direct_regression" if _DIRECT_MARKERS.search(text) else "runtime_probe"

    if _DIRECT_MARKERS.search(text):
        return "direct_regression"

    if _CONTRACT_MARKERS.search(text):
        if review_kb_context.strip() and "(no review kb matches)" not in review_kb_context.lower():
            return "contract_regression"
        if _CONCRETE_HARM_MARKERS.search(text):
            return "contract_regression"

    if _RUNTIME_MARKERS.search(text) or candidate.required_context:
        return "runtime_probe"

    if _GENERIC_GUARD_MARKERS.search(text) and not _CONCRETE_HARM_MARKERS.search(text):
        return "speculative_guard"

    if any(term in lower for term in ("might", "could", "consider", "may be")):
        return "speculative_guard"

    return "runtime_probe"


def review_kb_context_for_candidate(
    metadata: Mapping[str, Any],
    candidate: CandidateFinding,
) -> str:
    """Best-effort lookup of the task-scoped KB excerpt for a candidate."""
    pipe = metadata.get("critique_pipeline") if isinstance(metadata, Mapping) else None
    if not isinstance(pipe, Mapping):
        return ""
    by_task = pipe.get("by_task")
    if not isinstance(by_task, Mapping):
        return ""
    slot = by_task.get(candidate.patch_task_id)
    if not isinstance(slot, Mapping):
        return ""
    return str(slot.get("review_kb_excerpt") or "")


def classify_candidates(
    candidates: list[CandidateFinding],
    *,
    metadata: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Return serializable tier metadata for all candidates."""
    out: Dict[str, Dict[str, Any]] = {}
    for candidate in candidates:
        kb_context = review_kb_context_for_candidate(metadata, candidate)
        tier = classify_claim_tier(candidate, review_kb_context=kb_context)
        out[candidate.candidate_id] = {
            "tier": tier,
            "claim_type": candidate.claim_type,
            "suspected_category": candidate.suspected_category,
            "has_required_context": bool(candidate.required_context),
            "has_review_kb_context": bool(
                kb_context.strip() and "(no review kb matches)" not in kb_context.lower()
            ),
        }
    return out
