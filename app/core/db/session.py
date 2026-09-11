"""Async engine, session factory, and the FastAPI session dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

#Database engine and sessionmaker are lazily created so that importing this module never opens sockets -- unit tests and Alembic can import the models without a live database.
def get_engine() -> AsyncEngine:
    """Lazily create the process-wide engine.

    Lazy so importing this module never opens sockets -- unit tests and Alembic
    can import the models without a live database.
    """
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,#if echo is True, SQLAlchemy logs every statement to the root logger at INFO level. This is useful for debugging, but it is very verbose and should not be enabled in production.
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,#how many extra connections beyond the pool_size can be opened. This is useful for handling spikes in traffic, but it can also lead to resource exhaustion if set too high.
            pool_pre_ping=True,#tests connections before using them, so that a stale connection is not handed to the application. This is useful for long-running applications that may have idle connections in the pool.
            future=True,
        )
    return _engine

#Database sessionmaker is lazily created so that importing this module never opens sockets -- unit tests and Alembic can import the models without a live database.
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session.

    Commits on success, rolls back on any exception, always closes.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


#: Import this alias in routes: ``session: DbSession``.
DbSession = Annotated[AsyncSession, Depends(get_session)]

#check that the database is reachable, for readiness probes. This is not a health check: it does not verify that the schema is correct or that migrations have been applied. It is only a liveness probe for the database connection.
async def ping() -> bool:
    """Readiness probe for Postgres."""
    async with get_engine().connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        return result.scalar_one() == 1

#closes the engine on shutdown, so that tests can run multiple times in the same process without leaking connections. Alembic does not call this, but it does not need to: it opens and closes its own engine for each migration run.
async def dispose_engine() -> None:
    """Close the pool on shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
