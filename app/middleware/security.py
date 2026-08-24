"""Security headers and request-size limiting.

Headers follow the OWASP Secure Headers Project's baseline. Two choices worth
calling out:

* **HSTS is only sent over HTTPS.** Sending it on a plain-HTTP local response
  is meaningless at best and, if a browser ever honours it for `localhost`,
  actively obstructive.
* **The body-size limit is enforced from `Content-Length` *and* while
  streaming.** Trusting the header alone lets a chunked request without one
  send unbounded data.
"""

from __future__ import annotations

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.logging import get_logger

log = get_logger(__name__)

#: Sent on every response. Values are static; the CSP is deliberately strict
#: because this service returns JSON, not HTML -- /docs is the one exception.
BASE_SECURITY_HEADERS: dict[str, str] = {
    # Never let a browser second-guess a declared content type.
    "X-Content-Type-Options": "nosniff",
    # Legacy clickjacking defence; CSP frame-ancestors is the modern one.
    "X-Frame-Options": "DENY",
    # Do not leak URLs (which may contain ids) to third parties.
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Turn off powerful browser features this API never uses.
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
    # Do not advertise the stack.
    "X-Permitted-Cross-Domain-Policies": "none",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}

#: A JSON API needs nothing loadable at all.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

#: Swagger UI and ReDoc pull scripts/styles from a CDN and use inline styles,
#: so those two paths get a narrower policy rather than a broken page.
DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "font-src 'self' https://cdn.jsdelivr.net; "
    "worker-src 'self' blob:; "
    "frame-ancestors 'none'; base-uri 'none'"
)

_DOCS_PATHS = frozenset({"/docs", "/redoc", "/docs/oauth2-redirect"})


def apply_security_headers(
    headers: MutableHeaders,
    *,
    is_https: bool,
    is_docs: bool = False,
    hsts_max_age: int = 31_536_000,
) -> None:
    """Stamp the baseline headers onto ``headers`` without overwriting any set.

    Shared by :class:`SecurityHeadersMiddleware` and by the error renderer in
    ``app.errors``. Error responses need this second path because Starlette's
    ``ServerErrorMiddleware`` sits *outside* all user middleware, so a 500 it
    generates never passes back through the middleware chain.
    """
    for name, value in BASE_SECURITY_HEADERS.items():
        headers.setdefault(name, value)
    headers.setdefault("Content-Security-Policy", DOCS_CSP if is_docs else API_CSP)
    if is_https:
        headers.setdefault(
            "Strict-Transport-Security", f"max-age={hsts_max_age}; includeSubDomains"
        )


class SecurityHeadersMiddleware:
    """Attach the OWASP baseline headers to every response."""

    def __init__(self, app: ASGIApp, config: Settings) -> None:
        self.app = app
        self.hsts_max_age = config.hsts_max_age_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        is_docs = path in _DOCS_PATHS

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                apply_security_headers(
                    MutableHeaders(scope=message),
                    # HSTS is only meaningful over TLS.
                    is_https=scope.get("scheme") == "https",
                    is_docs=is_docs,
                    hsts_max_age=self.hsts_max_age,
                )
                # NOTE: the `Server: uvicorn` banner cannot be removed here --
                # uvicorn appends it *after* middleware, in its protocol layer.
                # It is suppressed with `--no-server-header` on the command line
                # (see deploy/docker/Dockerfile and the Makefile `run` target).
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestSizeLimitMiddleware:
    """Reject bodies larger than the configured limit."""

    def __init__(self, app: ASGIApp, config: Settings) -> None:
        self.app = app
        self.max_bytes = config.max_request_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(send, int(declared))
            return

        received = 0
        too_large = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # A chunked request can omit Content-Length entirely, so the
                    # stream itself has to be capped as well.
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, send)
        if too_large:
            log.warning(
                "request.body_too_large",
                received_bytes=received,
                limit_bytes=self.max_bytes,
                http_path=scope.get("path"),
            )

    async def _reject(self, send: Send, declared: int) -> None:
        from app.errors import ErrorResponse
        from app.logging import current_request_id, current_trace_id

        log.warning(
            "request.body_too_large",
            declared_bytes=declared,
            limit_bytes=self.max_bytes,
        )
        body = ErrorResponse(
            error="payload_too_large",
            message=f"Request body exceeds the {self.max_bytes} byte limit.",
            request_id=current_request_id(),
            trace_id=current_trace_id(),
        ).model_dump_json(exclude_none=True)

        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body.encode()})


def build_cors_kwargs(config: Settings) -> dict[str, object]:
    """CORS settings, with the one combination browsers reject ruled out.

    `allow_origins=["*"]` together with `allow_credentials=True` is rejected by
    every browser, and silently produces a CORS setup that never works. Fail
    loudly at startup instead.
    """
    origins = config.cors_origins_list
    allow_credentials = config.cors_allow_credentials

    if allow_credentials and "*" in origins:
        raise ValueError(
            "CORS_ALLOW_CREDENTIALS=true requires explicit CORS_ALLOW_ORIGINS "
            "(browsers reject credentialed requests against a wildcard origin)."
        )

    return {
        "allow_origins": origins,
        "allow_credentials": allow_credentials,
        "allow_methods": config.cors_methods_list,
        "allow_headers": config.cors_headers_list,
        "expose_headers": ["X-Request-ID", "X-Trace-ID"],
        "max_age": 600,
    }


__all__ = [
    "BASE_SECURITY_HEADERS",
    "RequestSizeLimitMiddleware",
    "SecurityHeadersMiddleware",
    "apply_security_headers",
    "build_cors_kwargs",
]
