from __future__ import annotations

import pytest

from src.config import Settings
from src.infrastructure import redis_checkpoint
from src.infrastructure.redis_checkpoint import (
    RedisCheckpointUnavailable,
    assert_redis_checkpoint_writable,
    delete_checkpoint_thread,
    redis_checkpoint_saver,
)


def test_assert_redis_checkpoint_writable_probes_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class StubRedis:
        @classmethod
        def from_url(cls, url: str) -> "StubRedis":
            calls.append(("from_url", url))
            return cls()

        def set(self, key: str, value: str, *, ex: int) -> None:
            calls.append(("set", (key, value, ex)))

        def delete(self, key: str) -> None:
            calls.append(("delete", key))

        def close(self) -> None:
            calls.append(("close", None))

    monkeypatch.setattr(redis_checkpoint, "Redis", StubRedis)

    assert_redis_checkpoint_writable("redis://example/0", namespace="test")

    assert calls[0] == ("from_url", "redis://example/0")
    assert calls[1][0] == "set"
    assert calls[2][0] == "delete"
    assert calls[3] == ("close", None)


def test_assert_redis_checkpoint_writable_raises_when_writes_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubRedis:
        @classmethod
        def from_url(cls, url: str) -> "StubRedis":
            return cls()

        def set(self, key: str, value: str, *, ex: int) -> None:
            raise RuntimeError("MISCONF Redis is configured to save RDB snapshots")

        def close(self) -> None:
            return None

    monkeypatch.setattr(redis_checkpoint, "Redis", StubRedis)

    with pytest.raises(RedisCheckpointUnavailable, match="MISCONF"):
        assert_redis_checkpoint_writable("redis://example/0", namespace="test")


def test_redis_checkpoint_saver_applies_ttl_and_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    calls: list[str] = []

    class StubSaver:
        @classmethod
        def from_conn_string(cls, redis_url: str, **kwargs: object) -> "StubSaver":
            captured["redis_url"] = redis_url
            captured.update(kwargs)
            return cls()

        def __enter__(self) -> "StubSaver":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def setup(self) -> None:
            calls.append("setup")

    monkeypatch.setattr(redis_checkpoint, "RedisSaver", StubSaver)

    settings = Settings(
        redis_url="redis://example/0",
        redis_namespace="reviewer",
        redis_ttl_seconds=90,
    )

    with redis_checkpoint_saver(settings) as saver:
        assert isinstance(saver, StubSaver)

    assert captured["redis_url"] == "redis://example/0"
    assert captured["ttl"] == {"default_ttl": 1.5}
    assert captured["checkpoint_prefix"] == "reviewer:checkpoint"
    assert captured["checkpoint_write_prefix"] == "reviewer:checkpoint_write"
    assert calls == ["setup"]


def test_delete_checkpoint_thread_uses_configured_saver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_threads: list[str] = []
    scan_patterns: list[str] = []
    unlinked: list[tuple[bytes, ...]] = []

    class StubSaver:
        @classmethod
        def from_conn_string(cls, redis_url: str, **kwargs: object) -> "StubSaver":
            return cls()

        def __enter__(self) -> "StubSaver":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def setup(self) -> None:
            return None

        def delete_thread(self, thread_id: str) -> None:
            deleted_threads.append(thread_id)

    class StubRedis:
        @classmethod
        def from_url(cls, url: str) -> "StubRedis":
            return cls()

        def scan_iter(self, *, match: str, count: int):
            scan_patterns.append(match)
            if "checkpoint_write" in match:
                yield b"write-key-1"
                yield b"write-key-2"
            else:
                yield b"checkpoint-key"

        def unlink(self, *keys: bytes) -> int:
            unlinked.append(keys)
            return len(keys)

        def close(self) -> None:
            return None

    monkeypatch.setattr(redis_checkpoint, "RedisSaver", StubSaver)
    monkeypatch.setattr(redis_checkpoint, "Redis", StubRedis)

    settings = Settings(redis_namespace="reviewer")
    delete_checkpoint_thread(settings, "run:owner__repo__pr1")

    assert deleted_threads == ["run:owner__repo__pr1"]
    assert "reviewer:checkpoint:run:owner__repo__pr1:*" in scan_patterns
    assert "reviewer:checkpoint_write:run:owner__repo__pr1:*" in scan_patterns
    assert (b"checkpoint-key",) in unlinked
    assert (b"write-key-1", b"write-key-2") in unlinked
