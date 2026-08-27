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


def test_execute_test_script_uses_prepared_verifier_python(tmp_path) -> None:
    commands: list[list[str]] = []

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
            commands.append(list(cmd))
            joined = " ".join(cmd)
            if cmd[:2] == ["sh", "-lc"]:
                return SandboxExecResult(exit_code=0, stdout="", stderr="")
            if cmd[:3] == ["python", "-m", "venv"]:
                return SandboxExecResult(exit_code=0, stdout="", stderr="")
            if cmd and cmd[0].endswith("/bin/python") and "-c" in cmd:
                return SandboxExecResult(exit_code=0, stdout=cmd[0], stderr="")
            if cmd and cmd[0].endswith("/bin/python"):
                return SandboxExecResult(exit_code=0, stdout="STATUS: SAFE\n", stderr="")
            return SandboxExecResult(exit_code=-1, stdout="", stderr=joined)

    settings = MagicMockSettings()
    settings.verifier_ruff_enabled = False

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
    assert rec.env_metadata["status"] == "usable"
    script_commands = [cmd for cmd in commands if cmd and cmd[-1].startswith("/tmp/verify_")]
    assert script_commands
    assert script_commands[0][0] == rec.env_metadata["python_path"]


def test_execute_test_script_records_target_import_probe_without_broad_install(tmp_path) -> None:
    commands: list[list[str]] = []

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
            commands.append(list(cmd))
            if cmd[:2] == ["sh", "-lc"]:
                return SandboxExecResult(exit_code=0, stdout="requirements.txt abc123\n", stderr="")
            if cmd[:3] == ["python", "-m", "venv"]:
                return SandboxExecResult(exit_code=0, stdout="", stderr="")
            if cmd and cmd[0].endswith("/bin/python") and "-c" in cmd:
                code = cmd[-1]
                if "importlib.import_module('pkg.mod')" in code:
                    return SandboxExecResult(
                        exit_code=1,
                        stdout="",
                        stderr="ModuleNotFoundError: No module named 'torch'",
                    )
                return SandboxExecResult(exit_code=0, stdout=cmd[0], stderr="")
            if cmd and cmd[0].endswith("/bin/python"):
                return SandboxExecResult(exit_code=0, stdout="STATUS: SAFE\n", stderr="")
            return SandboxExecResult(exit_code=-1, stdout="", stderr="unexpected")

    settings = MagicMockSettings()
    settings.verifier_ruff_enabled = False
    settings.verifier_prepare_env_install_deps = True

    rec = execute_test_script(
        repo_path=str(tmp_path),
        candidate_id="c1",
        attempt_number=1,
        test_code="print('hi')\n",
        settings=settings,
        sandbox_factory=FakeSandbox,
        graph_state={"metadata": {}, "verifier_candidate": {"file_path": "pkg/mod.py"}},
    )

    assert rec.status == VerificationStatus.COMPLETED
    assert rec.env_metadata["dependency_install_policy"] == "targeted_only"
    assert rec.env_metadata["missing_modules"] == ["torch"]
    assert rec.env_metadata["target_import_probes"][0]["module"] == "pkg.mod"
    assert not any(cmd[:4] == [rec.env_metadata["python_path"], "-m", "pip", "install"] for cmd in commands)


def test_execute_test_script_installs_typing_extensions_compat_shim(tmp_path) -> None:
    commands: list[list[str]] = []
    written: dict[str, bytes] = {}

    class FakeSandbox:
        image_name = "test"
        execution_workdir = "/repo"

        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def start(self, *_args, **_kwargs) -> None:
            pass

        def write_file_in_container(self, dest_path, content) -> None:  # noqa: ANN001
            written[str(dest_path)] = bytes(content)

        def stop(self) -> None:
            pass

        def execute_result(self, cmd, workdir=None):  # noqa: ANN001, ANN201
            commands.append(list(cmd))
            if cmd[:2] == ["sh", "-lc"]:
                return SandboxExecResult(exit_code=0, stdout="requirements.txt abc123\n", stderr="")
            if cmd[:3] == ["python", "-m", "venv"]:
                return SandboxExecResult(exit_code=0, stdout="", stderr="")
            if cmd and cmd[0].endswith("/bin/python") and "-c" in cmd:
                code = cmd[-1]
                if "getsitepackages" in code:
                    return SandboxExecResult(exit_code=0, stdout="/repo/.venv/lib/python/site-packages\n", stderr="")
                if "importlib.import_module('typing_extensions')" in code and not written:
                    return SandboxExecResult(
                        exit_code=1,
                        stdout="",
                        stderr="ModuleNotFoundError: No module named 'typing_extensions'",
                    )
                return SandboxExecResult(exit_code=0, stdout=cmd[0], stderr="")
            if cmd and cmd[0].endswith("/bin/python"):
                return SandboxExecResult(exit_code=0, stdout="STATUS: SAFE\n", stderr="")
            return SandboxExecResult(exit_code=-1, stdout="", stderr="unexpected")

    settings = MagicMockSettings()
    settings.verifier_ruff_enabled = False

    rec = execute_test_script(
        repo_path=str(tmp_path),
        candidate_id="c1",
        attempt_number=1,
        test_code="print('STATUS: SAFE')\n",
        settings=settings,
        sandbox_factory=FakeSandbox,
        graph_state={"metadata": {}, "verifier_candidate": {"file_path": "pkg/mod.py"}},
    )

    assert rec.status == VerificationStatus.COMPLETED
    assert rec.env_metadata["install_attempts"] == [
        {"target": "typing_extensions", "action": "compat_shim", "exit_code": 0}
    ]
    assert "/repo/.venv/lib/python/site-packages/typing_extensions.py" in written
    assert not any(cmd[:4] == [rec.env_metadata["python_path"], "-m", "pip", "install"] for cmd in commands)


class MagicMockSettings:
    sandbox_backend = "docker"
    verifier_image = "test-img"
    verifier_test_timeout_seconds = 60
    verifier_ruff_enabled = True
    verifier_flake8_enabled = False
    verifier_lint_output_max_chars = 10000
