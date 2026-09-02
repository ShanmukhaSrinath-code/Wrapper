"""The outbound client closes the last hole in the Correlation Contract.

Before ``app/core/http`` existed, an inbound ``request_id`` was propagated into
logs, traces, audit rows and Celery messages -- and then dropped the instant this
service called anyone else. The downstream service minted a fresh id, so "quote
one id, get the whole story" ended at the first network hop.

The first test in this file is the one that matters: the id and the W3C trace
context are on the outbound request, and they get there through an ``httpx``
event hook rather than a helper, so no call path can forget them.

The rest pin the failure behaviour: an upstream problem must arrive as a 502,
503 or 504 in the standard error shape -- never as a bare ``httpx`` exception
that becomes a 500 and pages the wrong team.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import UpstreamError, UpstreamTimeoutError, UpstreamUnavailableError
from app.core.http.breaker import CircuitBreaker, CircuitState


def _config(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        # Keep the retry loop fast; the backoff maths is tested on its own.
        "http_retry_backoff_seconds": 0.001,
        "http_max_retries": 2,
        "http_breaker_failure_threshold": 3,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _isolate_client_state() -> Iterator[None]:
    """The client and breaker are process-global; reset both between tests."""
    from app.core.http import client as mod

    mod._client = None
    mod._breaker = None
    yield
    mod._client = None
    mod._breaker = None


def _mock_client(handler: Any) -> httpx.AsyncClient:
    """A client with the real propagation hook and a fake transport."""
    from app.core.http.client import _propagate

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [_propagate]},
    )


def _install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    from app.core.http import client as mod

    monkeypatch.setattr(mod, "_client", _mock_client(handler))


# -- the point of the module --------------------------------------------------
async def test_the_request_id_is_carried_to_the_downstream_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the gap the module exists to close."""
    from app.core.http import request
    from app.core.logging import bind_request_context, clear_request_context

    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(req.headers)
        return httpx.Response(200, json={"ok": True})

    _install(monkeypatch, handler)
    bind_request_context(request_id="known-id-123", trace_id="abc")
    try:
        await request("GET", "https://upstream.test/things", config=_config())
    finally:
        clear_request_context()

    assert seen["x-request-id"] == "known-id-123"


async def test_the_w3c_trace_context_is_propagated(monkeypatch: pytest.MonkeyPatch) -> None:
    """`traceparent` is what makes the downstream spans children of this one.

    ``configure_tracing`` is called explicitly because without a real
    TracerProvider the API hands back a no-op tracer whose span context is
    invalid, and the W3C propagator correctly declines to write a header for an
    invalid span. The application configures the provider in ``create_app``, so
    this only restates in the test what production already guarantees.
    """
    from opentelemetry import trace

    from app.core.http import request
    from app.core.observability import configure_tracing

    configure_tracing(Settings())

    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(req.headers)
        return httpx.Response(200)

    _install(monkeypatch, handler)
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("caller") as caller:
        await request("GET", "https://upstream.test/things", config=_config())
        expected_trace = format(caller.get_span_context().trace_id, "032x")

    assert "traceparent" in seen, "the downstream service cannot join this trace"
    # The header must carry *this* trace, not merely be well-formed.
    assert expected_trace in seen["traceparent"]


async def test_no_bound_id_sends_no_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task published outside a request must not invent a misleading id."""
    from app.core.http import request
    from app.core.logging import clear_request_context

    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(req.headers)
        return httpx.Response(200)

    _install(monkeypatch, handler)
    clear_request_context()
    await request("GET", "https://upstream.test/things", config=_config())

    assert "x-request-id" not in seen


# -- failure translation ------------------------------------------------------
async def test_a_timeout_becomes_a_504_not_a_500(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.http import request

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=req)

    _install(monkeypatch, handler)
    with pytest.raises(UpstreamTimeoutError) as caught:
        await request("GET", "https://upstream.test/things", config=_config())

    assert caught.value.status_code == 504
    assert caught.value.error_code == "upstream_timeout"


async def test_a_transport_failure_becomes_a_502(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.http import request

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    _install(monkeypatch, handler)
    with pytest.raises(UpstreamError) as caught:
        await request("GET", "https://upstream.test/things", config=_config())

    assert caught.value.status_code == 502


async def test_a_4xx_is_returned_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the caller knows whether a 404 from a dependency is an error."""
    from app.core.http import request

    _install(monkeypatch, lambda req: httpx.Response(404, json={"error": "not_found"}))
    response = await request("GET", "https://upstream.test/things", config=_config())

    assert response.status_code == 404


# -- retries ------------------------------------------------------------------
async def test_a_transient_status_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.http import request

    attempts = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    _install(monkeypatch, handler)
    response = await request("GET", "https://upstream.test/things", config=_config())

    assert response.status_code == 200
    assert attempts["n"] == 3


async def test_a_4xx_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrying a rejected request rejects it again, more expensively."""
    from app.core.http import request

    attempts = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400)

    _install(monkeypatch, handler)
    await request("GET", "https://upstream.test/things", config=_config())

    assert attempts["n"] == 1


async def test_retries_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.http import request

    attempts = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503)

    _install(monkeypatch, handler)
    response = await request(
        "GET",
        "https://upstream.test/things",
        config=_config(http_max_retries=2, http_breaker_failure_threshold=99),
    )

    # The final 503 is returned rather than raised: the dependency answered.
    assert response.status_code == 503
    assert attempts["n"] == 3, "max_retries=2 must mean three attempts total"


# -- the breaker, through the client -----------------------------------------
async def test_an_open_circuit_fails_immediately_without_a_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: a dead host stops costing a timeout per caller."""
    from app.core.http import request
    from app.core.http.client import get_breaker

    attempts = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("refused", request=req)

    config = _config(http_max_retries=0, http_breaker_failure_threshold=2)
    _install(monkeypatch, handler)

    for _ in range(2):
        with pytest.raises(UpstreamError):
            await request("GET", "https://dead.test/things", config=config)

    before = attempts["n"]
    with pytest.raises(UpstreamUnavailableError) as caught:
        await request("GET", "https://dead.test/things", config=config)

    assert attempts["n"] == before, "the call was attempted despite an open circuit"
    assert caught.value.status_code == 503
    # A 503 the caller can act on.
    assert "Retry-After" in caught.value.response_headers
    assert get_breaker(config).state("dead.test") is CircuitState.OPEN


async def test_hosts_are_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """One dead dependency must not close the circuit on a healthy one."""
    from app.core.http import request

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "dead.test":
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200)

    config = _config(http_max_retries=0, http_breaker_failure_threshold=2)
    _install(monkeypatch, handler)

    for _ in range(3):
        with pytest.raises(UpstreamError):
            await request("GET", "https://dead.test/x", config=config)

    healthy = await request("GET", "https://healthy.test/x", config=config)
    assert healthy.status_code == 200


# -- the breaker, in isolation -----------------------------------------------
def _breaker_at(now: list[float], **kwargs: Any) -> CircuitBreaker:
    return CircuitBreaker(_clock=lambda: now[0], **kwargs)


def test_the_breaker_opens_only_on_consecutive_failures() -> None:
    """A success in between is evidence the host is alive; the count restarts."""
    now = [0.0]
    breaker = _breaker_at(now, failure_threshold=3)

    breaker.record_failure("h")
    breaker.record_failure("h")
    breaker.record_success("h")
    breaker.record_failure("h")
    breaker.record_failure("h")

    assert breaker.state("h") is CircuitState.CLOSED
    breaker.record_failure("h")
    assert breaker.state("h") is CircuitState.OPEN


def test_the_breaker_half_opens_after_the_reset_window() -> None:
    now = [100.0]
    breaker = _breaker_at(now, failure_threshold=1, reset_seconds=30.0)
    breaker.record_failure("h")
    assert breaker.allows("h") is False

    now[0] = 131.0
    assert breaker.state("h") is CircuitState.HALF_OPEN


def test_only_one_probe_is_admitted_while_half_open() -> None:
    """Otherwise a concurrent burst all hits a host that is probably still down."""
    now = [0.0]
    breaker = _breaker_at(now, failure_threshold=1, reset_seconds=10.0)
    breaker.record_failure("h")
    now[0] = 11.0

    assert breaker.allows("h") is True
    assert breaker.allows("h") is False
    assert breaker.allows("h") is False


def test_a_successful_probe_closes_the_circuit() -> None:
    now = [0.0]
    breaker = _breaker_at(now, failure_threshold=1, reset_seconds=10.0)
    breaker.record_failure("h")
    now[0] = 11.0
    breaker.allows("h")

    breaker.record_success("h")
    assert breaker.state("h") is CircuitState.CLOSED
    assert breaker.allows("h") is True


def test_a_failed_probe_reopens_without_re_counting() -> None:
    """The probe *was* the evidence -- it should not need the threshold again."""
    now = [0.0]
    breaker = _breaker_at(now, failure_threshold=5, reset_seconds=10.0)
    for _ in range(5):
        breaker.record_failure("h")
    now[0] = 11.0
    breaker.allows("h")

    breaker.record_failure("h")
    assert breaker.state("h") is CircuitState.OPEN
    assert breaker.allows("h") is False


def test_retry_after_is_always_actionable() -> None:
    """A Retry-After of 0 or a negative number is not something a client can use."""
    now = [0.0]
    breaker = _breaker_at(now, failure_threshold=1, reset_seconds=30.0)
    breaker.record_failure("h")

    assert breaker.retry_after("h") >= 1
    now[0] = 29.99
    assert breaker.retry_after("h") >= 1


def test_a_disabled_breaker_never_blocks() -> None:
    breaker = CircuitBreaker(failure_threshold=1, enabled=False)
    for _ in range(50):
        breaker.record_failure("h")

    assert breaker.allows("h") is True
    assert breaker.state("h") is CircuitState.CLOSED
