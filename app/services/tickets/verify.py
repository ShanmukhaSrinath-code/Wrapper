"""A one-click block check, designed to be driven from Swagger UI.

Why this exists: Swagger can only send headers an endpoint *declares*, and the
correlation ids arrive as **response headers**, which is easy to miss in the UI.
This endpoint touches every block of the base in one request and returns the ids
in the **response body**, each on its own field, together with the queries to
paste into Grafana.

It lives in the tickets package rather than its own because it uses the `Ticket`
model, and the import-linter contract forbids one feature importing another.

Demo scaffolding, like `app/services/demo.py` -- delete it when the POC is done.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.core import cache
from app.core.audit import write_audit
from app.core.db.session import DbSession
from app.core.jobs import enqueue
from app.core.logging import current_request_id, current_trace_id, get_logger
from app.core.security.current_user import CurrentUser
from app.core.storage import get_storage
from app.services.tickets.models import Ticket

log = get_logger(__name__)

router = APIRouter(prefix="/verify", tags=["verify"])


class BlockResult(BaseModel):
    """One block's outcome, in the words you would use to describe it."""

    block: str
    ok: bool
    evidence: str


class VerifyResponse(BaseModel):
    # --- the ids, each on its own line so they are easy to copy ---------------
    request_id: str | None = Field(description="Identifies THIS HTTP call. Paste into Grafana.")
    trace_id: str | None = Field(description="Identifies the timing tree. Paste into Tempo.")

    # --- what this request created, so you can look each one up --------------
    ticket_id: str = Field(description="Row in the `ticket` table.")
    audit_id: str | None = Field(description="Row in `audit_log`, written under the request_id.")
    task_id: str | None = Field(description="Celery task the worker will run under the same id.")
    storage_key: str = Field(description="Object key in MinIO.")
    presigned_url: str = Field(description="Time-limited URL. Paste in a browser tab.")

    blocks: list[BlockResult]

    # --- copy-paste, so nobody has to remember LogQL on stage ----------------
    grafana_dashboard: str
    loki_query: str
    loki_query_worker_only: str
    tempo_lookup: str


@router.post(
    "/blocks",
    response_model=VerifyResponse,
    summary="Touch every block once and return the ids separately",
)
async def verify_blocks(request: Request, session: DbSession, user: CurrentUser) -> VerifyResponse:
    """Exercise Postgres, Redis, MinIO, Celery and audit in a single request.

    Deliberately sequential and deliberately boring: each step is the smallest
    real use of one block, so a failure points at exactly one thing.
    """
    request_id = current_request_id()
    trace_id = current_trace_id()
    blocks: list[BlockResult] = []

    # --- 1. Postgres ---------------------------------------------------------
    ticket = Ticket(
        title="Block verification ticket",
        description="Created by POST /verify/blocks to prove the plumbing.",
        priority="normal",
        requester=user.id,
        assignee="alice",
        status="open",
    )
    session.add(ticket)
    await session.flush()
    blocks.append(
        BlockResult(
            block="PostgreSQL",
            ok=True,
            evidence=f"inserted ticket {ticket.id}, requester={user.id!r} from the principal",
        )
    )

    # --- 2. Redis, as a cache ------------------------------------------------
    probe_key = f"verify:{uuid.uuid4().hex[:12]}"
    await cache.set_json(probe_key, {"ticket_id": str(ticket.id)}, ttl_seconds=120)
    round_tripped = await cache.get_json(probe_key)
    cache_ok = round_tripped == {"ticket_id": str(ticket.id)}
    blocks.append(
        BlockResult(
            block="Redis (cache)",
            ok=cache_ok,
            evidence=(
                f"set and read back {probe_key!r}"
                if cache_ok
                else "cache unavailable -- reads degrade to a MISS, which is not a 500"
            ),
        )
    )

    # --- 3. MinIO ------------------------------------------------------------
    key = f"verify/{ticket.id}/proof.txt"
    body = f"written by POST /verify/blocks under request_id={request_id}\n".encode()
    stored = await get_storage().put(key, body, content_type="text/plain")
    presigned = await get_storage().presigned_url(stored.key)
    blocks.append(
        BlockResult(
            block="MinIO (object store)",
            ok=stored.size == len(body),
            evidence=f"{stored.size} bytes at {stored.key}, presigned URL issued",
        )
    )

    # --- 4. Audit ------------------------------------------------------------
    entry = await write_audit(
        "verify.blocks",
        resource_type="ticket",
        resource_id=str(ticket.id),
        http_method=request.method,
        http_path=request.url.path,
        client_ip=request.client.host if request.client else None,
        detail={"storage_key": stored.key, "cache_key": probe_key},
    )
    blocks.append(
        BlockResult(
            block="Audit trail",
            ok=entry is not None,
            evidence=(
                f"row {entry.id} written under this request_id"
                if entry
                else "audit write failed -- logged at error level, business op unaffected"
            ),
        )
    )

    # --- 5. Celery, via the Redis broker ------------------------------------
    task = enqueue(
        "tickets.notify_assignee", str(ticket.id), "alice", ticket.title, ticket.priority
    )
    blocks.append(
        BlockResult(
            block="Celery worker",
            ok=bool(task.id),
            evidence=f"task {task.id} published; the worker will log under this same request_id",
        )
    )

    # --- 6. correlation ------------------------------------------------------
    blocks.append(
        BlockResult(
            block="Correlation + tracing",
            ok=bool(request_id and trace_id),
            evidence="request_id and trace_id are on every line above and on the response headers",
        )
    )

    log.info("verify.blocks", ticket_id=str(ticket.id), blocks_ok=sum(b.ok for b in blocks))

    quoted = request_id or ""
    return VerifyResponse(
        request_id=request_id,
        trace_id=trace_id,
        ticket_id=str(ticket.id),
        audit_id=str(entry.id) if entry else None,
        task_id=task.id,
        storage_key=stored.key,
        presigned_url=presigned,
        blocks=blocks,
        grafana_dashboard=(
            f"http://localhost:3001/d/common-app-base?var-request_id={quoted}&from=now-15m&to=now"
        ),
        loki_query=f'{{service=~"app|worker"}} | json | request_id = "{quoted}"',
        loki_query_worker_only=f'{{service="worker"}} | json | request_id = "{quoted}"',
        tempo_lookup=f"Grafana -> Explore -> Tempo -> paste {trace_id}",
    )


@router.get("/ids", summary="Just the correlation ids for this call")
async def correlation_ids(user: CurrentUser) -> dict[str, Any]:
    """The smallest possible probe: no side effects, just the ids.

    Useful when you want to show *where the ids come from* without creating a
    ticket, an object and a background job every time.
    """
    request_id = current_request_id()
    return {
        "request_id": request_id,
        "trace_id": current_trace_id(),
        "actor": user.id,
        "loki_query": f'{{service="app"}} | json | request_id = "{request_id or ""}"',
    }
