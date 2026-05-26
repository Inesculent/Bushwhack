"""Run generated verifier scripts inside RepoSandbox with timeout.

Verifier uses ``Settings.verifier_image`` for host-mounted checkouts and
``Settings.verifier_clone_image`` (default ``agent-fs-sandbox``, includes git) when cloning a
remote PR inside Docker. After clone at ``/repo``, it may copy to a writable ``/exec_*`` workspace
via ``create_execution_workspace()``, then runs generated scripts there. Rebuild
``verifier-test-env:latest`` from ``Dockerfile.verifier`` if you want git in the slim image too.
"""

from __future__ import annotations

import ast
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.config import Settings, get_settings
from src.domain.verifier_schemas import VerificationStatus, VerifierAttemptRecord, VerifierLintRun
from src.infrastructure.sandbox import RepoSandbox

logger = logging.getLogger(__name__)


def _truncate_stream(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def _collect_lint_runs(sandbox: RepoSandbox, settings: Settings) -> List[VerifierLintRun]:
    """Run Ruff/Flake8 inside the cloned repo at /repo (best-effort; missing tools are recorded)."""
    runs: List[VerifierLintRun] = []
    cap = settings.verifier_lint_output_max_chars
    if settings.verifier_ruff_enabled:
        r = sandbox.execute_result(
            [
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
                command="ruff check . --no-cache --output-format concise",
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


def _graph_metadata(graph_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(graph_state, dict):
        return {}
    raw_meta = graph_state.get("metadata")
    return raw_meta if isinstance(raw_meta, dict) else {}


def _resolve_remote_clone_target(
    repo_path: str,
    graph_state: Optional[Dict[str, Any]],
) -> Tuple[str, str]:
    """Return (repo_url, checkout_ref) for in-container clone."""
    raw = (repo_path or "").strip()
    meta = _graph_metadata(graph_state)

    repo_url = str(meta.get("review_repo_url") or "").strip()
    checkout_ref = str(meta.get("review_checkout_ref") or "").strip()
    pr_number = meta.get("pr_number")
    if pr_number is None:
        pr_number = meta.get("review_pr_number")

    if not repo_url and raw.startswith(("http://", "https://")):
        repo_url = raw

    if not checkout_ref and pr_number is not None and str(pr_number).strip():
        checkout_ref = f"pull/{pr_number}/head"

    if not checkout_ref:
        checkout_ref = "HEAD"

    return repo_url, checkout_ref


def _needs_remote_clone(
    repo_path: str,
    graph_state: Optional[Dict[str, Any]],
    settings: Settings,
) -> bool:
    raw = (repo_path or "").strip()
    if raw and Path(raw).is_dir():
        return False
    repo_url, _ = _resolve_remote_clone_target(raw, graph_state)
    return bool(settings.verifier_clone_remote_in_container and repo_url)


def _verifier_sandbox_image(
    settings: Settings,
    repo_path: str,
    graph_state: Optional[Dict[str, Any]],
) -> str:
    """Pick image: clone-capable stack for remote PR checkout, else verifier test image."""
    if _needs_remote_clone(repo_path, graph_state, settings):
        clone_img = (settings.verifier_clone_image or "").strip()
        if clone_img:
            return clone_img
    return settings.verifier_image


def _start_verifier_sandbox(
    sandbox: RepoSandbox,
    repo_path: str,
    graph_state: Optional[Dict[str, Any]],
    *,
    settings: Settings | None = None,
) -> str:
    """Mount or clone repository; optionally prepare writable exec workspace.

    Returns sandbox_mode: ``repo_mount`` | ``exec_workspace`` | ``snippet_workspace``.
    """
    settings = settings or get_settings()
    raw = (repo_path or "").strip()
    local = Path(raw)
    if raw and local.is_dir():
        sandbox.start(str(local.resolve()))
        if getattr(settings, "verifier_use_execution_workspace", False):
            sandbox.create_execution_workspace(workspace_name="verify_exec")
            return "exec_workspace"
        return "repo_mount"

    repo_url, checkout_ref = _resolve_remote_clone_target(raw, graph_state)
    should_clone = settings.verifier_clone_remote_in_container and bool(repo_url)

    if not should_clone:
        if settings.verifier_require_repo_in_container:
            raise FileNotFoundError(
                "Verifier requires a repository in-container but no review_repo_url / https "
                f"repo_path is available. repo_path={raw!r}."
            )
        sandbox.start_snippet_workspace()
        return "snippet_workspace"

    sandbox.start_from_remote_ref(repo_url=repo_url, ref=checkout_ref)
    if getattr(settings, "verifier_use_execution_workspace", False):
        sandbox.create_execution_workspace(workspace_name="verify_exec")
        return "exec_workspace"
    return "repo_mount"


def validate_test_code(test_code: str) -> str | None:
    """Return an error message if ``test_code`` is not valid Python."""
    try:
        ast.parse(test_code)
    except SyntaxError as exc:
        return f"SyntaxError: {exc}"
    return None


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
    image_name = _verifier_sandbox_image(settings, repo_path, graph_state)
    sandbox = sb_cls(image_name=image_name)
    frag = _safe_candidate_path_fragment(candidate_id)
    remote_path = f"/tmp/verify_{frag}_{attempt_number}.py"
    record = VerifierAttemptRecord(
        attempt_number=attempt_number,
        test_code=test_code,
        status=VerificationStatus.EXECUTING,
    )
    started = time.perf_counter()
    try:
        syntax_err = validate_test_code(test_code)
        if syntax_err:
            record.exit_code = 2
            record.stdout = f"STATUS: HARNESS_ERROR | {syntax_err}"
            record.stderr = syntax_err
            record.status = VerificationStatus.COMPLETED
            record.sandbox_mode = "harness_preflight"
            return record

        sandbox_mode = _start_verifier_sandbox(
            sandbox, repo_path, graph_state, settings=settings
        )
        exec_wd = sandbox.execution_workdir
        record.repo_root = exec_wd
        record.sandbox_mode = sandbox_mode
        record.lint_runs = _collect_lint_runs(sandbox, settings)
        sandbox.write_file_in_container(remote_path, test_code.encode("utf-8"))
        cmd = ["python", remote_path]

        def _run() -> tuple[int, str, str]:
            r = sandbox.execute_result(cmd, workdir=exec_wd)
            return r.exit_code, r.stdout, r.stderr

        # Do not use ``with ThreadPoolExecutor`` on the timeout path: its __exit__ calls
        # shutdown(wait=True) before we stop the container, so a stuck docker exec blocks forever.
        pool = ThreadPoolExecutor(max_workers=1)
        timed_out = False
        try:
            fut = pool.submit(_run)
            try:
                exit_code, stdout, stderr = fut.result(
                    timeout=settings.verifier_test_timeout_seconds
                )
            except FuturesTimeout:
                timed_out = True
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
            else:
                record.exit_code = exit_code
                record.stdout = stdout
                record.stderr = stderr
                record.status = VerificationStatus.COMPLETED
        finally:
            if timed_out:
                try:
                    sandbox.stop()
                except Exception:  # noqa: BLE001
                    pass
                pool.shutdown(wait=False, cancel_futures=True)
            else:
                pool.shutdown(wait=True)
    except Exception as exc:  # noqa: BLE001
        record.exit_code = 2
        record.stdout = f"STATUS: HARNESS_ERROR | {exc.__class__.__name__}: {exc}"
        record.stderr = f"{exc.__class__.__name__}: {exc}"
        record.status = VerificationStatus.COMPLETED
        record.sandbox_mode = record.sandbox_mode or "harness_preflight"
        logger.warning("verifier sandbox error: %s", exc)
    finally:
        record.execution_time_seconds = time.perf_counter() - started
        try:
            sandbox.stop()
        except Exception:  # noqa: BLE001
            pass

    return record
