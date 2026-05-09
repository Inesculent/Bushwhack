"""Real-Docker smoke tests for verifier-style execution (matches sandbox_executor path).

There is no mock here: we start ``RepoSandbox`` with ``Settings.verifier_image``, push a tiny
script with ``write_file_in_container``, and run it via ``execute_result``.

Opt-in so default ``pytest`` runs do not require Docker or a built verifier image::

    VERIFIER_SANDBOX_INTEGRATION=1 pytest src/infrastructure/tests/test_verifier_sandbox_integration.py -m integration

Build the image first: ``scripts/build_verifier_image.sh`` (or ``.ps1`` on Windows).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.config import get_settings


def _docker_reachable() -> bool:
    try:
        import docker

        return bool(docker.from_env().ping())
    except Exception:  # noqa: BLE001
        return False


def _integration_enabled() -> bool:
    return os.getenv("VERIFIER_SANDBOX_INTEGRATION", "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.mark.integration
def test_verifier_sandbox_writes_and_executes_python(repo_root: Path) -> None:
    """Inline ``python -c`` plus ``/tmp`` script upload — same primitives as the verifier."""
    if not _integration_enabled():
        pytest.skip("Set VERIFIER_SANDBOX_INTEGRATION=1 (see module docstring)")
    if not _docker_reachable():
        pytest.skip("Docker daemon not reachable")

    from docker.errors import ImageNotFound

    from src.infrastructure.sandbox import RepoSandbox

    settings = get_settings()
    image = settings.verifier_image
    sandbox = RepoSandbox(image_name=image)
    try:
        try:
            sandbox.start(str(repo_root))
        except ImageNotFound:
            pytest.skip(f"Image not found locally: {image} — build with scripts/build_verifier_image.sh")

        inline = sandbox.execute_result(
            ["python", "-c", "print('verifier_inline_ok')"],
            workdir="/repo",
        )
        assert inline.exit_code == 0, (inline.stdout, inline.stderr)
        assert "verifier_inline_ok" in inline.stdout

        remote_path = "/tmp/verifier_integration_smoke.py"
        sandbox.write_file_in_container(
            remote_path,
            b"print('verifier_file_ok')\n",
        )
        file_run = sandbox.execute_result(["python", remote_path], workdir="/repo")
        assert file_run.exit_code == 0, (file_run.stdout, file_run.stderr)
        assert "verifier_file_ok" in file_run.stdout
    finally:
        sandbox.stop()
