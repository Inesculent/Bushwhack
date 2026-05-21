"""Map sandbox results to verdicts and coarse verification scope."""

from __future__ import annotations

import re
from typing import Any, Dict, Tuple

from src.domain.verifier_schemas import (
    VerifierAttemptRecord,
    VerifierVerdict,
    VerificationScope,
)

_HARNESS_STDERR_MARKERS = (
    "modulenotfounderror",
    "importerror",
    "syntaxerror",
    "indentationerror",
    "no module named",
)

_HARNESS_IMPORT_CRASH = re.compile(
    r"STATUS:\s*CRASHED\s*\|\s*ExceptionType:\s*(ImportError|ModuleNotFoundError)\b",
    re.IGNORECASE,
)

# Mocking PIL as MagicMock breaks `PIL.PngImagePlugin`; treat as harness noise.
_HARNESS_PIL_MOCK_CRASH = re.compile(
    r"ModuleNotFoundError:\s*No module named ['\"]PIL\.",
    re.IGNORECASE,
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


def attempt_was_harness_error(record: VerifierAttemptRecord) -> bool:
    """True when failure is setup/import/syntax, not product behavior under test."""
    if record.sandbox_mode in ("snippet_workspace", "harness_preflight"):
        return True
    combined = f"{record.stdout}\n{record.stderr}".lower()
    if "status: harness_error" in combined:
        return True
    if _HARNESS_IMPORT_CRASH.search(f"{record.stdout}\n{record.stderr}"):
        return True
    if _HARNESS_PIL_MOCK_CRASH.search(f"{record.stdout}\n{record.stderr}"):
        return True
    if record.exit_code == 2 and "status: harness_error" in record.stdout.lower():
        return True
    return False


def judge_attempt(record: VerifierAttemptRecord) -> Tuple[VerifierVerdict, str]:
    """Return (verdict, rationale) for one execution."""
    if record.timeout:
        return "inconclusive", "Execution timed out before completion."

    if attempt_was_harness_error(record):
        return (
            "inconclusive",
            "Harness or environment error (import, syntax, or missing repository); not product behavior.",
        )

    combined = f"{record.stdout}\n{record.stderr}"

    if record.exit_code == 0:
        if "status: safe" in combined.lower():
            return "refuted", "Verifier script reported STATUS: SAFE (bug not reproduced)."
        return "refuted", "Process exited 0 (no crash observed in verifier script)."

    if record.exit_code == 1 and "STATUS: CRASHED" in combined:
        return "verified", "Verifier script reported STATUS: CRASHED (exception path hit)."

    if record.exit_code == 2 and "STATUS: HARNESS_ERROR" in combined:
        return "inconclusive", "Verifier script reported STATUS: HARNESS_ERROR."

    if (
        record.exit_code == 1
        and "status: crashed" not in combined.lower()
        and record.stderr
        and any(m in record.stderr.lower() for m in _HARNESS_STDERR_MARKERS)
    ):
        return "inconclusive", "Process failed during import/setup before STATUS protocol."

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
