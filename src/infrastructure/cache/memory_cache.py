from time import time
from typing import Any, Dict, Optional, Tuple

from src.domain.interfaces import ICacheService


class InMemoryCache(ICacheService):
    """Small in-process cache used for local development and tests."""

    def __init__(self) -> None:
        self._store: Dict[str, Tuple[Optional[float], Dict[str, Any]]] = {}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self._store.get(key)
        if entry is None:
            return None

        expires_at, value = entry
        if expires_at is not None and expires_at <= time():
            del self._store[key]
            return None

        return value

    def set(self, key: str, value: Dict[str, Any], expire: int = 3600) -> None:
        expires_at = None if expire <= 0 else time() + expire
        self._store[key] = (expires_at, value)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def exists(self, key: str) -> bool:
        return self.get(key) is not None
