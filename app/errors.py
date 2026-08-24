"""Global exception handling and the single error response schema.

Every failure leaving this service looks the same to a caller::

    {"error": "not_found", "message": "...", "request_id": "..."}

and always carries the `request_id`, so a user can quote one id and an
engineer can pull the logs, the trace, the audit row and the Sentry event for
it. Internal detail (stack traces, driver messages) never crosses the boundary.
"""

from __future__ import annotations

from typing import Any

import sentry_sdk
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings
from app.logging import current_request_id, current_trace_id, get_logger
from app.middleware.security import apply_security_headers

log = get_logger(__name__)


class ErrorResponse(BaseModel):
    """The one error shape this service returns."""

    error: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable summary. Safe to show a user.")
    request_id: str | None = Field(default=None, description="Correlates logs, trace and audit.")
    trace_id: str | None = Field(default=None, description="OpenTelemetry trace id.")
    detail: Any | None = Field(default=None, description="Structured detail, e.g. field errors.")


class AppError(Exception):
    """Base class for expected, business-level failures.

    Raise a subclass when the caller did something the domain rejects. Anything
    else that escapes is treated as a bug and reported as `internal_error`.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    error_code: str = "app_error"

    def __init__(self, message: str, *, detail: Any | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_code = "not_found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    error_code = "conflict"


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "validation_error"


class PermissionDeniedError(AppError):
    """Raised once real authorization replaces the stub principal."""

    status_code = status.HTTP_403_FORBIDDEN
    error_code = "permission_denied"


class PayloadTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    error_code = "payload_too_large"


def _render(
    status_code: int,
    error: str,
    message: str,
    detail: Any | None = None,
    request: Request | None = None,
) -> JSONResponse:
    """Build the error response, stamping the correlation ids and security headers."""
    request_id = current_request_id()
    payload = ErrorResponse(
        error=error,
        message=message,
        request_id=request_id,
        trace_id=current_trace_id(),
        detail=detail,
    )
    response = JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(payload, exclude_none=True),
    )
    # Also echo it here: the correlation middleware sets this too, but an error
    # raised before it runs would otherwise lose the id.
    if request_id:
        response.headers["X-Request-ID"] = request_id

    # Starlette's ServerErrorMiddleware is the OUTERMOST layer, so a 500 it
    # generates never passes back through SecurityHeadersMiddleware. Stamp the
    # headers here as well, or error responses would ship without them.
    apply_security_headers(
        MutableHeaders(raw=response.raw_headers),
        is_https=bool(request and request.url.scheme == "https"),
    )
    return response


def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers for every class of failure. Order matters: most specific first."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        # Expected failures are warnings, not errors, and are not sent to Sentry:
        # a 404 is not a bug, and paging on it trains people to ignore alerts.
        log.warning(
            "request.app_error",
            error_code=exc.error_code,
            http_status=exc.status_code,
            http_path=request.url.path,
            message=exc.message,
        )
        return _render(exc.status_code, exc.error_code, exc.message, exc.detail, request)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        log.warning(
            "request.validation_error",
            http_path=request.url.path,
            error_count=len(exc.errors()),
        )
        return _render(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_error",
            "The request body or parameters failed validation.",
            detail=exc.errors(),
            request=request,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: "unauthorized",
            403: "permission_denied",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
            413: "payload_too_large",
            429: "rate_limited",
        }.get(exc.status_code, "http_error")
        if exc.status_code >= 500:
            log.error("request.http_error", http_status=exc.status_code, http_path=request.url.path)
        else:
            log.info("request.http_error", http_status=exc.status_code, http_path=request.url.path)
        return _render(exc.status_code, code, str(exc.detail), request=request)

    @app.exception_handler(SQLAlchemyError)
    async def _db(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        # Driver messages can leak schema and even data -- log them, never return them.
        log.exception("request.database_error", http_path=request.url.path)
        sentry_sdk.capture_exception(exc)
        return _render(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "database_error",
            "A database error occurred. The request was not completed.",
            request=request,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception(
            "request.unhandled_exception",
            http_path=request.url.path,
            http_method=request.method,
            exception_type=type(exc).__name__,
        )
        sentry_sdk.capture_exception(exc)
        return _render(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An unexpected error occurred. Quote the request_id when reporting this.",
            request=request,
        )


# ---------------------------------------------------------------------------
# Sentry
# ---------------------------------------------------------------------------
def configure_sentry(config: Settings) -> None:
    """Initialise Sentry, or do nothing at all if no DSN is configured.

    A no-op init matters: the same code must run in tests and locally without a
    DSN, and `sentry_sdk.capture_exception` is safe to call on an uninitialised
    client.
    """
    if not config.sentry_dsn:
        log.info("sentry.disabled", reason="SENTRY_DSN is not set")
        return

    import logging

    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=config.sentry_dsn,
        environment=config.environment,
        traces_sample_rate=config.sentry_traces_sample_rate,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
            SqlalchemyIntegration(),
            # `event_level=None` => log.error/log.exception become breadcrumbs,
            # not separate events. Without this one failure is reported three
            # times (our capture_exception + the handler log + the ASGI log),
            # tripling quota and alert noise for a single bug.
            LoggingIntegration(level=logging.INFO, event_level=None),
        ],
        # Bodies and headers can contain secrets; opt in deliberately if needed.
        send_default_pii=False,
        max_request_body_size="never",
    )
    sentry_sdk.set_tag("service", config.app_name)
    log.info("sentry.enabled", environment=config.environment)
