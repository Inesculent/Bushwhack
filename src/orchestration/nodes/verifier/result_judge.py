"""Map sandbox results to verdicts and coarse verification scope."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from src.domain.verifier_schemas import (
    VerifierAttemptRecord,
    VerifierVerdict,
    VerificationScope,
)


def infer_verification_scope(candidate: Dict[str, Any]) -> VerificationScope:
    fm = str(candidate.get("failure_mode") or "").strip()
    content = str(candidate.get("content") or "").lower()
    blob = fm.lower() + " " + content
    if len(fm) < 12:
        return "abstract_or_unverifiable"
    abstract_kw = (
        "network",
        "http",
        "https",
        "distributed",
        "race",
        "deadlock",
        "gui",
        "render",
        "multiprocess",
        "kubernetes",
        "async",
        "websocket",
    )
    if any(k in blob for k in abstract_kw):
        return "abstract_or_unverifiable"
    return "concrete_behavior"


def judge_attempt(record: VerifierAttemptRecord) -> Tuple[VerifierVerdict, str]:
    """Return (verdict, rationale) for one execution."""
    if record.timeout:
        return "inconclusive", "Execution timed out before completion."

    combined = f"{record.stdout}\n{record.stderr}"

    if record.exit_code == 0:
        return "refuted", "Process exited 0 (no crash observed in verifier script)."

    if record.exit_code == 1 and "STATUS: CRASHED" in combined:
        return "verified", "Verifier script reported STATUS: CRASHED (exception path hit)."

    if record.exit_code is not None and record.exit_code < 0:
        return "inconclusive", "Sandbox or harness error; no protocol result."

    return (
        "inconclusive",
        f"Exit code {record.exit_code} without clear STATUS protocol line.",
    )


def build_retry_feedback(record: VerifierAttemptRecord) -> str:
    from src.orchestration.prompts.renderer import load_reviewer_prompt

    template = load_reviewer_prompt("verifier/retry_instruct.md")
    def _tail(s: str, n: int = 2500) -> str:
        s = s.strip()
        if len(s) <= n:
            return s or "(empty)"
        return s[-n:]

    return template.format(
        exit_code=record.exit_code,
        stdout_tail=_tail(record.stdout),
        stderr_tail=_tail(record.stderr),
    )
