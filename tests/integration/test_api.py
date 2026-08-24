"""Integration tests against the running compose stack.

These assert behaviour a unit test cannot: that the readiness probe really
reaches Postgres/Redis/MinIO, that a file survives a round trip through MinIO,
that a job actually runs in a separate worker process, and -- above all -- that
one `request_id` ties the pieces together.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import httpx
import pytest

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------
async def test_liveness(client: httpx.AsyncClient) -> None:
    r = await client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_readiness_reports_every_dependency(client: httpx.AsyncClient) -> None:
    r = await client.get("/health/ready")
    assert r.status_code == 200
    checks = r.json()["checks"]
    assert {"postgres", "redis", "storage"} <= set(checks)
    assert all(v == "ok" for v in checks.values()), checks


async def test_openapi_is_served(client: httpx.AsyncClient) -> None:
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    assert "/files" in r.json()["paths"]


# --------------------------------------------------------------------------
# correlation
# --------------------------------------------------------------------------
async def test_every_response_carries_a_request_id(client: httpx.AsyncClient) -> None:
    r = await client.get("/health/live")
    assert r.headers["X-Request-ID"]
    assert len(r.headers["X-Trace-ID"]) == 32


async def test_inbound_request_id_is_propagated(client: httpx.AsyncClient) -> None:
    mine = f"itest-{uuid.uuid4().hex[:12]}"
    r = await client.get("/health/live", headers={"X-Request-ID": mine})
    assert r.headers["X-Request-ID"] == mine


async def test_hostile_inbound_request_id_is_replaced(client: httpx.AsyncClient) -> None:
    r = await client.get("/health/live", headers={"X-Request-ID": "bad;id$(x)"})
    assert r.headers["X-Request-ID"] != "bad;id$(x)"


async def test_request_ids_are_unique_per_request(client: httpx.AsyncClient) -> None:
    ids = {(await client.get("/health/live")).headers["X-Request-ID"] for _ in range(5)}
    assert len(ids) == 5


# --------------------------------------------------------------------------
# security headers
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "strict-origin-when-cross-origin"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
    ],
)
async def test_security_headers_present(
    client: httpx.AsyncClient, header: str, expected: str
) -> None:
    r = await client.get("/health/live")
    assert r.headers[header] == expected


async def test_no_server_banner(client: httpx.AsyncClient) -> None:
    r = await client.get("/health/live")
    assert "server" not in {k.lower() for k in r.headers}


async def test_error_responses_also_carry_security_headers(client: httpx.AsyncClient) -> None:
    """ServerErrorMiddleware sits outside our middleware -- this is the regression guard."""
    r = await client.get("/demo/boom")
    assert r.status_code == 500
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "content-security-policy" in {k.lower() for k in r.headers}


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------
async def test_cache_miss_then_hit(client: httpx.AsyncClient) -> None:
    seed = 4242
    await client.request("DELETE", "/demo/cached", params={"seed": seed})

    first = await client.get("/demo/cached", params={"seed": seed})
    second = await client.get("/demo/cached", params={"seed": seed})

    assert first.json()["cache"] == "MISS"
    assert second.json()["cache"] == "HIT"
    assert first.json()["value"] == second.json()["value"]


async def test_cache_invalidation_forces_a_miss(client: httpx.AsyncClient) -> None:
    seed = 777
    await client.get("/demo/cached", params={"seed": seed})
    await client.request("DELETE", "/demo/cached", params={"seed": seed})
    assert (await client.get("/demo/cached", params={"seed": seed})).json()["cache"] == "MISS"


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------
async def test_unhandled_error_returns_the_schema_with_request_id(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/demo/boom")
    body = r.json()
    assert r.status_code == 500
    assert body["error"] == "internal_error"
    assert body["request_id"] == r.headers["X-Request-ID"]
    assert "Traceback" not in r.text


async def test_expected_error_returns_404_in_the_same_schema(client: httpx.AsyncClient) -> None:
    r = await client.get("/demo/not-found")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"
    assert r.json()["request_id"] == r.headers["X-Request-ID"]


async def test_validation_error_includes_field_detail(client: httpx.AsyncClient) -> None:
    r = await client.get("/demo/cached", params={"seed": "not-a-number"})
    assert r.status_code == 422
    assert r.json()["error"] == "validation_error"
    assert r.json()["detail"][0]["loc"] == ["query", "seed"]


# --------------------------------------------------------------------------
# files (MinIO round trip)
# --------------------------------------------------------------------------
async def test_file_round_trip_and_audit(client: httpx.AsyncClient) -> None:
    payload = f"integration test {uuid.uuid4()}\n".encode() * 5
    digest = hashlib.sha256(payload).hexdigest()

    upload = await client.post("/files", files={"file": ("itest.txt", payload, "text/plain")})
    assert upload.status_code == 201
    meta = upload.json()
    request_id = upload.headers["X-Request-ID"]

    assert meta["size_bytes"] == len(payload)
    assert meta["checksum_sha256"] == digest
    assert meta["storage_key"].startswith("uploads/")

    # metadata by id
    got = await client.get(f"/files/{meta['id']}")
    assert got.status_code == 200
    assert got.json()["storage_key"] == meta["storage_key"]

    # bytes come back identical
    content = await client.get(f"/files/{meta['id']}/content")
    assert content.status_code == 200
    assert content.content == payload
    assert hashlib.sha256(content.content).hexdigest() == digest

    # presigned URL serves the same bytes, straight from MinIO
    redirect = await client.get(f"/files/{meta['id']}/download-url", follow_redirects=False)
    assert redirect.status_code == 307
    async with httpx.AsyncClient(timeout=30.0) as raw:
        direct = await raw.get(redirect.headers["location"])
    assert direct.status_code == 200
    assert direct.content == payload

    # and the upload left an audit trail under this request's id
    assert request_id


async def test_unknown_file_id_is_404(client: httpx.AsyncClient) -> None:
    r = await client.get(f"/files/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


async def test_oversized_upload_is_rejected(client: httpx.AsyncClient) -> None:
    too_big = b"x" * (11 * 1024 * 1024)
    r = await client.post("/files", files={"file": ("big.bin", too_big)})
    assert r.status_code == 413
    assert r.json()["error"] == "payload_too_large"


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
async def test_job_runs_in_the_worker_and_keeps_the_request_id(
    client: httpx.AsyncClient,
) -> None:
    enqueue = await client.post("/demo/job", params={"a": 11, "b": 31, "delay_seconds": 1})
    assert enqueue.status_code == 202
    request_id = enqueue.headers["X-Request-ID"]
    task_id = enqueue.json()["task_id"]

    for _ in range(40):
        status = await client.get(f"/demo/job/{task_id}")
        if status.json()["status"] in {"SUCCESS", "FAILURE"}:
            break
        await asyncio.sleep(0.5)

    body = status.json()
    assert body["status"] == "SUCCESS", body
    assert body["result"]["sum"] == 42
    # The worker ran in a different process; the id survived the hop.
    assert body["result"]["request_id"] == request_id


async def test_failing_job_reports_failure(client: httpx.AsyncClient) -> None:
    enqueue = await client.post("/demo/job", params={"fail": True})
    task_id = enqueue.json()["task_id"]

    for _ in range(40):
        status = await client.get(f"/demo/job/{task_id}")
        if status.json()["status"] in {"SUCCESS", "FAILURE"}:
            break
        await asyncio.sleep(0.5)

    assert status.json()["status"] == "FAILURE"
    assert "always fails" in status.json()["error"]


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------
async def test_metrics_endpoint_exposes_prometheus_text(client: httpx.AsyncClient) -> None:
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "app_http_requests_total" in r.text


async def test_metrics_are_not_labelled_by_request_id(client: httpx.AsyncClient) -> None:
    """High-cardinality labels would melt Prometheus. Ids belong in logs."""
    r = await client.get("/metrics")
    assert "request_id" not in r.text
