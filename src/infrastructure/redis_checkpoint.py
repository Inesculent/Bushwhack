"""Redis health checks for LangGraph checkpointing."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from langgraph.checkpoint.redis import RedisSaver
from redis import Redis

from src.config import Settings


class RedisCheckpointUnavailable(RuntimeError):
    """Raised when Redis cannot safely accept checkpoint writes."""


def _prefix_key(namespace: str, key: str) -> str:
    cleaned_namespace = namespace.strip(":")
    if not cleaned_namespace:
        return key
    return f"{cleaned_namespace}:{key}"


def assert_redis_checkpoint_writable(redis_url: str, *, namespace: str) -> None:
    """Fail fast when Redis is reachable but configured to reject writes."""
    client: Redis | None = None
    probe_key = f"{namespace}:checkpoint_probe:{uuid4().hex}"

    try:
        client = Redis.from_url(redis_url)
        client.set(probe_key, "1", ex=30)
        client.delete(probe_key)
    except Exception as exc:
        raise RedisCheckpointUnavailable(
            f"Redis checkpoint writes unavailable: {exc.__class__.__name__}: {exc}"
        ) from exc
    finally:
        if client is not None:
            client.close()


@contextmanager
def redis_checkpoint_saver(settings: Settings) -> Iterator[RedisSaver]:
    """Create a RedisSaver with bounded retention for checkpoint data."""
    ttl_minutes = settings.redis_ttl_seconds / 60
    with RedisSaver.from_conn_string(
        settings.redis_url,
        ttl={"default_ttl": ttl_minutes},
        checkpoint_prefix=_prefix_key(settings.redis_namespace, "checkpoint"),
        checkpoint_write_prefix=_prefix_key(
            settings.redis_namespace,
            "checkpoint_write",
        ),
    ) as saver:
        saver.setup()
        yield saver


def delete_checkpoint_thread(settings: Settings, thread_id: str) -> None:
    """Delete Redis checkpoints for a completed graph thread."""
    with redis_checkpoint_saver(settings) as saver:
        saver.delete_thread(thread_id)
