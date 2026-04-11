"""
Centralized LLM configuration and model factory
"""


import os
from dataclasses import dataclass
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Any, Literal, Optional, Type

load_dotenv()



@dataclass
class LLMConfig:
    provider: Literal["google", "openai", "anthropic", "local"]
    model_name: str
    api_key_env: Optional[str] = None  # Name of the environment variable containing the API key


MODELS = {
    # Google models
    "gemini-flash": LLMConfig(
        model_name="gemini-3.1-flash-lite-preview",
        provider="google",
        api_key_env="GOOGLE_API_KEY",
    ),
    "gemini-pro": LLMConfig(
        model_name="gemini-3.1-pro-preview",
        provider="google",
        api_key_env="GOOGLE_API_KEY",
    ),

    # OpenAI models
    "gpt-5.4": LLMConfig(
        model_name="gpt-5.4",
        provider="openai",
        api_key_env="OPENAI_API_KEY",
    ),
    "gpt-5.4-mini": LLMConfig(
        model_name="gpt-5.4-mini",
        provider="openai",
        api_key_env="OPENAI_API_KEY",
    ),
}


class Models:
    """
    Factory for creating LLM instances based on predefined configurations.
    """

    DEFAULT_ROLE_MODELS = {
        "explorer": "gemini-pro",
        "planner": "gemini-pro",
        "worker": "gemini-pro",
        "synthesizer": "gemini-pro",
    }

    @staticmethod
    def get(model_key: str):
        config = _get_model_config(model_key)
        llm_class = _get_llm_class(config.provider)
        llm_kwargs = _build_llm_kwargs(config)
        return llm_class(**llm_kwargs)

    @staticmethod
    def get_structured(model_key: str, schema: Type[BaseModel]):
        return Models.get(model_key).with_structured_output(schema)

    @staticmethod
    def explorer(schema: Type[BaseModel], model_key: Optional[str] = None):
        selected_model = model_key or Models.DEFAULT_ROLE_MODELS["explorer"]
        return Models.get_structured(selected_model, schema)

    @staticmethod
    def planner(schema: Type[BaseModel], model_key: Optional[str] = None):
        selected_model = model_key or Models.DEFAULT_ROLE_MODELS["planner"]
        return Models.get_structured(selected_model, schema)

    @staticmethod
    def worker(schema: Type[BaseModel], model_key: Optional[str] = None):
        selected_model = model_key or Models.DEFAULT_ROLE_MODELS["worker"]
        return Models.get_structured(selected_model, schema)

    @staticmethod
    def synthesizer(schema: Type[BaseModel], model_key: Optional[str] = None):
        selected_model = model_key or Models.DEFAULT_ROLE_MODELS["synthesizer"]
        return Models.get_structured(selected_model, schema)


def _get_model_config(model_key: str) -> LLMConfig:
    config = MODELS.get(model_key)
    if config is None:
        available_keys = ", ".join(sorted(MODELS.keys()))
        raise ValueError(f"Unknown model key: {model_key}. Available keys: {available_keys}")
    return config


def _get_llm_class(provider: str):
    """Get the appropriate LLM class for the provider."""
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic

    raise ValueError(f"Unknown provider: {provider}")


def _build_llm_kwargs(config: LLMConfig) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"model": config.model_name}
    if not config.api_key_env:
        return kwargs

    api_key_value = os.getenv(config.api_key_env)
    if not api_key_value:
        return kwargs

    if config.provider == "google":
        kwargs["google_api_key"] = api_key_value
    elif config.provider in {"openai", "anthropic"}:
        kwargs["api_key"] = api_key_value

    return kwargs


def list_available_models():
    """Print all available model configurations."""
    print("Available Models:")
    print("-" * 50)
    for key, config in MODELS.items():
        print(f"  {key}")
        print(f"    Provider: {config.provider}")
        print(f"    Model: {config.model_name}")
        print(f"    API Key: {config.api_key_env}")
        print()
