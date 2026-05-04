"""Manual smoke check for the local Qwen endpoint.

Run from the repository root:
    python src/infrastructure/tests/manual_qwen_llm_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
from openai import APIConnectionError, APIStatusError

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.config import get_settings
from src.infrastructure.llm.factory import Models


def _models_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/models"


def main() -> None:
    settings = get_settings()
    print(f"base_url: {settings.local_llm_base_url}")
    print("checking /models...")

    try:
        response = httpx.get(
            _models_url(settings.local_llm_base_url),
            headers={"Authorization": f"Bearer {settings.local_llm_api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        model_ids = [item.get("id") for item in response.json().get("data", [])]
        print(f"models: {model_ids}")
    except Exception as exc:  # noqa: BLE001 - this is a manual diagnostic script
        print(f"/models check failed: {exc.__class__.__name__}: {exc}")
        print("Verify the SSH tunnel is open and the remote server exposes an OpenAI-compatible /v1 endpoint.")
        return

    llm = Models.get("qwen2.5-coder-7b")
    try:
        response = llm.invoke("Reply with exactly: qwen endpoint ok")
    except APIConnectionError as exc:
        print(f"chat completion connection failed: {exc.__class__.__name__}: {exc}")
        print("The endpoint accepted the socket but closed during generation; check the remote model server logs.")
        return
    except APIStatusError as exc:
        print(f"chat completion HTTP error: {exc.status_code}: {exc.response.text}")
        return

    print("completion:")
    print(getattr(response, "content", response))


if __name__ == "__main__":
    main()
