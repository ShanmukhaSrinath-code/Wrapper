"""One exception must produce exactly one error record with a traceback.

Sentry was de-duplicated long ago; logging was not, so a single bug arrived
three times -- `request.failed`, `request.unhandled_exception` and uvicorn's
`Exception in ASGI application` -- each with a full stack. Three copies of one
event is three times the log bill and a false sense of how often it happened.

The assertions read the **rendered** log stream rather than `caplog`:
`configure_logging()` installs its own root handler, which evicts pytest's
capture handler, so `caplog.records` is empty by the time a request runs.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest


def _records(captured: str) -> list[dict]:
    out = []
    for line in captured.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


@pytest.mark.asyncio
async def test_one_exception_logs_one_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    from app.core.logging import configure_logging
    from app.main import app as asgi_app

    # Configure inside the test so the handler binds to the captured stdout.
    configure_logging()
    request_id = "SINGLE-ERROR-TEST"

    transport = httpx.ASGITransport(app=asgi_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/demo/boom", headers={"X-Request-ID": request_id})
    assert response.status_code == 500

    mine = [r for r in _records(capsys.readouterr().out) if r.get("request_id") == request_id]
    assert mine, "no log lines carried the request id"

    with_traceback = [r for r in mine if "exception" in r]
    assert len(with_traceback) == 1, (
        f"expected one traceback, got {[r.get('event') for r in with_traceback]}"
    )
    assert with_traceback[0]["event"] == "request.unhandled_exception"

    errors = [r for r in mine if r.get("level") == "error"]
    assert len(errors) == 1, f"expected one error record, got {[r.get('event') for r in errors]}"


def test_an_unlogged_exception_keeps_its_traceback() -> None:
    """The de-duplication must only drop tracebacks we have already logged."""
    from app.core.logging import DropAlreadyLoggedTraceback, mark_traceback_logged

    drop = DropAlreadyLoggedTraceback()
    unlogged = RuntimeError("nobody logged me")
    record = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        "f",
        1,
        "Exception in ASGI application",
        None,
        (type(unlogged), unlogged, None),
    )
    assert drop.filter(record) is True, "an unlogged exception must keep its stack"

    mark_traceback_logged(unlogged)
    assert drop.filter(record) is False, "a stack we already logged must be dropped"
