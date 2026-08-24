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
