"""Classify candidate failure modes for scoped verifier refutation."""

from __future__ import annotations

from typing import Any, Dict

_CRASH_MARKERS = (
    "crash",
    "exception",
    "raise",
    "indexerror",
    "typeerror",
    "attributeerror",
    "keyerror",
    "valueerror",
    "zerodivision",
    "traceback",
    "segfault",
)

_WRONG_OUTPUT_MARKERS = (
    "wrong",
    "incorrect result",
    "data loss",
    "loses",
    "lost group",
    "silent",
    "empty string",
    "mismatch",
    "returns none",
    "implicit none",
    "missing return",
    "wrong value",
    "wrong match",
    "capturing group",
    "full match",
)


def failure_mode_class(candidate: Dict[str, Any]) -> str:
    """Return 'crash', 'wrong_output', or 'unknown' from failure_mode + content."""
    blob = (
        f"{candidate.get('failure_mode', '')} {candidate.get('content', '')} "
        f"{candidate.get('evidence_summary', '')}"
    ).lower()
    has_crash = any(m in blob for m in _CRASH_MARKERS)
    has_wrong = any(m in blob for m in _WRONG_OUTPUT_MARKERS)
    if has_crash and not has_wrong:
        return "crash"
    if has_wrong:
        return "wrong_output"
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
    if harness_error or str(verifier_verdict).lower() != "refuted":
        return False
    if str(verification_scope).lower() != "concrete_behavior":
        return False
    combined = f"{stdout}\n{stderr}".lower()
    if "status: safe" in combined:
        return True
    fclass = failure_mode_class(candidate)
    if fclass == "wrong_output":
        return False
    if fclass == "crash":
        return True
    return True


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
    if (
        str(verifier_verdict).lower() == "refuted"
        and "status: safe" in combined
        and verifier_refutation_applies(
            candidate,
            verifier_verdict=verifier_verdict,
            verification_scope=verification_scope,
            harness_error=harness_error,
            stdout=stdout,
            stderr=stderr,
        )
    ):
        return "clean_product_signal"
    if str(verifier_verdict).lower() == "refuted" and not verifier_refutation_applies(
        candidate,
        verifier_verdict=verifier_verdict,
        verification_scope=verification_scope,
        harness_error=harness_error,
        stdout=stdout,
        stderr=stderr,
    ):
        return "static_claim_not_runtime_refutable"
    if str(verifier_verdict).lower() in {"verified", "refuted"}:
        return "advisory"
    return "advisory"
