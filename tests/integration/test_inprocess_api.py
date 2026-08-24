"""In-process API tests.

`test_api.py` drives the containerised app over HTTP, which proves the real
deployment works but is invisible to coverage -- the code runs in another
process. These tests drive the very same app object through an ASGI transport
(the shared `app_client` fixture in tests/conftest.py), so the routers,
middleware, error handlers and adapters are actually measured.

They still use the real Postgres, Redis and MinIO from the compose stack
(published on localhost), so nothing important is mocked away.
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
async def test_liveness(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_readiness_pings_real_dependencies(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/health/ready")
    assert r.status_code == 200, r.text
    assert r.json()["checks"] == {"postgres": "ok", "redis": "ok", "storage": "ok"}


# --------------------------------------------------------------------------
# docs
# --------------------------------------------------------------------------
async def test_swagger_docs_pin_their_inline_script(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/docs")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "'sha256-" in csp, "the inline bootstrap script is not pinned by hash"
    assert "'unsafe-inline'" not in csp.split("style-src")[0]


async def test_redoc_allows_its_blob_worker(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/redoc")
    assert r.status_code == 200
    assert "worker-src 'self' blob:" in r.headers["content-security-policy"]


async def test_oauth2_redirect_page_is_served(app_client: httpx.AsyncClient) -> None:
    assert (await app_client.get("/docs/oauth2-redirect")).status_code == 200


async def test_openapi_lists_the_routers(app_client: httpx.AsyncClient) -> None:
    paths = (await app_client.get("/openapi.json")).json()["paths"]
    for p in ("/health/live", "/files", "/demo/cached", "/demo/job", "/metrics"):
        assert p in paths


# --------------------------------------------------------------------------
# correlation middleware
# --------------------------------------------------------------------------
async def test_request_id_is_minted_and_returned(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/health/live")
    assert len(r.headers["X-Request-ID"]) >= 8
    assert len(r.headers["X-Trace-ID"]) == 32


async def test_inbound_request_id_is_honoured(app_client: httpx.AsyncClient) -> None:
    mine = f"inproc-{uuid.uuid4().hex[:10]}"
    r = await app_client.get("/health/live", headers={"X-Request-ID": mine})
    assert r.headers["X-Request-ID"] == mine


async def test_hostile_request_id_is_discarded(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/health/live", headers={"X-Request-ID": "a;b$(c)"})
    assert r.headers["X-Request-ID"] != "a;b$(c)"


async def test_request_id_header_is_not_duplicated(app_client: httpx.AsyncClient) -> None:
    """Regression guard: the middleware once appended a second copy on errors."""
    for path in ("/health/live", "/demo/not-found", "/demo/boom"):
        r = await app_client.get(path)
        assert r.headers.get_list("X-Request-ID") == [r.headers["X-Request-ID"]]


# --------------------------------------------------------------------------
# security middleware
# --------------------------------------------------------------------------
async def test_security_headers_on_success(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/health/live")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Content-Security-Policy"].startswith("default-src 'none'")


async def test_security_headers_on_error(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/demo/boom")
    assert r.status_code == 500
    assert r.headers["X-Content-Type-Options"] == "nosniff"


async def test_hsts_present_only_over_https(app_client: httpx.AsyncClient) -> None:
    plain = await app_client.get("/health/live")
    assert "strict-transport-security" not in plain.headers

    secure = await app_client.get("https://testserver/health/live")
    assert secure.headers["strict-transport-security"].startswith("max-age=")


async def test_oversized_body_is_rejected(app_client: httpx.AsyncClient) -> None:
    r = await app_client.post("/files", files={"file": ("big.bin", b"x" * (11 * 1024 * 1024))})
    assert r.status_code == 413
    assert r.json()["error"] == "payload_too_large"


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------
async def test_cache_miss_then_hit(app_client: httpx.AsyncClient) -> None:
    seed = 9021
    await app_client.request("DELETE", "/demo/cached", params={"seed": seed})
    assert (await app_client.get("/demo/cached", params={"seed": seed})).json()["cache"] == "MISS"
    assert (await app_client.get("/demo/cached", params={"seed": seed})).json()["cache"] == "HIT"


async def test_cache_delete_reports_how_many_keys_went(app_client: httpx.AsyncClient) -> None:
    seed = 9022
    await app_client.get("/demo/cached", params={"seed": seed})
    r = await app_client.request("DELETE", "/demo/cached", params={"seed": seed})
    assert r.json()["invalidated"] == 1


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------
async def test_unhandled_exception_is_shaped_and_correlated(
    app_client: httpx.AsyncClient,
) -> None:
    r = await app_client.get("/demo/boom")
    body = r.json()
    assert r.status_code == 500
    assert body["error"] == "internal_error"
    assert body["request_id"] == r.headers["X-Request-ID"]
    assert "Traceback" not in r.text and "ZeroDivision" not in r.text


async def test_app_error_becomes_404(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/demo/not-found")
    assert r.status_code == 404
    assert r.json()["detail"] == {"looked_for": "nothing"}


async def test_unrouted_path_uses_the_same_schema(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/no-such-route")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


async def test_validation_error_reports_the_field(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/demo/cached", params={"seed": "abc"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["query", "seed"]


async def test_method_not_allowed(app_client: httpx.AsyncClient) -> None:
    r = await app_client.put("/health/live")
    assert r.status_code == 405
    assert r.json()["error"] == "method_not_allowed"


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------
async def test_audited_route_writes_a_correlated_row(app_client: httpx.AsyncClient) -> None:
    marker = f"inproc-{uuid.uuid4().hex[:10]}"
    r = await app_client.post(
        "/demo/audited", params={"note": "in-process"}, headers={"X-Request-ID": marker}
    )
    body = r.json()
    assert body["request_id"] == marker
    assert body["trace_id"] == r.headers["X-Trace-ID"]
    assert body["audit_id"]


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------
async def test_upload_download_and_presign(app_client: httpx.AsyncClient) -> None:
    payload = f"in-process {uuid.uuid4()}".encode() * 3
    digest = hashlib.sha256(payload).hexdigest()

    up = await app_client.post("/files", files={"file": ("f.txt", payload, "text/plain")})
    assert up.status_code == 201
    meta = up.json()
    assert meta["checksum_sha256"] == digest
    assert meta["storage_key"].startswith("uploads/")

    assert (await app_client.get(f"/files/{meta['id']}")).json()["id"] == meta["id"]

    content = await app_client.get(f"/files/{meta['id']}/content")
    assert content.content == payload
    assert content.headers["X-Checksum-SHA256"] == digest

    redirect = await app_client.get(f"/files/{meta['id']}/download-url", follow_redirects=False)
    assert redirect.status_code == 307
    assert meta["storage_key"] in redirect.headers["location"]


async def test_upload_sanitises_a_traversing_filename(app_client: httpx.AsyncClient) -> None:
    """A crafted filename must not escape the uploads/ prefix."""
    up = await app_client.post(
        "/files", files={"file": ("../../../etc/passwd", b"nope", "text/plain")}
    )
    assert up.status_code == 201
    key = up.json()["storage_key"]
    assert key.startswith("uploads/")
    assert ".." not in key


async def test_missing_file_is_404(app_client: httpx.AsyncClient) -> None:
    missing = uuid.uuid4()
    assert (await app_client.get(f"/files/{missing}")).status_code == 404
    assert (await app_client.get(f"/files/{missing}/content")).status_code == 404
    assert (await app_client.get(f"/files/{missing}/download-url")).status_code == 404


# --------------------------------------------------------------------------
# jobs (enqueued here, executed by the worker container)
# --------------------------------------------------------------------------
async def test_enqueue_and_poll(app_client: httpx.AsyncClient) -> None:
    enq = await app_client.post("/demo/job", params={"a": 5, "b": 6, "delay_seconds": 0.5})
    assert enq.status_code == 202
    task_id = enq.json()["task_id"]
    request_id = enq.headers["X-Request-ID"]

    for _ in range(40):
        status = await app_client.get(f"/demo/job/{task_id}")
        if status.json()["status"] in {"SUCCESS", "FAILURE"}:
            break
        await asyncio.sleep(0.5)

    body = status.json()
    assert body["status"] == "SUCCESS", body
    assert body["result"]["sum"] == 11
    assert body["result"]["request_id"] == request_id


async def test_failing_job_surfaces_its_error(app_client: httpx.AsyncClient) -> None:
    task_id = (await app_client.post("/demo/job", params={"fail": True})).json()["task_id"]
    for _ in range(40):
        status = await app_client.get(f"/demo/job/{task_id}")
        if status.json()["status"] in {"SUCCESS", "FAILURE"}:
            break
        await asyncio.sleep(0.5)
    assert status.json()["status"] == "FAILURE"


async def test_unknown_task_id_is_pending(app_client: httpx.AsyncClient) -> None:
    """Celery cannot distinguish 'never existed' from 'not started'."""
    r = await app_client.get(f"/demo/job/{uuid.uuid4()}")
    assert r.status_code == 200
    assert r.json()["status"] == "PENDING"


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------
async def test_metrics_exposed_without_high_cardinality_labels(
    app_client: httpx.AsyncClient,
) -> None:
    await app_client.get("/demo/cached", params={"seed": 5})
    r = await app_client.get("/metrics")
    assert r.status_code == 200
    assert "app_http_requests_total" in r.text
    assert "request_id" not in r.text
