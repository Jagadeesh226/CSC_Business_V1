"""Redis Streams wake-up messages for workers.

Messages carry only a task id. They are a speed-up, not the record: if Redis
is down or a message is lost, workers still find due tasks by polling
PostgreSQL, so nothing is lost and nothing runs twice.
"""

import logging
import os
from collections.abc import Callable, Iterable

import redis

STREAM = "csc:tasks"
GROUP = "csc-workers"
# Keep the stream bounded; it is only a notification channel.
MAX_STREAM_LENGTH = 10_000

log = logging.getLogger(__name__)


def _default_factory() -> redis.Redis:
    return redis.Redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        socket_timeout=5,
        socket_connect_timeout=2,
    )


_factory: Callable[[], redis.Redis] = _default_factory


def set_redis_factory(factory: Callable[[], redis.Redis] | None) -> None:
    """Tests swap in a fake Redis."""
    global _factory
    _factory = factory or _default_factory


def get_redis() -> redis.Redis:
    return _factory()


def publish(task_ids: Iterable[int]) -> None:
    """Best effort: a failure here only delays tasks until the next poll."""
    ids = list(task_ids)
    if not ids:
        return
    try:
        client = get_redis()
        for task_id in ids:
            client.xadd(
                STREAM,
                {"task_id": str(task_id)},
                maxlen=MAX_STREAM_LENGTH,
                approximate=True,
            )
    except redis.RedisError as exc:
        log.warning(
            "Could not notify workers (%s); they will poll.", type(exc).__name__
        )


def ensure_group(client: redis.Redis) -> None:
    try:
        client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
