"""Request rate limiting, shared across replicas via Redis.

The counter lives in Redis rather than in the process, because an in-process
counter multiplies by the replica count: three pods each allowing 120 requests
per minute is a 360-request limit that nobody configured. A shared counter means
the number in the config is the number the caller actually gets.

Three decisions worth knowing about:

**It fails open.** If Redis is unreachable the request is allowed and the outage
is logged. Rate limiting is a protection, not a correctness requirement --
failing closed would convert a Redis blip into a total outage, which is strictly
worse than the incident being prevented. This mirrors ``app.core.cache``: losing
Redis costs a safeguard, never uptime.

**The window is fixed, not sliding.** ``INCR`` on a keyed window is one round
trip and needs no per-request bookkeeping. The known cost is a boundary burst: a
caller can spend its budget at the end of one window and again at the start of
the next, so the true worst case is 2x the limit over a window's width. For an
abuse and runaway-retry brake that is an acceptable trade; a metered quota would
need a sliding window and should say so.

**Every response carries the budget**, not only rejections. A caller that can
discover the limit only by exceeding it has to exceed it -- which is the traffic
spike the limit existed to prevent.
"""

from __future__ import annotations

import math
from typing import Any

from prometheus_client import Counter
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.cache.client import CACHE_OUTAGE_ERRORS, get_client
from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

#: Rejections are counted here rather than left to the HTTP route metrics: this
#: middleware short-circuits, so a rejected request never reaches the route
#: instrumentation that would otherwise have recorded it.
RATE_LIMIT_REJECTED = Counter(
    "app_rate_limit_rejected_total",
    "Requests rejected because the caller exceeded its rate limit.",
)

LIMIT_HEADER = "X-RateLimit-Limit"
REMAINING_HEADER = "X-RateLimit-Remaining"
RESET_HEADER = "X-RateLimit-Reset"
RETRY_AFTER_HEADER = "Retry-After"

#: INCR and the TTL must be one atomic step. As two commands, a process that
#: dies between them leaves a key with no expiry -- and that client is locked
#: out permanently by a bug that only appears under a crash.
_WINDOW_SCRIPT = """
local hits = redis.call('INCR', KEYS[1])
if hits == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return {hits, redis.call('PTTL', KEYS[1])}
"""


class RateLimitMiddleware:
    """Pure-ASGI, so a rejection costs no body read and no route resolution."""

    def __init__(self, app: ASGIApp, config: Settings) -> None:
        self.app = app
        self._config = config
        self._limit = config.rate_limit_requests
        self._window_ms = config.rate_limit_window_seconds * 1000
        self._exempt = config.rate_limit_exempt_paths_set
        self._script: Any = None
        #: The client the cached script is bound to. A ``Script`` holds a
        #: reference to the client that registered it, and the process-wide
        #: Redis client is replaced whenever the pool is disposed and rebuilt --
        #: on shutdown, and between tests. Caching the script without also
        #: tracking its client would keep using the dead one.
        self._script_client: Any = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._config.rate_limit_enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self._exempt:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        client_key = self._client_key(request)
        allowed, remaining, reset_seconds = await self._consume(client_key)

        if not allowed:
            RATE_LIMIT_REJECTED.inc()
            log.warning(
                "request.rate_limited",
                http_path=path,
                http_method=scope.get("method", ""),
                client_key=client_key,
                limit=self._limit,
                window_seconds=self._config.rate_limit_window_seconds,
                retry_after=reset_seconds,
            )
            await self._reject(send, reset_seconds)
            return

        async def send_with_budget(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault(LIMIT_HEADER, str(self._limit))
                headers.setdefault(REMAINING_HEADER, str(remaining))
                headers.setdefault(RESET_HEADER, str(reset_seconds))
            await send(message)

        await self.app(scope, receive, send_with_budget)

    # -- internals ----------------------------------------------------------
    def _client_key(self, request: Request) -> str:
        """Identify the caller.

        ``X-Forwarded-For`` is consulted only when explicitly trusted: with
        nothing stripping the header, a caller can forge a fresh value per
        request and hand itself an unlimited budget. The left-most entry is the
        original client; the rest are proxies.
        """
        if self._config.rate_limit_trust_forwarded_for:
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def _consume(self, client_key: str) -> tuple[bool, int, int]:
        """Count one request. Returns ``(allowed, remaining, reset_seconds)``."""
        redis = get_client()
        if self._script is None or self._script_client is not redis:
            self._script = redis.register_script(_WINDOW_SCRIPT)
            self._script_client = redis

        try:
            hits_raw, ttl_raw = await self._script(
                keys=[f"ratelimit:{client_key}"], args=[self._window_ms]
            )
        except CACHE_OUTAGE_ERRORS as exc:
            # Fail open -- see the module docstring.
            log.warning("ratelimit.unavailable", client_key=client_key, error=str(exc))
            return True, self._limit, self._config.rate_limit_window_seconds
        except Exception as exc:
            # Also fail open, and this breadth is deliberate. `app.core.cache`
            # lets a non-transport error propagate, because a cache sits in the
            # *data* path and a serialisation bug there must not be hidden. This
            # limiter is in the *availability* path: it is the first thing every
            # request touches, so any bug in it that raised would take the whole
            # service down -- a far worse outcome than an uncounted request. It
            # is logged as an error, not a warning, precisely because reaching
            # here means something is wrong that is not merely an outage.
            log.error(
                "ratelimit.failed_open",
                client_key=client_key,
                error=f"{type(exc).__name__}: {exc}",
            )
            return True, self._limit, self._config.rate_limit_window_seconds

        hits, ttl_ms = int(hits_raw), int(ttl_raw)
        # PTTL answers -1 (key has no expiry) or -2 (key is gone) in a race with
        # expiry. Both mean "assume a full window" rather than reporting a
        # negative Retry-After, which a client cannot act on.
        reset_seconds = (
            math.ceil(ttl_ms / 1000) if ttl_ms > 0 else self._config.rate_limit_window_seconds
        )
        return hits <= self._limit, max(0, self._limit - hits), max(1, reset_seconds)

    async def _reject(self, send: Send, reset_seconds: int) -> None:
        """Send the standard error shape as a 429.

        Written directly to the ASGI channel rather than raised. FastAPI's
        exception handlers live *inside* the user middleware stack, so an
        exception raised here would never reach them -- it would surface as a
        500, which is precisely the wrong answer. Same reason, and same
        approach, as ``RequestSizeLimitMiddleware``.

        The imports are local to break an import cycle: ``app.core.errors``
        imports this package's ``security`` module for its header helper.
        """
        from app.core.errors import ErrorResponse
        from app.core.logging import current_request_id, current_trace_id

        body = ErrorResponse(
            error="rate_limited",
            message="Rate limit exceeded. Retry after the window resets.",
            request_id=current_request_id(),
            trace_id=current_trace_id(),
            detail={
                "limit": self._limit,
                "window_seconds": self._config.rate_limit_window_seconds,
                "retry_after_seconds": reset_seconds,
            },
        ).model_dump_json(exclude_none=True)

        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    # A 429 without Retry-After invites an immediate retry --
                    # exactly the behaviour the limit exists to stop.
                    (RETRY_AFTER_HEADER.encode(), str(reset_seconds).encode()),
                    (LIMIT_HEADER.encode(), str(self._limit).encode()),
                    (REMAINING_HEADER.encode(), b"0"),
                    (RESET_HEADER.encode(), str(reset_seconds).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body.encode()})
