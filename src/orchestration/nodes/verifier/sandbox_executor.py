"""Run generated verifier scripts inside RepoSandbox with timeout."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import Settings, get_settings
from src.domain.verifier_schemas import VerificationStatus, VerifierAttemptRecord, VerifierLintRun
from src.infrastructure.sandbox import RepoSandbox

logger = logging.getLogger(__name__)


def _truncate_stream(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def _collect_lint_runs(sandbox: RepoSandbox, settings: Settings) -> List[VerifierLintRun]:
    """Run Ruff/Flake8 inside the mounted repo (best-effort; missing tools are recorded)."""
    runs: List[VerifierLintRun] = []
    cap = settings.verifier_lint_output_max_chars
    if settings.verifier_ruff_enabled:
        r = sandbox.execute_result(
            [
                "python",
                "-m",
                "ruff",
                "check",
                ".",
                "--no-cache",
                "--output-format",
                "concise",
            ],
            workdir="/repo",
        )
        runs.append(
            VerifierLintRun(
                tool="ruff",
                command="python -m ruff check . --no-cache --output-format concise",
                exit_code=r.exit_code,
                stdout=_truncate_stream(r.stdout, cap),
                stderr=_truncate_stream(r.stderr, cap),
            )
        )
    if settings.verifier_flake8_enabled:
        r = sandbox.execute_result(
            ["python", "-m", "flake8", ".", "--count"],
            workdir="/repo",
        )
        runs.append(
            VerifierLintRun(
                tool="flake8",
                command="python -m flake8 . --count",
                exit_code=r.exit_code,
                stdout=_truncate_stream(r.stdout, cap),
                stderr=_truncate_stream(r.stderr, cap),
            )
        )
    return runs


def _safe_candidate_path_fragment(candidate_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", candidate_id).strip("_")
    return (cleaned or "candidate")[:80]


def _start_verifier_sandbox(
    sandbox: RepoSandbox,
    repo_path: str,
    graph_state: Optional[Dict[str, Any]],
) -> None:
    """Mount a local clone or clone from GitHub when ``repo_path`` is a URL (e.g. snapshot resume)."""
    raw = (repo_path or "").strip()
    local = Path(raw)
    if raw and local.is_dir():
        sandbox.start(str(local.resolve()))
        return

    meta: Dict[str, Any] = {}
    if isinstance(graph_state, dict):
        raw_meta = graph_state.get("metadata")
        if isinstance(raw_meta, dict):
            meta = raw_meta

    repo_url = str(meta.get("review_repo_url") or "").strip()
    checkout_ref = str(meta.get("review_checkout_ref") or "").strip()
    pr_number = meta.get("pr_number")
    if pr_number is None:
        pr_number = meta.get("review_pr_number")

    if not repo_url and raw.startswith(("http://", "https://")):
        repo_url = raw

    if not checkout_ref and pr_number is not None and str(pr_number).strip():
        checkout_ref = f"pull/{pr_number}/head"

    if not repo_url:
        raise FileNotFoundError(
            "Verifier sandbox needs either a local repo directory in repo_path, "
            "or metadata.review_repo_url (and checkout ref / PR number) to clone inside the container. "
            f"Got repo_path={raw!r}."
        )

    if not checkout_ref:
        checkout_ref = "HEAD"

    sandbox.start_from_remote_ref(repo_url=repo_url, ref=checkout_ref)


def execute_test_script(
    *,
    repo_path: str,
    candidate_id: str,
    attempt_number: int,
    test_code: str,
    settings: Settings | None = None,
    sandbox_factory: type[RepoSandbox] | None = None,
    graph_state: Optional[Dict[str, Any]] = None,
) -> VerifierAttemptRecord:
    """Write script to /tmp and run ``python`` with timeout; stop sandbox after."""
    settings = settings or get_settings()
    sb_cls = sandbox_factory or RepoSandbox
    sandbox = sb_cls(image_name=settings.verifier_image)
    frag = _safe_candidate_path_fragment(candidate_id)
    remote_path = f"/tmp/verify_{frag}_{attempt_number}.py"
    record = VerifierAttemptRecord(
        attempt_number=attempt_number,
        test_code=test_code,
        status=VerificationStatus.EXECUTING,
    )
    started = time.perf_counter()
    try:
        _start_verifier_sandbox(sandbox, repo_path, graph_state)
        record.lint_runs = _collect_lint_runs(sandbox, settings)
        sandbox.write_file_in_container(remote_path, test_code.encode("utf-8"))
        cmd = ["python", remote_path]

        def _run() -> tuple[int, str, str]:
            r = sandbox.execute_result(cmd, workdir="/repo")
            return r.exit_code, r.stdout, r.stderr

        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_run)
            try:
                exit_code, stdout, stderr = fut.result(timeout=settings.verifier_test_timeout_seconds)
            except FuturesTimeout:
                record.timeout = True
                record.exit_code = -1
                record.stdout = ""
                record.stderr = "Verifier execution timed out."
                record.status = VerificationStatus.FAILED
                logger.warning(
                    "verifier timeout candidate_id=%s attempt=%s",
                    candidate_id,
                    attempt_number,
                )
                return record

        record.exit_code = exit_code
        record.stdout = stdout
        record.stderr = stderr
        record.status = VerificationStatus.COMPLETED
    except Exception as exc:  # noqa: BLE001
        record.exit_code = -1
        record.stderr = f"{exc.__class__.__name__}: {exc}"
        record.status = VerificationStatus.FAILED
        logger.warning("verifier sandbox error: %s", exc)
    finally:
        record.execution_time_seconds = time.perf_counter() - started
        try:
            sandbox.stop()
        except Exception:  # noqa: BLE001
            pass

    return record
