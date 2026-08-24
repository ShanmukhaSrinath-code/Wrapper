"""Redis client and small caching helpers.

The client is created lazily and shared process-wide -- ``redis.asyncio``
manages its own connection pool, so a single client is the right unit.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

_client: redis.Redis | None = None


def get_client() -> redis.Redis:
    """Lazily create the process-wide Redis client."""
    global _client
    if _client is None:
        _client = redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            health_check_interval=30,
        )
    return _client


async def ping() -> bool:
    """Readiness probe for Redis."""
    return bool(await get_client().ping())


async def close_client() -> None:
    """Close the pool on shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------
#: Transport failures that mean "Redis is not reachable right now". These are
#: survivable: a cache is an optimisation, so losing it should cost latency, not
#: availability. Anything *else* -- a TypeError, a bad serialiser -- is a bug and
#: is deliberately left to propagate.
CACHE_OUTAGE_ERRORS = (RedisConnectionError, RedisTimeoutError)


# --------------------------------------------------------------------------
# JSON value helpers
# --------------------------------------------------------------------------
async def get_json(key: str) -> Any | None:
    """Return the decoded value at ``key``, or ``None`` on miss.

    A cache outage is reported as a miss. The caller then recomputes from the
    origin, which is exactly what it would do for a genuine miss -- so an outage
    degrades a route's latency rather than its status code.
    """
    try:
        raw = await get_client().get(key)
    except CACHE_OUTAGE_ERRORS as exc:
        log.warning("cache.unavailable", operation="get", key=key, error=str(exc))
        return None

    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # A poisoned key must not break the caller -- treat it as a miss. If the
        # cache is also unreachable, the eviction simply does not happen.
        with contextlib.suppress(*CACHE_OUTAGE_ERRORS):
            await get_client().delete(key)
        return None


async def set_json(key: str, value: Any, ttl_seconds: int | None = None) -> None:
    """Store ``value`` as JSON with a TTL (defaults to ``CACHE_TTL_SECONDS``).

    Failing to *populate* a cache must never fail the request that just did the
    work to compute the value.
    """
    ttl = settings.cache_ttl_seconds if ttl_seconds is None else ttl_seconds
    try:
        await get_client().set(key, json.dumps(value, default=str), ex=ttl)
    except CACHE_OUTAGE_ERRORS as exc:
        log.warning("cache.unavailable", operation="set", key=key, error=str(exc))


async def delete(*keys: str) -> int:
    """Drop keys; returns how many existed (0 if the cache is unreachable)."""
    if not keys:
        return 0
    try:
        return int(await get_client().delete(*keys))
    except CACHE_OUTAGE_ERRORS as exc:
        log.warning("cache.unavailable", operation="delete", keys=list(keys), error=str(exc))
        return 0


async def get_or_set[T](
    key: str,
    producer: Callable[[], Awaitable[T]],
    ttl_seconds: int | None = None,
) -> tuple[T, bool]:
    """Cache-aside read.

    Returns ``(value, hit)`` so callers can surface or log whether the value
    came from Redis -- the honest alternative to guessing from timing.
    """
    cached = await get_json(key)
    if cached is not None:
        return cached, True

    value = await producer()
    await set_json(key, value, ttl_seconds)
    return value, False
