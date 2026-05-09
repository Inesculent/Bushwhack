"""Helpers for classifying local OpenAI-compatible LLM timeouts."""

from __future__ import annotations

import time
from typing import List
from urllib.parse import urlsplit, urlunsplit

import httpx

from src.config import Settings
from src.infrastructure.llm.factory import MODELS


def is_local_model(model_key: str) -> bool:
    config = MODELS.get(model_key)
    return config is not None and config.provider == "local"


def status_urls(base_url: str) -> List[str]:
    normalized = base_url.rstrip("/")
    split = urlsplit(normalized)
    path = split.path.rstrip("/")
    candidates: List[str] = []

    if path.endswith("/v1"):
        root_path = path[: -len("/v1")] or "/"
        root = urlunsplit((split.scheme, split.netloc, root_path.rstrip("/"), "", ""))
        candidates.append(f"{root.rstrip('/')}/health")

    candidates.append(f"{normalized}/health")
    candidates.append(f"{normalized}/models")
    return list(dict.fromkeys(candidates))


def is_timeout_exception(exc: Exception) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        class_name = current.__class__.__name__.lower()
        message = str(current).lower()
        if "timeout" in class_name or "timed out" in message or "request timed out" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


def local_llm_server_active(settings: Settings) -> tuple[bool, str]:
    headers = {"Authorization": f"Bearer {settings.local_llm_api_key}"}
    last_error = "no status endpoints checked"
    for url in status_urls(settings.local_llm_base_url):
        try:
            response = httpx.get(url, headers=headers, timeout=settings.local_llm_status_timeout_seconds)
        except httpx.HTTPError as exc:
            last_error = f"{url}: {exc.__class__.__name__}"
            continue
        if response.status_code < 500 and response.status_code != 404:
            return True, f"{url}: HTTP {response.status_code}"
        last_error = f"{url}: HTTP {response.status_code}"
    return False, last_error


def sleep_for_retry(backoff_seconds: float, attempt: int, deadline: float | None = None) -> None:
    sleep_for = backoff_seconds * attempt
    if deadline is not None:
        sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
    if sleep_for > 0:
        time.sleep(sleep_for)
