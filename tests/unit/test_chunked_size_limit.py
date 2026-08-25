"""An over-limit body must be rejected as 413, chunked or not.

The stream cap already bounded memory -- that part was never broken. But it
worked by pretending the client had disconnected, so the caller got
`400 "error parsing the body"`: a message that blames the client's syntax for
what is actually a size limit, and gives them nothing to act on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request

from app.core.config import Settings
from app.core.errors import register_exception_handlers
from app.core.middleware.correlation import CorrelationMiddleware
from app.core.middleware.security import RequestSizeLimitMiddleware

LIMIT = 1024


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(
        RequestSizeLimitMiddleware,
        config=Settings(_env_file=None, max_request_body_bytes=LIMIT),
    )
    app.add_middleware(CorrelationMiddleware)

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"received": len(await request.body())}

    return app


async def _chunks(total: int, size: int = 256) -> AsyncIterator[bytes]:
    """Yield a body in pieces, so httpx sends it chunked with no Content-Length."""
    sent = 0
    while sent < total:
        piece = b"x" * min(size, total - sent)
        sent += len(piece)
        yield piece


async def _post(body_bytes: int, *, chunked: bool) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        if chunked:
            return await client.post("/echo", content=_chunks(body_bytes))
        return await client.post("/echo", content=b"x" * body_bytes)


@pytest.mark.asyncio
async def test_chunked_over_limit_is_413_not_400() -> None:
    response = await _post(LIMIT * 4, chunked=True)
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_chunked_rejection_uses_the_standard_error_schema() -> None:
    response = await _post(LIMIT * 4, chunked=True)
    body = response.json()
    assert body["error"] == "payload_too_large"
    assert str(LIMIT) in body["message"]
    assert body["request_id"]


@pytest.mark.asyncio
async def test_declared_over_limit_is_still_413() -> None:
    """The Content-Length path was already correct; keep it that way."""
    response = await _post(LIMIT * 4, chunked=False)
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"


@pytest.mark.asyncio
async def test_a_body_within_the_limit_still_goes_through() -> None:
    response = await _post(LIMIT // 2, chunked=True)
    assert response.status_code == 200
    assert response.json() == {"received": LIMIT // 2}
