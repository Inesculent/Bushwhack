"""Microscopic snapshot pointer storage (Redis or in-memory)."""

from __future__ import annotations

import json
from typing import Any, Dict, Protocol

from redis import Redis

from src.config import Settings


class SnapshotPointerStore(Protocol):
    def write_pointer(
        self,
        *,
        run_id: str,
        snapshot_id: str,
        snapshot_root: str,
        status: str,
    ) -> None:
        ...


class InMemorySnapshotPointerStore:
    """Test-friendly store that keeps the last pointer per run_id."""

    def __init__(self) -> None:
        self.pointers: Dict[str, Dict[str, Any]] = {}

    def write_pointer(
        self,
        *,
        run_id: str,
        snapshot_id: str,
        snapshot_root: str,
        status: str,
    ) -> None:
        self.pointers[run_id] = {
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "snapshot_root": snapshot_root,
            "status": status,
        }


class RedisSnapshotPointerStore:
    """Redis-backed pointer records under a namespace distinct from checkpoints."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ttl = int(settings.semantic_snapshot_pointer_ttl_seconds)

    def _key(self, run_id: str) -> str:
        ns = self._settings.redis_namespace.strip(":") or "langgraph"
        return f"{ns}:snapshot_pointer:{run_id}"

    def write_pointer(
        self,
        *,
        run_id: str,
        snapshot_id: str,
        snapshot_root: str,
        status: str,
    ) -> None:
        payload = {
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "snapshot_root": snapshot_root,
            "status": status,
        }
        client = Redis.from_url(self._settings.redis_url)
        try:
            client.set(self._key(run_id), json.dumps(payload, sort_keys=True), ex=self._ttl)
        finally:
            client.close()
