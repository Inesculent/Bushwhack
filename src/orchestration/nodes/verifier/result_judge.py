"""Map sandbox results to verdicts and coarse verification scope."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple

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

_MISSING_MODULE = re.compile(
    r"(?:ModuleNotFoundError:\s*)?No module named ['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

_HARNESS_IMPORT_CRASH = re.compile(
    r"STATUS:\s*CRASHED\s*\|\s*ExceptionType:\s*(ImportError|ModuleNotFoundError)\b",
    re.IGNORECASE,
)

_HARNESS_PIL_MOCK_CRASH = re.compile(
    r"ModuleNotFoundError:\s*No module named ['\"]PIL\.",
    re.IGNORECASE,
)

_VERIFY_SCRIPT_PATH = re.compile(r"/tmp/verify_[^\s:]+\.py", re.IGNORECASE)

_SIGNATURE_MISMATCH = re.compile(
    r"missing \d+ required positional argument",
    re.IGNORECASE,
)

_STUB_TYPING_ATTR = re.compile(
    r"AttributeError:.*(?:SimpleNamespace|IO\.|node_typing)",
    re.IGNORECASE,
)

_STATUS_LINE = re.compile(
    r"STATUS:\s*(SAFE|CRASHED|MISMATCH|HARNESS_ERROR)\b",
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


def _combined_output(record: VerifierAttemptRecord) -> str:
    return f"{record.stdout}\n{record.stderr}"


def _normalize_repo_path(path: str) -> str:
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _traceback_in_target_file(combined: str, target_file_path: str) -> bool:
    norm = _normalize_repo_path(target_file_path)
    if not norm:
        return False
    blob = combined.replace("\\", "/")
    markers = (
        f"/repo/{norm}",
        f"/workspace/{norm}",
        f"/exec_workspace/{norm}",
        norm,
    )
    return any(m in blob for m in markers)


def _traceback_only_in_verifier_script(combined: str) -> bool:
    blob = combined.replace("\\", "/")
    if "/tmp/verify_" in blob and _VERIFY_SCRIPT_PATH.search(blob):
        if not re.search(r"/repo/|/workspace/|/exec_", blob):
            return True
    return False


def _wrong_status_protocol(record: VerifierAttemptRecord) -> bool:
    """True when exit code does not match the STATUS line printed (if any)."""
    if record.timeout:
        return False
    combined = _combined_output(record)
    lower = combined.lower()
    if record.exit_code == 0:
        return "status: safe" not in lower
    if record.exit_code == 1:
        return "status: mismatch" not in lower and "status: crashed" not in lower
    if record.exit_code == 2:
        return "status: harness_error" not in lower
    if record.exit_code is not None and record.exit_code < 0:
        return True
    return _STATUS_LINE.search(combined) is None


def classify_attempt_failure(
    record: VerifierAttemptRecord,
    *,
    target_file_path: str = "",
) -> str:
    """Coarse failure class for retry hints (aligned with judge harness detection)."""
    if record.timeout:
        return "timeout"
    combined = _combined_output(record)
    lower = combined.lower()

    if attempt_was_harness_error(record):
        if record.sandbox_mode == "harness_preflight" or (
            "syntaxerror" in lower and "status: harness_error" in lower
        ):
            return "syntax_error"
        if _MISSING_MODULE.search(combined):
            return "module_not_found"
        if _SIGNATURE_MISMATCH.search(combined):
            return "signature_mismatch"
        if (
            any(m in lower for m in _HARNESS_STDERR_MARKERS)
            or _HARNESS_IMPORT_CRASH.search(combined)
            or _HARNESS_PIL_MOCK_CRASH.search(combined)
            or _STUB_TYPING_ATTR.search(combined)
        ):
            return "import_error"
        if "status: harness_error" in lower or (
            record.exit_code == 2 and "harness_error" in lower
        ):
            return "harness_error"
        if "status: crashed" in lower and crash_is_harness_not_product(
            record, target_file_path=target_file_path
        ):
            return "signature_mismatch" if _SIGNATURE_MISMATCH.search(combined) else "harness_error"
        return "harness_error"

    if "status: mismatch" in lower:
        return "wrong_output"
    if "status: crashed" in lower:
        return "product_crash"
    if "status: safe" in lower:
        return "safe"
    if _wrong_status_protocol(record):
        return "wrong_status_protocol"
    return "unknown"


def missing_modules_from_attempts(attempts: Sequence[VerifierAttemptRecord]) -> List[str]:
    """Extract missing import names from verifier attempts for diagnostics."""
    out: List[str] = []
    seen: set[str] = set()
    for record in attempts:
        for match in _MISSING_MODULE.findall(_combined_output(record)):
            name = match.strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def crash_is_harness_not_product(
    record: VerifierAttemptRecord,
    *,
    target_file_path: str = "",
) -> bool:
    """True when STATUS: CRASHED (or traceback) reflects test setup, not product under test."""
    combined = _combined_output(record)
    lower = combined.lower()

    if "status: harness_error" in lower:
        return True
    if _HARNESS_IMPORT_CRASH.search(combined):
        return True
    if _HARNESS_PIL_MOCK_CRASH.search(combined):
        return True
    if _SIGNATURE_MISMATCH.search(combined):
        return True
    if _STUB_TYPING_ATTR.search(combined):
        return True
    if _traceback_only_in_verifier_script(combined):
        return True

    # Import failures during module load (before product invoke)
    if record.exit_code == 1 and "status: crashed" in lower:
        if _HARNESS_IMPORT_CRASH.search(combined):
            return True
        if "importerror" in lower or "modulenotfounderror" in lower:
            if not _traceback_in_target_file(combined, target_file_path):
                return True
        if "attributeerror" in lower and "io." in lower:
            return True
        if "typeerror" in lower and _SIGNATURE_MISMATCH.search(combined):
            return True
        if target_file_path and not _traceback_in_target_file(combined, target_file_path):
            return True

    return False


def attempt_indicates_product_proof(
    record: VerifierAttemptRecord,
    *,
    target_file_path: str = "",
) -> bool:
    """True when this attempt supports a concrete_behavior verified verdict."""
    combined = _combined_output(record)
    if "STATUS: MISMATCH" in combined:
        return True
    if record.exit_code == 1 and "status: crashed" in combined.lower():
        if crash_is_harness_not_product(record, target_file_path=target_file_path):
            return False
        if target_file_path:
            return _traceback_in_target_file(combined, target_file_path)
        return not _traceback_only_in_verifier_script(combined)
    return False


def attempt_was_harness_error(record: VerifierAttemptRecord) -> bool:
    """True when failure is setup/import/syntax, not product behavior under test."""
    if record.sandbox_mode in ("snippet_workspace", "harness_preflight"):
        return True
    combined = _combined_output(record)
    lower = combined.lower()
    if "status: harness_error" in lower:
        return True
    if _HARNESS_IMPORT_CRASH.search(combined):
        return True
    if _HARNESS_PIL_MOCK_CRASH.search(combined):
        return True
    if record.exit_code == 2 and "status: harness_error" in lower:
        return True
    if _STUB_TYPING_ATTR.search(combined) or _SIGNATURE_MISMATCH.search(combined):
        return True
    if record.exit_code == 1 and "status: crashed" in lower:
        return crash_is_harness_not_product(record)
    if (
        record.exit_code == 1
        and "status: crashed" not in lower
        and record.stderr
        and any(m in record.stderr.lower() for m in _HARNESS_STDERR_MARKERS)
    ):
        return True
    return False


def judge_attempt(
    record: VerifierAttemptRecord,
    *,
    target_file_path: str = "",
) -> Tuple[VerifierVerdict, str]:
    """Return (verdict, rationale) for one execution."""
    if record.timeout:
        return "inconclusive", "Execution timed out before completion."

    if attempt_was_harness_error(record):
        return (
            "inconclusive",
            "Harness or environment error (import, syntax, or missing repository); not product behavior.",
        )

    combined = _combined_output(record)

    if record.exit_code == 0:
        if "status: safe" in combined.lower():
            return "refuted", "Verifier script reported STATUS: SAFE (bug not reproduced)."
        return "refuted", "Process exited 0 (no crash observed in verifier script)."

    if record.exit_code == 1 and "STATUS: MISMATCH" in combined:
        return "verified", "Verifier script reported STATUS: MISMATCH (wrong output reproduced)."

    if record.exit_code == 1 and "STATUS: CRASHED" in combined:
        if crash_is_harness_not_product(record, target_file_path=target_file_path):
            return (
                "inconclusive",
                "STATUS: CRASHED reflects harness/setup (import, signature, or verifier script); retry with corrected imports and execute() arity.",
            )
        if attempt_indicates_product_proof(record, target_file_path=target_file_path):
            return (
                "verified",
                "Verifier script reported STATUS: CRASHED with traceback in target file (product behavior).",
            )
        return (
            "inconclusive",
            "STATUS: CRASHED without traceback in cited target file; cannot confirm product defect.",
        )

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


def _summarize_attempt(
    record: VerifierAttemptRecord,
    *,
    target_file_path: str = "",
) -> str:
    combined = _combined_output(record)
    status_m = _STATUS_LINE.search(combined)
    status = status_m.group(1).upper() if status_m else "none"
    err_class = classify_attempt_failure(record, target_file_path=target_file_path)
    return (
        f"attempt={record.attempt_number} exit={record.exit_code} "
        f"status={status} class={err_class}"
    )


def verifier_hint_flags_for_attempts(
    *,
    verdict: VerifierVerdict,
    attempts: Sequence[VerifierAttemptRecord],
    target_file_path: str = "",
) -> Dict[str, bool]:
    """Shared harness/product flags for verifier_finalize and legacy runner."""
    harness_error = any(attempt_was_harness_error(a) for a in attempts)
    last_attempt = attempts[-1] if attempts else None
    product_verified = (
        verdict == "verified"
        and not harness_error
        and last_attempt is not None
        and attempt_indicates_product_proof(last_attempt, target_file_path=target_file_path)
    )
    return {
        "harness_error": harness_error,
        "product_verified": product_verified,
    }


def build_retry_feedback(
    record: VerifierAttemptRecord,
    *,
    prior_attempts: Sequence[VerifierAttemptRecord] | None = None,
    target_file_path: str = "",
) -> str:
    from src.orchestration.prompts.renderer import load_reviewer_prompt

    template = load_reviewer_prompt("verifier/retry_instruct.md")

    def _tail(s: str, n: int = 2500) -> str:
        s = s.strip()
        if len(s) <= n:
            return s or "(empty)"
        return s[-n:]

    error_class = classify_attempt_failure(record, target_file_path=target_file_path)
    prior = list(prior_attempts or [])
    prior_summary = (
        "\n".join(
            _summarize_attempt(p, target_file_path=target_file_path) for p in prior[-4:]
        )
        if prior
        else "(first attempt)"
    )

    repeat_hint = ""
    if prior:
        prior_classes = [
            classify_attempt_failure(p, target_file_path=target_file_path) for p in prior
        ]
        if prior_classes and prior_classes[-1] == error_class:
            repeat_hint = (
                f"Do not repeat the same failure ({error_class}). "
                "Read INPUT_TYPES / inspect.signature on the target before calling execute."
            )

    action_hint = ""
    if error_class == "signature_mismatch":
        action_hint = (
            "Fix execute() call arity and keyword names using inspect.signature or INPUT_TYPES "
            f"from the module under test{f' ({target_file_path})' if target_file_path else ''}."
        )
    elif error_class == "import_error":
        action_hint = (
            "Stub only modules/attributes named in the traceback (minimal sys.modules), "
            "or import a narrower symbol. Use STATUS: HARNESS_ERROR for setup failures."
        )
    elif error_class == "syntax_error":
        action_hint = "Fix Python syntax in the verifier script before running product code."
    elif error_class == "harness_error":
        action_hint = (
            "Use STATUS: HARNESS_ERROR (exit 2) for import/setup/syntax before product invoke; "
            "fix harness imports and read the cited target file from focused context."
        )
    elif error_class == "timeout":
        action_hint = (
            "Shorten the repro (smaller inputs, fewer iterations) and avoid heavy imports; "
            "still print exactly one STATUS line before exit."
        )
    elif error_class == "wrong_status_protocol":
        action_hint = (
            "Match exit code to STATUS: SAFE→0, MISMATCH/CRASHED→1, HARNESS_ERROR→2. "
            "Print exactly one STATUS line before sys.exit."
        )
    elif error_class == "product_crash":
        action_hint = (
            "CRASHED was not tied to the cited target file; ensure traceback references "
            f"{target_file_path or 'the defect path'} after product code is invoked."
        )

    return template.format(
        exit_code=record.exit_code,
        error_class=error_class,
        prior_attempts_summary=prior_summary,
        repeat_hint=repeat_hint or "(none)",
        action_hint=action_hint or "(see stderr/stdout)",
        target_file_path=target_file_path or "(unknown)",
        stdout_tail=_tail(record.stdout),
        stderr_tail=_tail(record.stderr),
    )
