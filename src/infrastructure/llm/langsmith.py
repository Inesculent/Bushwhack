"""LangSmith environment wiring for LangChain and LangGraph tracing."""

from __future__ import annotations

import os

from src.config import Settings


def configure_langsmith_environment(settings: Settings) -> None:
    """Expose REVIEW_LANGSMITH_* settings through the env vars LangSmith reads."""
    os.environ["LANGSMITH_TRACING"] = "true" if settings.langsmith_tracing else "false"
    if not settings.langsmith_tracing:
        return

    _set_if_present("LANGSMITH_API_KEY", settings.langsmith_api_key)
    _set_if_present("LANGSMITH_PROJECT", settings.langsmith_project)
    _set_if_present("LANGSMITH_ENDPOINT", settings.langsmith_endpoint)
    _set_if_present("LANGSMITH_WORKSPACE_ID", settings.langsmith_workspace_id)
    os.environ["LANGSMITH_HIDE_INPUTS"] = "true" if settings.langsmith_hide_inputs else "false"
    os.environ["LANGSMITH_HIDE_OUTPUTS"] = "true" if settings.langsmith_hide_outputs else "false"
    if settings.langsmith_callbacks_background is not None:
        os.environ["LANGCHAIN_CALLBACKS_BACKGROUND"] = (
            "true" if settings.langsmith_callbacks_background else "false"
        )


def langsmith_model_metadata(configured_model_name: str, provider: str) -> dict[str, str]:
    """Return metadata LangSmith uses to label local OpenAI-compatible models."""
    metadata = {"ls_provider": provider}
    if provider == "local":
        metadata["ls_provider"] = "openai-compatible"
        metadata["ls_model_name"] = configured_model_name
    return metadata


def _set_if_present(name: str, value: str | None) -> None:
    if value:
        os.environ[name] = value
