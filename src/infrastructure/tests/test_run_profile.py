"""Tests for --local / --remote execution profile presets."""

from __future__ import annotations

import os

import pytest

from src.config import Settings, get_settings
from src.infrastructure.run_profile import apply_run_profile


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_apply_run_profile_local_sets_docker_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_SANDBOX_BACKEND", raising=False)
    apply_run_profile("local")
    settings = Settings()
    assert settings.sandbox_backend == "docker"
    assert settings.redis_url == "redis://localhost:6379/0"


def test_apply_run_profile_remote_sets_apptainer_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_SANDBOX_BACKEND", raising=False)
    apply_run_profile("remote")
    settings = Settings()
    assert settings.sandbox_backend == "apptainer"
    assert settings.ast_mcp_enabled is False
    assert settings.redis_url == "redis://127.0.0.1:6379/0"
    assert settings.local_llm_base_url == "http://127.0.0.1:8000/v1"
