"""Shared test fixtures.

Unit tests run against nothing external. Integration and e2e tests run against
the compose stack and are skipped -- loudly, with a reason -- when it is not
up, so a missing stack never masquerades as a passing suite.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8000")
GRAFANA_URL = os.environ.get("TEST_GRAFANA_URL", "http://localhost:3001")
LOKI_URL = os.environ.get("TEST_LOKI_URL", "http://localhost:3100")
PROMETHEUS_URL = os.environ.get("TEST_PROMETHEUS_URL", "http://localhost:9090")


def _stack_is_up() -> bool:
    try:
        return httpx.get(f"{BASE_URL}/health/live", timeout=2.0).status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def stack_up() -> bool:
    return _stack_is_up()


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(autouse=True)
def _require_stack(request: pytest.FixtureRequest) -> None:
    """Skip integration/e2e tests when the compose stack is not running."""
    marks = {m.name for m in request.node.iter_markers()}
    if marks & {"integration", "e2e"} and not _stack_is_up():
        pytest.skip(f"compose stack not reachable at {BASE_URL} - run `make up`")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client against the running stack."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as c:
        yield c


@pytest.fixture
def sync_client() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as c:
        yield c


@pytest.fixture
async def app_client() -> AsyncIterator[httpx.AsyncClient]:
    """Drive the application **in this process**, over an ASGI transport.

    Prefer this over `client` for feature tests: it exercises the same app
    object uvicorn serves, and -- unlike calls over the network to the
    container -- the code it runs is visible to coverage.

    Lives here rather than in one test module so any new feature suite can just
    ask for it.
    """
    # Import lazily: importing app.main builds the app, and unit tests that
    # never touch it should not pay for that.
    from app import cache
    from app.db import session as db_session
    from app.main import app as asgi_app
    from app.storage import get_storage

    # ASGITransport does not run the lifespan hook, so do the one piece of
    # startup that request handling depends on.
    await get_storage().ensure_ready()

    transport = httpx.ASGITransport(app=asgi_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c

    # The engine and Redis client are process singletons bound to the loop that
    # created them, and pytest-asyncio gives each test a fresh loop. Without
    # disposing them the next test dies with "Event loop is closed".
    await db_session.dispose_engine()
    await cache.close_client()
