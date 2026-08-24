"""Application factory.

Everything a request passes through is registered here, in the order it runs.
Business logic lives in ``app/api/`` routers; this module only wires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.core import cache, storage
from app.core.api import docs, health
from app.core.config import Settings, settings
from app.core.db import session as db_session
from app.core.discovery import discover_routers, import_discovered_models
from app.core.errors import configure_sentry, register_exception_handlers
from app.core.jobs.celery_app import load_tasks
from app.core.logging import configure_logging, get_logger
from app.core.middleware.correlation import CorrelationMiddleware
from app.core.middleware.security import (
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
    build_cors_kwargs,
)
from app.core.observability import (
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
    # Register every discovered task in *this* process too. The API and the
    # worker run the same discovery, so `enqueue()` can trust its registry
    # check: if the name is missing here, the worker does not have it either.
    task_modules = load_tasks()
    # Same for models -- importing them keeps Base.metadata complete for any
    # code that reflects on it at runtime.
    model_modules = import_discovered_models()
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
        task_modules=task_modules,
        model_modules=model_modules,
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
        # Docs are served by app/api/docs.py instead, so the inline Swagger
        # bootstrap script can be pinned by hash under a strict CSP.
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # --- middleware ----------------------------------------------------------
    # Starlette runs these in REVERSE order of registration, so the last one
    # added is the outermost. Reading bottom-up gives the request path:
    #   CorrelationMiddleware  (ids exist before anything else logs)
    #     -> CORS              (preflight answered without touching the app)
    #       -> SecurityHeaders (stamps every response, errors included)
    #         -> SizeLimit     (reject huge bodies before a route sees them)
    #           -> routes
    app.add_middleware(RequestSizeLimitMiddleware, config=config)
    app.add_middleware(SecurityHeadersMiddleware, config=config)
    app.add_middleware(CORSMiddleware, **build_cors_kwargs(config))
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
    # Infrastructure endpoints are mounted explicitly: they are part of the base,
    # not features, and they must exist even if no plugin does.
    app.include_router(docs.router)
    app.include_router(health.router)

    # Feature routers are discovered. Any module under PLUGIN_PACKAGES exposing a
    # module-level `router` is mounted, so adding an endpoint needs no edit here.
    for module_name, router in discover_routers():
        app.include_router(router)
        log.info("router.mounted", module=module_name, prefix=router.prefix or "/")

    return app


app = create_app()
