"""Application factory.

Everything a request passes through is registered here, in the order it runs.
Business logic lives in ``app/api/`` routers; this module only wires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import health
from app.config import Settings, settings
from app.db import session as db_session


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop long-lived resources (DB pool, Redis, ...)."""
    yield
    await db_session.dispose_engine()


def create_app(config: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    Kept as a factory so tests can build an isolated app with overridden
    settings instead of importing a module-level singleton.
    """
    config = config or settings

    app = FastAPI(
        title=config.app_name,
        version=__version__,
        description="Common Application Base — clone this and add business logic.",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # --- readiness checks ----------------------------------------------------
    # Registered here rather than inside app/api/health.py so the health module
    # never has to know which dependencies this deployment happens to use.
    health.register_readiness_check("postgres", db_session.ping)

    # --- routers -------------------------------------------------------------
    app.include_router(health.router)

    return app


app = create_app()
