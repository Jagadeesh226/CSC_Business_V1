"""Redis Streams wake-up messages for workers.

Messages carry only a task id. They are a speed-up, not the record: if Redis
is down or a message is lost, workers still find due tasks by polling
PostgreSQL, so nothing is lost and nothing runs twice.
"""

import logging
import os
import time
from collections.abc import Callable, Iterable

import redis

STREAM = "csc:tasks"
GROUP = "csc-workers"
# Keep the stream bounded; it is only a notification channel.
MAX_STREAM_LENGTH = 10_000

log = logging.getLogger(__name__)


_clients: dict[float | None, redis.Redis] = {}


def _default_factory(read_timeout: float | None = None) -> redis.Redis:
    """One client per read timeout, reused (connection pooling)."""
    if read_timeout not in _clients:
        _clients[read_timeout] = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            socket_timeout=read_timeout or 5,
            socket_connect_timeout=1,
        )
    return _clients[read_timeout]


_factory: Callable[..., redis.Redis] = _default_factory
# After a failed publish, skip Redis for a while so requests are not slowed
# by repeated connection attempts; workers poll in the meantime.
_PUBLISH_PAUSE_SECONDS = 30.0
_publish_paused_until = 0.0


def set_redis_factory(factory: Callable[..., redis.Redis] | None) -> None:
    """Tests swap in a fake Redis."""
    global _factory, _publish_paused_until
    _factory = factory or _default_factory
    _clients.clear()
    _publish_paused_until = 0.0


def get_redis(read_timeout: float | None = None) -> redis.Redis:
    """``read_timeout`` must exceed any blocking read's wait."""
    if _factory is _default_factory:
        return _factory(read_timeout)
    return _factory()


def publish(task_ids: Iterable[int]) -> None:
    """Best effort, never raises: a failure only delays tasks until the
    next poll (the task rows are already committed)."""
    global _publish_paused_until
    ids = list(task_ids)
    if not ids or time.monotonic() < _publish_paused_until:
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
    except Exception as exc:  # noqa: BLE001 - incl. a malformed REDIS_URL
        _publish_paused_until = time.monotonic() + _PUBLISH_PAUSE_SECONDS
        log.warning(
            "Could not notify workers (%s); they will poll.", type(exc).__name__
        )


def ensure_group(client: redis.Redis) -> None:
    try:
        client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
