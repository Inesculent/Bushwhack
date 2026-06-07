"""Opt-in Apptainer smoke tests (cluster / --remote profile).

    APPTAINER_SANDBOX_INTEGRATION=1 pytest src/infrastructure/tests/test_apptainer_sandbox_integration.py -m integration
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from src.config import Settings
from src.infrastructure.sandbox import build_repo_sandbox, sandbox_runtime_available


def _integration_enabled() -> bool:
    return os.getenv("APPTAINER_SANDBOX_INTEGRATION", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@pytest.mark.integration
def test_apptainer_sandbox_echo(repo_root: Path) -> None:
    if not _integration_enabled():
        pytest.skip("Set APPTAINER_SANDBOX_INTEGRATION=1 (see module docstring)")
    if not shutil.which("apptainer"):
        pytest.skip("apptainer not on PATH")

    sif = os.environ.get("REVIEW_APPTAINER_IMAGE", "").strip()
    if not sif:
        pytest.skip("Set REVIEW_APPTAINER_IMAGE to a built .sif path")

    settings = Settings(sandbox_backend="apptainer", apptainer_image=sif)
    if not sandbox_runtime_available(settings):
        pytest.skip(f"Apptainer runtime not ready for {sif}")

    sandbox = build_repo_sandbox(settings)
    try:
        sandbox.start(str(repo_root))
        result = sandbox.execute_result(["echo", "apptainer_ok"], workdir="/repo")
        assert result.exit_code == 0
        assert "apptainer_ok" in result.stdout
    finally:
        sandbox.stop()
