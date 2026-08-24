"""Regression tests for Fix 5 -- a cache outage must cost latency, not uptime.

The audit found that stopping Redis turned every cache-backed route into a 500:
``redis.exceptions.TimeoutError`` propagated straight out of ``get_or_set``. A
cache is an optimisation; losing it should make responses slower, not make the
service unavailable.

Readiness still reports Redis as down -- that part was already correct and must
stay correct, so traffic drains on its own. What changes is that requests already
in flight, and requests to a pod that has not yet been removed from the Service,
keep succeeding.
"""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError


class _DeadRedis:
    """A Redis client that fails the way a stopped Redis actually fails."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls: list[str] = []

    async def get(self, *_: Any, **__: Any) -> Any:
        self.calls.append("get")
        raise self._exc

    async def set(self, *_: Any, **__: Any) -> Any:
        self.calls.append("set")
        raise self._exc

    async def delete(self, *_: Any, **__: Any) -> Any:
        self.calls.append("delete")
        raise self._exc


@pytest.fixture(params=[RedisTimeoutError("timeout"), RedisConnectionError("refused")])
def dead_client(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> _DeadRedis:
    """Patch the module's client factory to return a broken client."""
    from app.cache import client as client_mod

    dead = _DeadRedis(request.param)
    monkeypatch.setattr(client_mod, "get_client", lambda: dead)
    return dead


async def test_get_json_returns_none_instead_of_raising(dead_client: _DeadRedis) -> None:
    """A read failure is indistinguishable from a miss, which is the point."""
    from app.cache import get_json

    assert await get_json("any-key") is None
    assert "get" in dead_client.calls


async def test_set_json_swallows_the_failure(dead_client: _DeadRedis) -> None:
    """Failing to populate a cache must not fail the request that computed it."""
    from app.cache import set_json

    await set_json("any-key", {"value": 1}, 60)
    assert "set" in dead_client.calls


async def test_delete_swallows_the_failure(dead_client: _DeadRedis) -> None:
    from app.cache import delete

    assert await delete("any-key") == 0


async def test_get_or_set_falls_through_to_the_origin(dead_client: _DeadRedis) -> None:
    """The whole point: the caller still gets its answer, computed live."""
    from app.cache import get_or_set

    calls = {"n": 0}

    async def origin() -> dict[str, int]:
        calls["n"] += 1
        return {"computed": 42}

    value, hit = await get_or_set("any-key", origin)

    assert value == {"computed": 42}
    assert hit is False, "a failed cache read must report a miss, not a hit"
    assert calls["n"] == 1, "the origin function must have been called"


async def test_get_or_set_still_reports_a_hit_when_redis_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degradation must not mask a working cache."""
    import json

    from app.cache import client as client_mod
    from app.cache import get_or_set

    class _LiveRedis:
        async def get(self, *_: Any, **__: Any) -> str:
            return json.dumps({"computed": 7})

        async def set(self, *_: Any, **__: Any) -> None:
            return None

    monkeypatch.setattr(client_mod, "get_client", lambda: _LiveRedis())

    async def origin() -> dict[str, int]:  # pragma: no cover - must not run
        raise AssertionError("origin called despite a live cache hit")

    value, hit = await get_or_set("any-key", origin)
    assert value == {"computed": 7}
    assert hit is True


async def test_unexpected_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only Redis transport failures degrade. A bug must still be loud."""
    from app.cache import client as client_mod
    from app.cache import get_json

    class _BrokenClient:
        async def get(self, *_: Any, **__: Any) -> Any:
            raise ValueError("this is a programming error, not an outage")

    monkeypatch.setattr(client_mod, "get_client", lambda: _BrokenClient())

    with pytest.raises(ValueError):
        await get_json("any-key")
