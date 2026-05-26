"""Verifier sandbox must not hang after wall-clock timeout."""

from __future__ import annotations

import threading
import time

from src.domain.verifier_schemas import VerificationStatus
from src.infrastructure.sandbox import SandboxExecResult
from src.orchestration.nodes.verifier.sandbox_executor import execute_test_script


def test_execute_test_script_timeout_returns_without_waiting_on_worker(tmp_path) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingSandbox:
        image_name = "test"
        execution_workdir = "/repo"

        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def start(self, *_args, **_kwargs) -> None:
            pass

        def write_file_in_container(self, *_args, **_kwargs) -> None:
            pass

        def stop(self) -> None:
            release.set()

        def execute_result(self, cmd, workdir=None):  # noqa: ANN001, ANN201
            started.set()
            release.wait(timeout=30)
            return SandboxExecResult(exit_code=0, stdout="never", stderr="")

    settings = _TimeoutSettings()

    t0 = time.perf_counter()
    rec = execute_test_script(
        repo_path=str(tmp_path),
        candidate_id="blocked",
        attempt_number=1,
        test_code="import time\n",
        settings=settings,
        sandbox_factory=BlockingSandbox,
        graph_state={"metadata": {}},
    )
    elapsed = time.perf_counter() - t0

    assert started.wait(timeout=2), "worker thread should have started"
    assert rec.timeout is True
    assert rec.status == VerificationStatus.FAILED
    assert elapsed < settings.verifier_test_timeout_seconds + 3


class _TimeoutSettings:
    verifier_image = "test-img"
    verifier_test_timeout_seconds = 1
    verifier_ruff_enabled = False
    verifier_flake8_enabled = False
    verifier_lint_output_max_chars = 1000
    verifier_use_execution_workspace = False
    verifier_clone_remote_in_container = False
    verifier_require_repo_in_container = False
