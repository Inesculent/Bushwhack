"""Sandbox factory and backend selection tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.config import Settings
from src.infrastructure.sandbox import (
    DockerRepoSandbox,
    build_repo_sandbox,
    resolve_verifier_sandbox_image,
)
from src.infrastructure.sandbox_apptainer import ApptainerRepoSandbox


def test_build_repo_sandbox_returns_docker_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.infrastructure.sandbox_docker.docker.from_env",
        MagicMock(return_value=MagicMock()),
    )
    settings = Settings(sandbox_backend="docker")
    sandbox = build_repo_sandbox(settings)
    assert isinstance(sandbox, DockerRepoSandbox)


def test_build_repo_sandbox_returns_apptainer_when_configured(tmp_path) -> None:
    sif = tmp_path / "test.sif"
    sif.write_bytes(b"fake")
    settings = Settings(
        sandbox_backend="apptainer",
        apptainer_image=str(sif),
    )
    sandbox = build_repo_sandbox(settings)
    assert isinstance(sandbox, ApptainerRepoSandbox)


def test_resolve_verifier_sandbox_image_apptainer_clone(tmp_path) -> None:
    clone_sif = tmp_path / "clone.sif"
    clone_sif.write_bytes(b"x")
    settings = Settings(
        sandbox_backend="apptainer",
        apptainer_verifier_image=str(clone_sif),
    )
    assert resolve_verifier_sandbox_image(settings, needs_clone=True) == str(clone_sif)


def test_resolve_verifier_sandbox_image_docker() -> None:
    settings = Settings(
        sandbox_backend="docker",
        verifier_image="verifier-test-env:latest",
        verifier_clone_image="agent-fs-sandbox",
    )
    assert resolve_verifier_sandbox_image(settings, needs_clone=False) == "verifier-test-env:latest"
    assert resolve_verifier_sandbox_image(settings, needs_clone=True) == "agent-fs-sandbox"


def test_docker_create_execution_workspace_uses_verify_exec_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    sandbox = DockerRepoSandbox.__new__(DockerRepoSandbox)
    sandbox.container = object()
    sandbox._execution_workdir = "/repo"

    def fake_execute(cmd, check_exit_code=False):
        calls.append(cmd)
        return ""

    monkeypatch.setattr(sandbox, "execute", fake_execute)

    workspace = sandbox.create_execution_workspace("verify_exec")

    assert workspace == "/verify_exec"
    assert sandbox.execution_workdir == "/verify_exec"
    assert any("/verify_exec" in " ".join(cmd) for cmd in calls)
