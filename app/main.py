"""Application factory.

Everything a request passes through is registered here, in the order it runs.
Business logic lives in ``app/api/`` routers; this module only wires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__, cache, storage
from app.api import demo, files, health
from app.config import Settings, settings
from app.db import session as db_session
from app.errors import configure_sentry, register_exception_handlers
from app.logging import configure_logging, get_logger
from app.middleware.correlation import CorrelationMiddleware
from app.observability import (
    configure_metrics,
    configure_tracing,
    instrument_app,
    instrument_sqlalchemy,
)

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop long-lived resources (DB pool, Redis, ...)."""
    # The engine must exist before it can be instrumented, and it is built
    # lazily -- so this belongs in lifespan, not in create_app().
    instrument_sqlalchemy(db_session.get_engine())
    # Create the bucket if it does not exist, so a fresh stack works on the
    # first upload instead of failing once.
    try:
        await storage.get_storage().ensure_ready()
    except Exception as exc:
        log.warning("storage.ensure_ready.failed", error=str(exc))
    log.info(
        "app.startup",
        version=__version__,
        environment=settings.environment,
        readiness_checks=list(health.registered_checks()),
    )
    yield
    log.info("app.shutdown")
    await db_session.dispose_engine()
    await cache.close_client()


def create_app(config: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    Kept as a factory so tests can build an isolated app with overridden
    settings instead of importing a module-level singleton.
    """
    config = config or settings

    # Logging and tracing come first: everything below may want to log.
    configure_logging()
    configure_sentry(config)
    configure_tracing(config)

    app = FastAPI(
        title=config.app_name,
        version=__version__,
        description="Common Application Base — clone this and add business logic.",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # --- middleware ----------------------------------------------------------
    # Added last => outermost. CorrelationMiddleware must wrap everything so
    # even a failure inside another middleware still carries its request_id.
    app.add_middleware(CorrelationMiddleware)

    # --- error handling ------------------------------------------------------
    register_exception_handlers(app)

    # --- observability -------------------------------------------------------
    configure_metrics(app, config)
    instrument_app(app, config)

    # --- readiness checks ----------------------------------------------------
    # Registered here rather than inside app/api/health.py so the health module
    # never has to know which dependencies this deployment happens to use.
    health.register_readiness_check("postgres", db_session.ping)
    health.register_readiness_check("redis", cache.ping)
    health.register_readiness_check("storage", storage.ping)

    # --- routers -------------------------------------------------------------
    app.include_router(health.router)
    app.include_router(demo.router)
    app.include_router(files.router)

    return app


app = create_app()
