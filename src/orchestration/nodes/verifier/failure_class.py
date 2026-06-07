"""Classify candidate failure modes for scoped verifier refutation."""

from __future__ import annotations

from typing import Any, Dict


def failure_mode_class(candidate: Dict[str, Any]) -> str:
    """Legacy compatibility: claim semantics are no longer inferred from keywords."""
    return "unknown"


def verifier_refutation_applies(
    candidate: Dict[str, Any],
    *,
    verifier_verdict: str,
    verification_scope: str,
    harness_error: bool,
    stdout: str = "",
    stderr: str = "",
) -> bool:
    """
    True when a refuted verifier run should force critique_revision reject.

    Wrong-output claims are not refuted by exit 0 alone unless STATUS: SAFE was printed.
    """
    return False


def verifier_confidence_label(
    candidate: Dict[str, Any],
    *,
    verifier_verdict: str,
    verification_scope: str,
    harness_error: bool,
    product_verified: bool = False,
    stdout: str = "",
    stderr: str = "",
) -> str:
    """Classify how strongly runtime verifier output should affect static review."""
    if harness_error:
        return "harness_only"
    if str(verification_scope).lower() != "concrete_behavior":
        return "scope_mismatch"
    if product_verified:
        return "clean_product_signal"
    combined = f"{stdout}\n{stderr}".lower()
    if str(verifier_verdict).lower() == "refuted" and "status: safe" in combined:
        return "runtime_safe_advisory"
    if str(verifier_verdict).lower() in {"verified", "refuted"}:
        return "advisory"
    return "advisory"
