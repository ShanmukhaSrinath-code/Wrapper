"""structlog configuration and the request-scoped logging context.

Every log line the application emits is JSON and carries the correlation ids
bound for the current request, because the ids live in ``contextvars`` that
structlog merges automatically. Nothing has to pass a logger around.

See the Correlation Contract in README.md.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from typing import Any

import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    merge_contextvars,
)

from app.core.config import settings

#: Keys bound per request and merged into every log line.
REQUEST_ID_KEY = "request_id"
TRACE_ID_KEY = "trace_id"
SPAN_ID_KEY = "span_id"


def _add_service_context(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Stamp every line with the service identity, so Loki can label by it."""
    event_dict.setdefault("service", settings.app_name)
    event_dict.setdefault("environment", settings.environment)
    return event_dict


def configure_logging() -> None:
    """Install structlog + route stdlib logging through it.

    Called once at startup. Uvicorn's and SQLAlchemy's stdlib loggers are
    routed through the same pipeline so *their* lines carry the correlation ids
    too -- otherwise an access log could not be joined to an application log.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        merge_contextvars,  # <- pulls request_id / trace_id into every line
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _add_service_context,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    # structlog does NOT render here. It hands the event dict to the stdlib
    # logger, and ProcessorFormatter below performs the single final render.
    # Rendering in both places is what produces JSON nested inside "event".
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # --- route stdlib logging (uvicorn, sqlalchemy, celery) through structlog -
    # `foreign_pre_chain` gives records from stdlib loggers the same treatment,
    # so a uvicorn line carries the correlation ids exactly like ours does.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )

    handler.addFilter(DropAlreadyLoggedTraceback())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine", "celery"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True

    # uvicorn.access duplicates what our own middleware logs, with none of the
    # correlation ids -- silence it and keep the structured line instead.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Traceback de-duplication
# --------------------------------------------------------------------------
#: Set on an exception object once its traceback has been logged with the
#: correlation ids. Layers above us -- Starlette's ServerErrorMiddleware
#: re-raises, and uvicorn then logs "Exception in ASGI application" -- would
#: otherwise print the same stack again, with no ids, in a different format.
_LOGGED_FLAG = "_cab_traceback_logged"


def mark_traceback_logged(exc: BaseException) -> None:
    """Record that this exception's stack has already been logged, once."""
    # Some exception types forbid attribute assignment; missing the flag only
    # costs a duplicate traceback, so it is not worth failing the request over.
    with contextlib.suppress(Exception):
        object.__setattr__(exc, _LOGGED_FLAG, True)


def traceback_already_logged(exc: BaseException) -> bool:
    return bool(getattr(exc, _LOGGED_FLAG, False))


class DropAlreadyLoggedTraceback(logging.Filter):
    """Drop records repeating a traceback we have already emitted.

    Deliberately keyed on the **exception object**, not on the logger name or
    the message: an exception nobody logged still gets its stack printed, which
    is what makes this safe. Losing the only copy of a traceback would be a far
    worse bug than printing it twice.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (exc is not None and traceback_already_logged(exc))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Prefer module-level ``log = get_logger(__name__)``."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]


# --------------------------------------------------------------------------
# request-scoped context
# --------------------------------------------------------------------------
def bind_request_context(
    *,
    request_id: str,
    trace_id: str | None = None,
    span_id: str | None = None,
    **extra: Any,
) -> None:
    """Bind correlation ids for the current task/request."""
    values: dict[str, Any] = {REQUEST_ID_KEY: request_id, **extra}
    if trace_id:
        values[TRACE_ID_KEY] = trace_id
    if span_id:
        values[SPAN_ID_KEY] = span_id
    bind_contextvars(**values)


def clear_request_context() -> None:
    """Drop the bound context so ids never leak between requests."""
    clear_contextvars()


def current_request_id() -> str | None:
    """The request id bound to the current context, if any."""
    return structlog.contextvars.get_contextvars().get(REQUEST_ID_KEY)


def current_trace_id() -> str | None:
    """The trace id bound to the current context, if any."""
    return structlog.contextvars.get_contextvars().get(TRACE_ID_KEY)
