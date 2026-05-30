"""Unit tests for ApptainerRepoSandbox (mocked CLI)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.infrastructure.sandbox_apptainer import ApptainerRepoSandbox


@pytest.fixture()
def sif_path(tmp_path: Path) -> Path:
    sif = tmp_path / "agent.sif"
    sif.write_bytes(b"fake-sif")
    return sif


def test_apptainer_start_mounts_repo(monkeypatch: pytest.MonkeyPatch, sif_path: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("src.infrastructure.sandbox_apptainer.subprocess.run", fake_run)

    sandbox = ApptainerRepoSandbox(sif_path=str(sif_path), bind_tmpfs=True)
    instance_id = sandbox.start(str(repo))

    assert instance_id.startswith("bw-")
    start_cmd = calls[0]
    assert "instance" in start_cmd
    assert "start" in start_cmd
    assert any(f"{repo.resolve()}:/repo:ro" in arg for arg in start_cmd)
    sandbox.stop()


def test_apptainer_execute_uses_instance_uri(
    monkeypatch: pytest.MonkeyPatch, sif_path: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(cmd, **kwargs):
        return MagicMock(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("src.infrastructure.sandbox_apptainer.subprocess.run", fake_run)

    sandbox = ApptainerRepoSandbox(sif_path=str(sif_path))
    sandbox.start(str(repo))
    result = sandbox.execute_result(["echo", "hello"])
    assert result.exit_code == 0
    assert "ok" in result.stdout
    sandbox.stop()
