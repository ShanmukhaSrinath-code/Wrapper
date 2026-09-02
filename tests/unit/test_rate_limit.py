"""Rate limiting: the budget is shared, published, and never takes the app down.

Three properties matter more than the arithmetic, and each has a test here:

1. **It fails open.** A Redis outage must cost the safeguard, not the service.
   Failing closed would turn a cache blip into a full outage -- a strictly worse
   incident than the one being prevented.
2. **The budget is published on every response**, not only on rejections. A
   caller that can discover the limit only by exceeding it has to exceed it.
3. **`X-Forwarded-For` is not trusted by default.** Trusting it unconditionally
   lets any caller forge a fresh identity per request and bypass the limit
   entirely, which is worse than having no limit because it looks like one.

The middleware is driven directly over ASGI rather than through `create_app()`,
so these need no compose stack.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.config import Settings
from app.core.middleware.ratelimit import RateLimitMiddleware


class _FakeRedis:
    """Enough Redis to run the window script, including its TTL semantics."""

    def __init__(self, *, fail: Exception | None = None, ttl_ms: int = 60_000) -> None:
        self.counts: dict[str, int] = {}
        self._fail = fail
        self._ttl_ms = ttl_ms
        self.script_calls = 0

    def register_script(self, _script: str) -> Any:
        async def run(keys: list[str], args: list[Any]) -> list[int]:
            self.script_calls += 1
            if self._fail is not None:
                raise self._fail
            key = keys[0]
            self.counts[key] = self.counts.get(key, 0) + 1
            return [self.counts[key], self._ttl_ms]

        return run


async def _ok(_request: Any) -> JSONResponse:
    return JSONResponse({"ok": True})


def _build(
    config: Settings, redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> httpx.AsyncClient:
    from app.core.middleware import ratelimit as mod

    monkeypatch.setattr(mod, "get_client", lambda: redis)
    app = Starlette(routes=[Route("/thing", _ok), Route("/health/live", _ok)])
    wrapped = RateLimitMiddleware(app, config=config)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="http://testserver"
    )


def _config(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"rate_limit_requests": 3, "rate_limit_window_seconds": 60}
    base.update(overrides)
    return Settings(**base)


# -- the happy path -----------------------------------------------------------
async def test_requests_under_the_limit_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()
    async with _build(_config(), redis, monkeypatch) as client:
        for _ in range(3):
            assert (await client.get("/thing")).status_code == 200


async def test_every_response_publishes_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """The remaining count must be discoverable without hitting the limit."""
    redis = _FakeRedis()
    async with _build(_config(), redis, monkeypatch) as client:
        first = await client.get("/thing")
        assert first.headers["X-RateLimit-Limit"] == "3"
        assert first.headers["X-RateLimit-Remaining"] == "2"
        assert first.headers["X-RateLimit-Reset"] == "60"

        second = await client.get("/thing")
        assert second.headers["X-RateLimit-Remaining"] == "1"


# -- rejection ----------------------------------------------------------------
async def test_over_the_limit_is_rejected_in_the_standard_error_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()
    async with _build(_config(), redis, monkeypatch) as client:
        for _ in range(3):
            assert (await client.get("/thing")).status_code == 200

        blocked = await client.get("/thing")
        assert blocked.status_code == 429
        body = blocked.json()
        # The same shape as every other error this service returns.
        assert body["error"] == "rate_limited"
        assert "message" in body
        assert body["detail"]["limit"] == 3
        assert body["detail"]["window_seconds"] == 60


async def test_rejection_says_when_to_come_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 without Retry-After invites the immediate retry it exists to stop."""
    redis = _FakeRedis(ttl_ms=42_000)
    async with _build(_config(), redis, monkeypatch) as client:
        for _ in range(3):
            await client.get("/thing")
        blocked = await client.get("/thing")

    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "42"
    assert blocked.headers["X-RateLimit-Remaining"] == "0"


async def test_a_rejected_request_never_reaches_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of rejecting in middleware: the work is not done at all."""
    calls = []

    async def counting(_request: Any) -> JSONResponse:
        calls.append(1)
        return JSONResponse({"ok": True})

    from app.core.middleware import ratelimit as mod

    redis = _FakeRedis()
    monkeypatch.setattr(mod, "get_client", lambda: redis)
    app = Starlette(routes=[Route("/thing", counting)])
    wrapped = RateLimitMiddleware(app, config=_config())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="http://testserver"
    ) as client:
        for _ in range(5):
            await client.get("/thing")

    assert len(calls) == 3, "the route ran for over-budget requests"


# -- degradation --------------------------------------------------------------
async def test_a_redis_outage_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Losing Redis must cost the safeguard, never availability."""
    redis = _FakeRedis(fail=RedisConnectionError("refused"))
    async with _build(_config(), redis, monkeypatch) as client:
        for _ in range(10):
            assert (await client.get("/thing")).status_code == 200
    assert redis.script_calls == 10, "the limiter stopped trying to count"


async def test_a_missing_ttl_never_yields_a_negative_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PTTL answers -1/-2 in a race with expiry; both must mean 'a full window'."""
    redis = _FakeRedis(ttl_ms=-1)
    async with _build(_config(), redis, monkeypatch) as client:
        response = await client.get("/thing")
    assert response.headers["X-RateLimit-Reset"] == "60"


# -- scoping ------------------------------------------------------------------
async def test_exempt_paths_are_never_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Throttling a liveness probe makes the orchestrator restart a healthy pod."""
    redis = _FakeRedis()
    async with _build(_config(), redis, monkeypatch) as client:
        for _ in range(20):
            assert (await client.get("/health/live")).status_code == 200
    assert redis.script_calls == 0


async def test_disabled_means_no_counting(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()
    async with _build(_config(rate_limit_enabled=False), redis, monkeypatch) as client:
        for _ in range(10):
            assert (await client.get("/thing")).status_code == 200
    assert redis.script_calls == 0


async def test_forwarded_for_is_ignored_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise a caller forges a new identity per request and has no limit."""
    redis = _FakeRedis()
    async with _build(_config(), redis, monkeypatch) as client:
        for i in range(5):
            await client.get("/thing", headers={"X-Forwarded-For": f"10.0.0.{i}"})

    # One key, not five: the spoofed header bought nothing.
    assert len(redis.counts) == 1


async def test_forwarded_for_is_used_when_explicitly_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()
    config = _config(rate_limit_trust_forwarded_for=True)
    async with _build(config, redis, monkeypatch) as client:
        await client.get("/thing", headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})

    # The left-most entry is the original client; proxies follow it.
    assert "ratelimit:203.0.113.7" in redis.counts


async def test_clients_have_independent_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()
    config = _config(rate_limit_trust_forwarded_for=True)
    async with _build(config, redis, monkeypatch) as client:
        for _ in range(3):
            await client.get("/thing", headers={"X-Forwarded-For": "1.1.1.1"})
        # A different caller starts fresh rather than inheriting the exhaustion.
        other = await client.get("/thing", headers={"X-Forwarded-For": "2.2.2.2"})

    assert other.status_code == 200


async def test_any_limiter_failure_still_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not only transport errors.

    This limiter runs before everything else on every request, so a bug in it
    that raised would take the whole service down -- a far worse outcome than an
    uncounted request. The real case that found this: the Redis pool is a
    process singleton bound to the loop that built it, and a stale connection
    raises `RuntimeError`, not a redis error.
    """
    redis = _FakeRedis(fail=RuntimeError("Event loop is closed"))
    async with _build(_config(), redis, monkeypatch) as client:
        response = await client.get("/thing")

    assert response.status_code == 200


async def test_the_script_is_rebound_when_the_client_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered script pins the client that registered it.

    The pool is disposed and rebuilt on shutdown and between tests, so a cached
    script must not outlive its client -- it would keep talking to the dead one.
    """
    from app.core.middleware import ratelimit as mod

    first, second = _FakeRedis(), _FakeRedis()
    current = [first]
    monkeypatch.setattr(mod, "get_client", lambda: current[0])

    app = Starlette(routes=[Route("/thing", _ok)])
    wrapped = RateLimitMiddleware(app, config=_config())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="http://testserver"
    ) as client:
        await client.get("/thing")
        current[0] = second  # the pool was disposed and rebuilt
        assert (await client.get("/thing")).status_code == 200

    assert first.script_calls == 1
    assert second.script_calls == 1, "the middleware kept using the disposed client"
