"""One `request_id`, checked against every store that should know it.

This is the Correlation Contract expressed as tests: logs (Loki), audit
(Postgres, via the API's own response), metrics (Prometheus) and the trace id.
`make smoke` proves the same thing end to end; these keep it from regressing.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx
import pytest

from tests.conftest import LOKI_URL, PROMETHEUS_URL

pytestmark = pytest.mark.integration


async def _loki_lines(request_id: str, attempts: int = 15) -> list[dict]:
    """Poll Loki until the request's lines are ingested."""
    start = int((time.time() - 900) * 1e9)
    query = f'{{service="app"}} | json | request_id = `{request_id}`'
    async with httpx.AsyncClient(timeout=15.0) as c:
        for _ in range(attempts):
            r = await c.get(
                f"{LOKI_URL}/loki/api/v1/query_range",
                params={"query": query, "limit": 50, "start": str(start)},
            )
            if r.status_code == 200:
                streams = r.json().get("data", {}).get("result", [])
                if streams:
                    return streams
            await asyncio.sleep(2)
    return []


async def test_request_id_reaches_loki(client: httpx.AsyncClient) -> None:
    marker = f"corr-{uuid.uuid4().hex[:16]}"
    r = await client.post(
        "/demo/audited", params={"note": "correlation-test"}, headers={"X-Request-ID": marker}
    )
    assert r.status_code == 200
    assert r.headers["X-Request-ID"] == marker

    streams = await _loki_lines(marker)
    assert streams, f"no Loki lines found for request_id={marker}"

    ids = {
        stream["stream"].get("request_id")
        for stream in streams
        if stream["stream"].get("request_id")
    }
    assert marker in ids


async def test_audit_row_shares_the_request_and_trace_ids(client: httpx.AsyncClient) -> None:
    marker = f"corr-{uuid.uuid4().hex[:16]}"
    r = await client.post("/demo/audited", headers={"X-Request-ID": marker})
    body = r.json()

    assert body["request_id"] == marker == r.headers["X-Request-ID"]
    assert body["trace_id"] == r.headers["X-Trace-ID"]
    assert body["audit_id"], "no audit row was written"


async def test_prometheus_counts_the_traffic(client: httpx.AsyncClient) -> None:
    for _ in range(5):
        await client.get("/demo/cached", params={"seed": 31337})

    async with httpx.AsyncClient(timeout=15.0) as c:
        for _ in range(15):
            r = await c.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": 'sum(app_http_requests_total{handler="/demo/cached"})'},
            )
            result = r.json().get("data", {}).get("result", [])
            if result and float(result[0]["value"][1]) > 0:
                return
            await asyncio.sleep(2)
    pytest.fail("Prometheus never reported traffic for /demo/cached")


async def test_prometheus_target_is_up() -> None:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(
            f"{PROMETHEUS_URL}/api/v1/query", params={"query": 'up{job="common-app-base"}'}
        )
    result = r.json()["data"]["result"]
    assert result, "the app is not a Prometheus target"
    assert result[0]["value"][1] == "1"
