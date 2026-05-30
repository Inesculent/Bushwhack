"""Redis health checks for LangGraph checkpointing."""
from __future__ import annotations

import os
import time
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
    retries = 3 if os.environ.get("SLURM_JOB_ID") else 1
    last_exc: Exception | None = None
    for attempt in range(retries):
        client: Redis | None = None
        probe_key = f"{namespace}:checkpoint_probe:{uuid4().hex}"
        try:
            client = Redis.from_url(redis_url)
            client.set(probe_key, "1", ex=30)
            client.delete(probe_key)
            return
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2)
        finally:
            if client is not None:
                client.close()
    raise RedisCheckpointUnavailable(
        f"Redis checkpoint writes unavailable: {last_exc.__class__.__name__}: {last_exc}"
    ) from last_exc


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


def _delete_keys_by_pattern(client: Redis, pattern: str, *, batch_size: int = 500) -> int:
    deleted = 0
    batch: list[bytes] = []
    for key in client.scan_iter(match=pattern, count=batch_size):
        batch.append(key)
        if len(batch) >= batch_size:
            deleted += int(client.unlink(*batch))
            batch.clear()
    if batch:
        deleted += int(client.unlink(*batch))
    return deleted


def delete_checkpoint_thread(settings: Settings, thread_id: str) -> None:
    """Delete Redis checkpoints and pending writes for a completed graph thread."""
    with redis_checkpoint_saver(settings) as saver:
        saver.delete_thread(thread_id)

    client: Redis | None = None
    try:
        client = Redis.from_url(settings.redis_url)
        checkpoint_prefix = _prefix_key(settings.redis_namespace, "checkpoint")
        write_prefix = _prefix_key(settings.redis_namespace, "checkpoint_write")
        # LangGraph's Redis saver stores large per-node channel writes separately
        # from checkpoint rows. delete_thread() may leave those writes behind.
        _delete_keys_by_pattern(client, f"{checkpoint_prefix}:{thread_id}:*")
        _delete_keys_by_pattern(client, f"{write_prefix}:{thread_id}:*")
    finally:
        if client is not None:
            client.close()
