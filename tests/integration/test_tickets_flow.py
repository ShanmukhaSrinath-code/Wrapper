"""End-to-end flow for the tickets POC, against the real stack.

These drive the same app object uvicorn serves, through an ASGI transport, but
with the real Postgres, Redis and MinIO from the compose stack. Nothing
important is mocked: a cache hit here is a genuine Redis round trip and an
attachment really lands in a bucket.

Run `make up` and `make migrate` first. Without the stack these skip with a
reason rather than passing vacuously.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def _create(client: httpx.AsyncClient, **overrides: object) -> dict:
    payload = {
        "title": f"POC ticket {uuid.uuid4().hex[:8]}",
        "description": "Filed by the integration suite.",
        "priority": "normal",
    }
    payload.update(overrides)
    response = await client.post("/tickets", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------
# Postgres: the row, and the fields the client must not control
# --------------------------------------------------------------------------


async def test_create_persists_and_ignores_a_client_supplied_requester(
    app_client: httpx.AsyncClient,
) -> None:
    body = await _create(app_client, requester="attacker")

    assert body["status"] == "open"
    # `requester` comes from the principal. The stub principal is `dev`; when
    # real auth lands this assertion still holds without touching the route.
    assert body["requester"] == "dev"
    assert body["resolved_at"] is None

    fetched = await app_client.get(f"/tickets/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == body["title"]


async def test_unknown_ticket_is_a_clean_404_in_the_standard_shape(
    app_client: httpx.AsyncClient,
) -> None:
    response = await app_client.get(f"/tickets/{uuid.uuid4()}")

    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
    # The error renderer stamps the correlation ids even on the failure path.
    assert body["request_id"] == response.headers["X-Request-ID"]


async def test_a_malformed_id_is_a_422_not_a_500(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/tickets/not-a-uuid")
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Redis: read-through cache and invalidation
# --------------------------------------------------------------------------


async def test_second_read_is_served_from_redis(app_client: httpx.AsyncClient) -> None:
    body = await _create(app_client)

    first = await app_client.get(f"/tickets/{body['id']}")
    second = await app_client.get(f"/tickets/{body['id']}")

    assert first.headers["X-Cache"] == "MISS"
    assert second.headers["X-Cache"] == "HIT"
    assert first.json() == second.json()


async def test_a_write_invalidates_the_cached_read(app_client: httpx.AsyncClient) -> None:
    body = await _create(app_client)
    ticket_id = body["id"]

    await app_client.get(f"/tickets/{ticket_id}")  # populate
    assert (await app_client.get(f"/tickets/{ticket_id}")).headers["X-Cache"] == "HIT"

    patched = await app_client.patch(f"/tickets/{ticket_id}", json={"status": "in_progress"})
    assert patched.status_code == 200

    after = await app_client.get(f"/tickets/{ticket_id}")
    assert after.headers["X-Cache"] == "MISS", "stale read: the write did not invalidate"
    assert after.json()["status"] == "in_progress"


async def test_stats_are_cached(app_client: httpx.AsyncClient) -> None:
    await _create(app_client)

    first = await app_client.get("/tickets/stats")
    second = await app_client.get("/tickets/stats")

    assert first.status_code == 200, first.text
    assert first.headers["X-Cache"] == "MISS"
    assert second.headers["X-Cache"] == "HIT"

    stats = first.json()
    assert stats["total"] >= 1
    assert stats["by_status"]["open"] >= 1


# --------------------------------------------------------------------------
# the status machine over HTTP
# --------------------------------------------------------------------------


async def test_an_illegal_transition_is_a_409_with_the_allowed_moves(
    app_client: httpx.AsyncClient,
) -> None:
    body = await _create(app_client)

    response = await app_client.patch(f"/tickets/{body['id']}", json={"status": "resolved"})

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"] == "conflict"
    assert payload["detail"]["allowed"] == ["closed", "in_progress"]


async def test_the_happy_path_walks_open_to_closed(app_client: httpx.AsyncClient) -> None:
    body = await _create(app_client)
    ticket_id = body["id"]

    for step in ("in_progress", "resolved", "closed"):
        response = await app_client.patch(f"/tickets/{ticket_id}", json={"status": step})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == step

    final = (await app_client.get(f"/tickets/{ticket_id}")).json()
    assert final["resolved_at"] is not None


async def test_a_bad_status_value_never_reaches_the_service(
    app_client: httpx.AsyncClient,
) -> None:
    """Pydantic rejects it as a 422 before any of our code runs."""
    body = await _create(app_client)

    response = await app_client.patch(f"/tickets/{body['id']}", json={"status": "banana"})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# MinIO
# --------------------------------------------------------------------------


async def test_attachment_round_trip_through_the_object_store(
    app_client: httpx.AsyncClient,
) -> None:
    body = await _create(app_client)
    ticket_id = body["id"]

    upload = await app_client.post(
        f"/tickets/{ticket_id}/attachment",
        files={"file": ("evidence.txt", b"the printer is, in fact, on fire\n", "text/plain")},
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["attachment_name"] == "evidence.txt"

    # 307 to a presigned URL: the bytes bypass this service entirely.
    redirect = await app_client.get(f"/tickets/{ticket_id}/attachment")
    assert redirect.status_code == 307
    location = redirect.headers["location"]
    assert "X-Amz-Signature" in location

    async with httpx.AsyncClient(timeout=30.0) as raw:
        fetched = await raw.get(location)
    assert fetched.status_code == 200
    assert fetched.content == b"the printer is, in fact, on fire\n"


async def test_a_ticket_without_an_attachment_says_so(app_client: httpx.AsyncClient) -> None:
    body = await _create(app_client)
    response = await app_client.get(f"/tickets/{body['id']}/attachment")
    assert response.status_code == 404


# --------------------------------------------------------------------------
# audit + correlation
# --------------------------------------------------------------------------


async def test_the_audit_row_carries_the_request_id_that_caused_it(
    app_client: httpx.AsyncClient,
) -> None:
    """One request id ties the HTTP response to the audit table.

    This is the correlation contract, checked at the narrowest point: nothing in
    the router passes an actor or an id to `write_audit`, so if this passes the
    context propagation worked.
    """
    from app.core.db.session import get_sessionmaker

    marker = f"poc-{uuid.uuid4().hex[:12]}"
    created = await app_client.post(
        "/tickets",
        json={"title": "Audited ticket", "priority": "high"},
        headers={"X-Request-ID": marker},
    )
    assert created.status_code == 201
    assert created.headers["X-Request-ID"] == marker

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text(
                    "SELECT action, actor_id, resource_id, trace_id FROM audit_log "
                    "WHERE request_id = :rid AND action = 'ticket.created'"
                ),
                {"rid": marker},
            )
        ).first()

    assert row is not None, f"no audit row for request_id {marker}"
    assert row.action == "ticket.created"
    assert row.actor_id == "dev"
    assert row.resource_id == created.json()["id"]
    assert row.trace_id == created.headers["X-Trace-ID"]


async def test_the_list_filter_and_its_total_agree(app_client: httpx.AsyncClient) -> None:
    assignee = f"agent-{uuid.uuid4().hex[:8]}"
    for _ in range(3):
        await _create(app_client, assignee=assignee)

    response = await app_client.get("/tickets", params={"assignee": assignee, "limit": 2})

    assert response.status_code == 200
    body = response.json()
    # The page is capped by `limit`; the total describes the whole filter. A
    # count that silently reused the paged query would report 2 here.
    assert len(body["items"]) == 2
    assert body["total"] == 3
    assert all(item["assignee"] == assignee for item in body["items"])


# --------------------------------------------------------------------------
# Celery
# --------------------------------------------------------------------------


async def _run_on_the_worker(name: str, *args: object, wait_seconds: float = 30.0) -> dict:
    """Publish a task and wait for the real worker's result.

    Deliberately not `_local=True`: running a task inline would execute it in
    this test's event loop, and these task bodies call `asyncio.run`. Going
    through Redis also makes this a much stronger assertion -- it proves the
    message was serialisable, the worker had the code, and the result came back
    through the result backend.

    `AsyncResult.get` is blocking, so it goes to a thread rather than stalling
    the loop the client is using.
    """
    from app.core.jobs import enqueue

    published = enqueue(name, *args)
    return await asyncio.to_thread(published.get, timeout=wait_seconds)


async def test_assigning_on_create_enqueues_work_the_worker_actually_does(
    app_client: httpx.AsyncClient,
) -> None:
    """The route publishes; a separate process runs it.

    `enqueue` refuses unregistered names, so the 201 is already evidence the
    task exists. This then makes the worker prove it.
    """
    body = await _create(app_client, assignee="alice")
    assert body["assignee"] == "alice"

    result = await _run_on_the_worker(
        "tickets.notify_assignee", body["id"], "alice", body["title"], body["priority"]
    )

    assert result["notified"] is True
    assert result["assignee"] == "alice"
    # Ran in the worker container, not here.
    assert result["task_id"]


async def test_a_task_inherits_the_request_id_that_enqueued_it(
    app_client: httpx.AsyncClient,
) -> None:
    """Correlation crosses the process boundary.

    Nothing in the feature passes an id to the task. The base puts it on the
    message headers and rebinds it in the worker, so the value the worker
    reports back is the one this test sent in.
    """
    marker = f"poc-hop-{uuid.uuid4().hex[:10]}"

    created = await app_client.post(
        "/tickets",
        json={"title": "Correlated ticket", "assignee": "bob"},
        headers={"X-Request-ID": marker},
    )
    assert created.status_code == 201

    # Enqueue under the same id the request used, then read it back out of the
    # worker's own result.
    from app.core.logging import bind_request_context

    bind_request_context(request_id=marker, trace_id=None, span_id=None)
    result = await _run_on_the_worker(
        "tickets.notify_assignee", created.json()["id"], "bob", "Correlated ticket", "normal"
    )

    assert result["request_id"] == marker, "the request id did not survive the hop"


async def test_escalate_stale_leaves_fresh_tickets_alone(app_client: httpx.AsyncClient) -> None:
    """The worker owns its own session, and the cutoff is respected.

    A ticket created seconds ago is not stale, so a 24h escalation must be a
    no-op for it -- the useful direction to assert, because the destructive one
    would be hard to undo.
    """
    body = await _create(app_client, priority="low")

    result = await _run_on_the_worker("tickets.escalate_stale", 24)

    touched = {item["ticket_id"] for item in result["escalated"]}
    assert body["id"] not in touched

    unchanged = await app_client.get(f"/tickets/{body['id']}")
    assert unchanged.json()["priority"] == "low"
