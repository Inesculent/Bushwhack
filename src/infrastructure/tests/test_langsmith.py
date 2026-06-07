from __future__ import annotations

import os
from typing import Any

from src.config import Settings
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.langsmith import configure_langsmith_environment


def test_settings_accept_review_langsmith_env(monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_LANGSMITH_TRACING", "true")
    monkeypatch.setenv("REVIEW_LANGSMITH_API_KEY", "lsv2-test")
    monkeypatch.setenv("REVIEW_LANGSMITH_PROJECT", "trace-project")
    monkeypatch.setenv("REVIEW_LANGSMITH_WORKSPACE_ID", "workspace-id")
    monkeypatch.setenv("REVIEW_LANGSMITH_HIDE_INPUTS", "true")
    monkeypatch.setenv("REVIEW_LANGSMITH_HIDE_OUTPUTS", "false")

    settings = Settings(_env_file=None)

    assert settings.langsmith_tracing is True
    assert settings.langsmith_api_key == "lsv2-test"
    assert settings.langsmith_project == "trace-project"
    assert settings.langsmith_workspace_id == "workspace-id"
    assert settings.langsmith_hide_inputs is True
    assert settings.langsmith_hide_outputs is False


def test_configure_langsmith_environment_sets_langchain_vars(monkeypatch) -> None:
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGSMITH_HIDE_INPUTS", raising=False)
    monkeypatch.delenv("LANGSMITH_HIDE_OUTPUTS", raising=False)
    monkeypatch.delenv("LANGCHAIN_CALLBACKS_BACKGROUND", raising=False)
    settings = Settings(
        _env_file=None,
        REVIEW_LANGSMITH_TRACING=True,
        REVIEW_LANGSMITH_API_KEY="lsv2-test",
        REVIEW_LANGSMITH_PROJECT="trace-project",
        REVIEW_LANGSMITH_CALLBACKS_BACKGROUND=False,
    )

    configure_langsmith_environment(settings)

    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2-test"
    assert os.environ["LANGSMITH_PROJECT"] == "trace-project"
    assert os.environ["LANGSMITH_HIDE_INPUTS"] == "false"
    assert os.environ["LANGSMITH_HIDE_OUTPUTS"] == "true"
    assert os.environ["LANGCHAIN_CALLBACKS_BACKGROUND"] == "false"


def test_models_get_adds_langsmith_metadata_for_local_models(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setenv("REVIEW_LANGSMITH_TRACING", "true")
    monkeypatch.setenv("REVIEW_LANGSMITH_API_KEY", "lsv2-test")
    monkeypatch.setattr(
        "src.infrastructure.llm.factory._get_llm_class",
        lambda _provider: FakeChatOpenAI,
    )
    monkeypatch.setattr(
        "src.infrastructure.llm.factory.get_settings",
        lambda: Settings(_env_file=None),
    )

    Models.get("qwen2.5-coder-7b")

    assert captured["metadata"]["ls_provider"] == "openai-compatible"
    assert captured["metadata"]["ls_model_name"] == "Qwen/Qwen2.5-Coder-7B-Instruct"
    assert os.environ["LANGSMITH_TRACING"] == "true"


def test_real_chat_openai_accepts_langsmith_metadata(monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_LANGSMITH_TRACING", "false")

    llm = Models.get("qwen2.5-coder-7b")

    assert llm.metadata["ls_provider"] == "openai-compatible"
    assert llm.metadata["ls_model_name"] == "Qwen/Qwen2.5-Coder-7B-Instruct"
