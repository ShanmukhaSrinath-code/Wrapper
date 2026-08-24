"""The Correlation Contract, implemented as one middleware.

For every request this middleware:

1. accepts an inbound ``X-Request-ID`` or mints a UUID4;
2. opens an OpenTelemetry span, so a ``trace_id``/``span_id`` exist;
3. binds ``request_id`` + ``trace_id`` + ``span_id`` into structlog's
   contextvars, so **every** log line for the request carries them;
4. tags the Sentry scope with the same ids;
5. echoes ``X-Request-ID`` (and ``X-Trace-ID``) back on the response;
6. emits one structured access log line with method, path, status, duration.

Everything downstream -- audit rows, Celery tasks, error responses -- reads the
ids back out of the context rather than being handed them, so a new component
is correlated by default.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from opentelemetry import trace
from starlette.requests import Request
from starlette.types import ASGIApp

from app.logging import bind_request_context, clear_request_context, get_logger

REQUEST_ID_HEADER = "X-Request-ID"
TRACE_ID_HEADER = "X-Trace-ID"

log = get_logger(__name__)
_tracer = trace.get_tracer(__name__)

#: Paths whose access logs are noise -- probes and scrapes fire constantly.
_QUIET_PATHS = frozenset({"/health/live", "/health/ready", "/metrics"})


def _is_valid_request_id(value: str) -> bool:
    """Accept an inbound id only if it is sane.

    An unvalidated client header becomes a log-injection and cardinality
    vector, so cap the length and the alphabet.
    """
    return 8 <= len(value) <= 128 and all(c.isalnum() or c in "-_" for c in value)


class CorrelationMiddleware:
    """Pure-ASGI middleware so it can wrap *everything*, exception handlers included."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:  # type: ignore[type-arg]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _is_valid_request_id(incoming) else str(uuid.uuid4())

        path = scope.get("path", "")
        method = scope.get("method", "")

        clear_request_context()
        started = time.perf_counter()
        status_code = 500

        with _tracer.start_as_current_span(f"{method} {path}") as span:
            ctx = span.get_span_context()
            trace_id = format(ctx.trace_id, "032x") if ctx.trace_id else None
            span_id = format(ctx.span_id, "016x") if ctx.span_id else None

            bind_request_context(request_id=request_id, trace_id=trace_id, span_id=span_id)
            span.set_attribute("request.id", request_id)
            _tag_sentry(request_id, trace_id)

            # The route handler needs the id for error payloads and audit rows.
            scope.setdefault("state", {})
            scope["state"]["request_id"] = request_id
            scope["state"]["trace_id"] = trace_id

            async def send_with_headers(message: dict) -> None:  # type: ignore[type-arg]
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = message["status"]
                    headers = message.setdefault("headers", [])
                    headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
                    if trace_id:
                        headers.append((TRACE_ID_HEADER.lower().encode(), trace_id.encode()))
                await send(message)

            try:
                await self.app(scope, receive, send_with_headers)
            except Exception:
                # Log here so the failure carries its ids even if the exception
                # escapes every handler, then let it propagate.
                log.exception(
                    "request.failed",
                    http_method=method,
                    http_path=path,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                # Deliberately do NOT clear the context here. Starlette's
                # ServerErrorMiddleware sits *outside* this middleware, so the
                # 500 handler runs after this `raise` -- clearing now would
                # strip request_id from the very error response that needs it.
                # The next request clears the context on entry anyway.
                raise
            finally:
                if path not in _QUIET_PATHS:
                    log.info(
                        "request.completed",
                        http_method=method,
                        http_path=path,
                        http_status=status_code,
                        duration_ms=round((time.perf_counter() - started) * 1000, 2),
                        client_ip=request.client.host if request.client else None,
                    )

        clear_request_context()


def _tag_sentry(request_id: str, trace_id: str | None) -> None:
    """Tag the current Sentry scope, if the SDK is installed and initialised."""
    try:
        import sentry_sdk

        if sentry_sdk.get_client().is_active():
            scope = sentry_sdk.get_current_scope()
            scope.set_tag("request_id", request_id)
            if trace_id:
                scope.set_tag("trace_id", trace_id)
    except Exception as exc:
        log.debug("sentry.tag.failed", error=str(exc))


def get_request_id(request: Request) -> str | None:
    """Read the request id back out of the ASGI scope."""
    return request.scope.get("state", {}).get("request_id")
