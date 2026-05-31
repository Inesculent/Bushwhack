"""Verifier sandbox captures optional Ruff/Flake8 output."""

from __future__ import annotations

from unittest.mock import patch

from src.domain.verifier_schemas import VerificationStatus
from src.infrastructure.sandbox import SandboxExecResult
from src.orchestration.nodes.verifier.sandbox_executor import execute_test_script


def test_execute_test_script_attaches_ruff_lint_run(tmp_path) -> None:
    class FakeSandbox:
        image_name = "test"
        execution_workdir = "/repo"

        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def start(self, *_args, **_kwargs) -> None:
            pass

        def write_file_in_container(self, *_args, **_kwargs) -> None:
            pass

        def stop(self) -> None:
            pass

        def execute_result(self, cmd, workdir=None):  # noqa: ANN001, ANN201
            joined = " ".join(cmd)
            if "ruff" in joined:
                return SandboxExecResult(exit_code=1, stdout="R503 missing return\n", stderr="")
            if cmd and cmd[0] == "python" and len(cmd) > 1:
                return SandboxExecResult(exit_code=0, stdout="ok", stderr="")
            return SandboxExecResult(exit_code=-1, stdout="", stderr="unexpected")

    settings = MagicMockSettings()

    with patch(
        "src.orchestration.nodes.verifier.sandbox_executor.get_settings",
        return_value=settings,
    ):
        rec = execute_test_script(
            repo_path=str(tmp_path),
            candidate_id="c1",
            attempt_number=1,
            test_code="print('hi')\n",
            settings=settings,
            sandbox_factory=FakeSandbox,
            graph_state={"metadata": {}},
        )

    assert rec.status == VerificationStatus.COMPLETED
    assert len(rec.lint_runs) >= 1
    assert rec.lint_runs[0].tool == "ruff"
    assert "R503" in rec.lint_runs[0].stdout


class MagicMockSettings:
    sandbox_backend = "docker"
    verifier_image = "test-img"
    verifier_test_timeout_seconds = 60
    verifier_ruff_enabled = True
    verifier_flake8_enabled = False
    verifier_lint_output_max_chars = 10000
