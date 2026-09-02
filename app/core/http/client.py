"""The outbound HTTP client -- correlation, timeouts, retries, breaker.

Until this module existed the Correlation Contract stopped at this service's
edge. An inbound ``X-Request-ID`` was accepted, propagated into logs, traces,
audit rows and Celery messages -- and then dropped the moment the code called
someone else. The downstream service minted a brand new id, and the one thing
the whole base is for ("quote one id, get the whole story") ended at the first
network hop.

Every outbound request made through :func:`get_client` carries:

* ``X-Request-ID`` -- this request's id, so the downstream service adopts it
  rather than inventing one (its correlation middleware already validates and
  accepts an inbound value);
* ``traceparent`` -- the W3C trace context, so the downstream service's spans
  become children of this one and a single Grafana waterfall spans both;
* a **span** of its own, named ``HTTP <method>``, following the OpenTelemetry
  semantic conventions.

It also refuses to make an unbounded call. A timeout is applied whether or not
the caller remembered one, transient failures are retried with jitter, and a
:class:`~app.core.http.breaker.CircuitBreaker` turns a dead host into an
immediate 503 instead of a queue of waiting workers.

Usage from a feature::

    from app.core.http import get_client

    response = await get_client().get("https://api.example.com/things")

Do not construct ``httpx.AsyncClient`` directly in a feature: it would work,
and it would silently drop the correlation ids.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any
from urllib.parse import urlsplit

import httpx
from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import SpanKind, Status, StatusCode

from app.core.config import Settings, settings
from app.core.errors import UpstreamError, UpstreamTimeoutError, UpstreamUnavailableError
from app.core.http.breaker import CircuitBreaker
from app.core.logging import current_request_id, get_logger

log = get_logger(__name__)
_tracer = trace.get_tracer(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

#: Failures worth another attempt: the request never got a considered answer.
#: A 4xx is deliberately absent -- retrying a rejected request just rejects it
#: again, more expensively.
_RETRY_STATUS = frozenset({429, 502, 503, 504})
_TRANSPORT_ERRORS = (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.PoolTimeout)

_client: httpx.AsyncClient | None = None
_breaker: CircuitBreaker | None = None


def _host_of(url: httpx.URL | str) -> str:
    """Host key for the breaker. Port included: two ports are two dependencies."""
    raw = str(url)
    parts = urlsplit(raw)
    return parts.netloc or raw


def get_breaker(config: Settings | None = None) -> CircuitBreaker:
    """The process-wide breaker. Shared so every caller sees one host's state."""
    global _breaker
    cfg = config or settings
    if _breaker is None:
        _breaker = CircuitBreaker(
            failure_threshold=cfg.http_breaker_failure_threshold,
            reset_seconds=cfg.http_breaker_reset_seconds,
            enabled=cfg.http_breaker_enabled,
        )
    return _breaker


async def _propagate(request: httpx.Request) -> None:
    """Inject correlation into every outbound request.

    An ``httpx`` event hook rather than a wrapper function, so the ids are
    attached even when a caller reaches for ``client.stream()`` or builds a
    request by hand -- there is no code path through this client that can forget.
    """
    request_id = current_request_id()
    if request_id:
        request.headers[REQUEST_ID_HEADER] = request_id
    # W3C trace context. `inject` writes `traceparent` (and `tracestate`) from
    # the *currently active* span, which inside a route is the span this module
    # opens below -- so the downstream service parents onto the actual call.
    inject(request.headers)


def get_client(config: Settings | None = None) -> httpx.AsyncClient:
    """The process-wide client. Created lazily, closed in the app lifespan.

    One client, not one per call: ``httpx`` pools connections, and a client per
    call means a fresh TCP handshake (and TLS negotiation) every time.
    """
    global _client
    cfg = config or settings
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                cfg.http_read_timeout_seconds,
                connect=cfg.http_connect_timeout_seconds,
            ),
            limits=httpx.Limits(max_connections=cfg.http_max_connections),
            event_hooks={"request": [_propagate]},
            # Redirects are not followed by default: a silent redirect can move
            # a request to a host the breaker is not tracking, and can replay a
            # body somewhere the caller never named.
            follow_redirects=False,
        )
    return _client


async def close_client() -> None:
    """Close the pool. Called from the application lifespan."""
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def request(
    method: str,
    url: str,
    *,
    config: Settings | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Make a traced, correlated, retried, breaker-guarded outbound call.

    Raises :class:`UpstreamUnavailableError` if the circuit is open,
    :class:`UpstreamTimeoutError` on timeout, and :class:`UpstreamError` on a
    transport failure -- so an upstream problem reaches the caller as a 502/503/
    504 in the standard error shape rather than as a bare ``httpx`` exception
    that becomes a 500 and pages the wrong team.

    Note what is *not* translated: a 4xx or 5xx response is returned as-is. The
    call succeeded in reaching the dependency, and only the caller knows whether
    a 404 from it is an error or an answer.
    """
    cfg = config or settings
    client = get_client(cfg)
    breaker = get_breaker(cfg)
    host = _host_of(url)

    if not breaker.allows(host):
        retry_after = breaker.retry_after(host)
        log.warning("http.circuit_open", upstream_host=host, retry_after=retry_after)
        raise UpstreamUnavailableError(
            f"Upstream {host} is unavailable (circuit open).",
            detail={"upstream_host": host, "retry_after_seconds": retry_after},
            response_headers={"Retry-After": str(retry_after)},
        )

    attempts = max(1, cfg.http_max_retries + 1)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        with _tracer.start_as_current_span(f"HTTP {method.upper()}", kind=SpanKind.CLIENT) as span:
            span.set_attribute("http.request.method", method.upper())
            span.set_attribute("url.full", str(url))
            span.set_attribute("server.address", host)
            if attempt > 1:
                span.set_attribute("http.request.resend_count", attempt - 1)

            try:
                response = await client.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                last_error = exc
                span.set_status(Status(StatusCode.ERROR, "timeout"))
                breaker.record_failure(host)
                if attempt == attempts:
                    log.warning(
                        "http.timeout", upstream_host=host, attempts=attempt, error=str(exc)
                    )
                    raise UpstreamTimeoutError(
                        f"Upstream {host} did not respond in time.",
                        detail={"upstream_host": host, "attempts": attempt},
                    ) from exc
            except _TRANSPORT_ERRORS as exc:
                last_error = exc
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                breaker.record_failure(host)
                if attempt == attempts:
                    log.warning(
                        "http.transport_error",
                        upstream_host=host,
                        attempts=attempt,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    raise UpstreamError(
                        f"Could not reach upstream {host}.",
                        detail={"upstream_host": host, "attempts": attempt},
                    ) from exc
            else:
                span.set_attribute("http.response.status_code", response.status_code)
                if response.status_code in _RETRY_STATUS and attempt < attempts:
                    # A retryable status is a failure for the breaker's purposes:
                    # a host answering 503 to everything is exactly the case the
                    # breaker exists to stop hammering.
                    breaker.record_failure(host)
                    span.set_status(Status(StatusCode.ERROR, f"HTTP {response.status_code}"))
                    await _backoff(cfg, attempt, response)
                    continue
                if response.status_code >= 500:
                    breaker.record_failure(host)
                    span.set_status(Status(StatusCode.ERROR, f"HTTP {response.status_code}"))
                else:
                    breaker.record_success(host)
                return response

        await _backoff(cfg, attempt, None)

    # Unreachable: the final attempt either returns or raises above. Kept so a
    # future edit to the loop cannot silently return None.
    raise UpstreamError(f"Could not reach upstream {host}.") from last_error


async def _backoff(config: Settings, attempt: int, response: httpx.Response | None) -> None:
    """Exponential backoff with jitter, honouring ``Retry-After`` when given.

    Jitter matters more than the delay: without it every caller that failed
    during an outage retries in lockstep the moment it ends and knocks the
    dependency over again. Same reasoning as the Celery retry policy.
    """
    if response is not None:
        header = response.headers.get("Retry-After", "")
        if header.isdigit():
            await asyncio.sleep(min(float(header), config.http_read_timeout_seconds))
            return
    delay = config.http_retry_backoff_seconds * (2 ** (attempt - 1))
    await asyncio.sleep(delay * (0.5 + random.random()))  # noqa: S311 - jitter, not crypto


async def get(url: str, **kwargs: Any) -> httpx.Response:
    return await request("GET", url, **kwargs)


async def post(url: str, **kwargs: Any) -> httpx.Response:
    return await request("POST", url, **kwargs)
