"""Run generated verifier scripts inside RepoSandbox with timeout."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import TYPE_CHECKING

from src.config import Settings, get_settings
from src.domain.verifier_schemas import VerificationStatus, VerifierAttemptRecord
from src.infrastructure.sandbox import RepoSandbox

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _safe_candidate_path_fragment(candidate_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", candidate_id).strip("_")
    return (cleaned or "candidate")[:80]


def execute_test_script(
    *,
    repo_path: str,
    candidate_id: str,
    attempt_number: int,
    test_code: str,
    settings: Settings | None = None,
    sandbox_factory: type[RepoSandbox] | None = None,
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
        sandbox.start(repo_path)
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
