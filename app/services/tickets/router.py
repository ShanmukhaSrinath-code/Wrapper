"""Ticket endpoints.

Nothing here is registered by hand: `discover_routers()` finds the module-level
`router` and mounts it. Note what these handlers do *not* do -- no logging
setup, no correlation ids, no try/except for the error shape, no session
management, no metrics. All of that is already applied to them.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Query, Request, Response, UploadFile, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.core.audit import write_audit
from app.core.config import settings
from app.core.db.session import DbSession
from app.core.errors import NotFoundError, PayloadTooLargeError
from app.core.jobs import enqueue
from app.core.logging import get_logger
from app.core.security.current_user import CurrentUser
from app.core.storage import get_storage
from app.services.tickets import service
from app.services.tickets.models import Ticket
from app.services.tickets.schemas import (
    Priority,
    Status,
    TicketCreate,
    TicketList,
    TicketRead,
    TicketStats,
    TicketUpdate,
)

log = get_logger(__name__)

router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.post("", response_model=TicketRead, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    request: Request,
    payload: TicketCreate,
    session: DbSession,
    user: CurrentUser,
) -> TicketRead:
    """File a ticket.

    Four subsystems in one handler, and only the first is this feature's code:
    Postgres (the row), audit (who did it), the queue (notify the assignee), and
    the cache (drop the now-stale stats).
    """
    row = Ticket(
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        assignee=payload.assignee,
        # From the principal, never from the body.
        requester=user.id,
        status="open",
    )
    session.add(row)
    # Flush, not commit. DbSession commits when the request succeeds and rolls
    # back if anything after this raises.
    await session.flush()

    await write_audit(
        "ticket.created",
        resource_type="ticket",
        resource_id=str(row.id),
        http_method=request.method,
        http_path=request.url.path,
        client_ip=request.client.host if request.client else None,
        detail={"title": row.title, "priority": row.priority, "assignee": row.assignee},
    )

    result = TicketRead.model_validate(row)

    if row.assignee:
        # The task is given everything it needs as arguments. It deliberately
        # does not re-read the row: this message is published before the session
        # commits, so a worker that queried the table could find nothing.
        # Self-contained payloads sidestep that race entirely.
        enqueue(
            "tickets.notify_assignee",
            str(row.id),
            row.assignee,
            row.title,
            row.priority,
        )

    await service.invalidate(row.id)
    log.info("ticket.created", ticket_id=str(row.id), priority=row.priority)
    return result


@router.get("", response_model=TicketList)
async def list_tickets(
    session: DbSession,
    user: CurrentUser,
    status_filter: Status | None = Query(default=None, alias="status"),
    priority: Priority | None = Query(default=None),
    assignee: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> TicketList:
    """A page of tickets, plus the honest total for that filter.

    Not cached: it is parameterised enough that the hit rate would be poor and
    invalidation would have to be a prefix scan.
    """
    query = service.list_query(status_filter, priority, assignee)

    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    page = query.order_by(Ticket.created_at.desc()).limit(limit).offset(offset)
    rows = (await session.scalars(page)).all()

    return TicketList(
        items=[TicketRead.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/stats", response_model=TicketStats)
async def ticket_stats(session: DbSession, user: CurrentUser, response: Response) -> TicketStats:
    """Cached aggregate. `X-Cache` says whether Redis answered.

    Declared before `/{ticket_id}` so the literal path wins the match -- a
    routing detail that is easy to get wrong and that fails as a confusing 422
    rather than a 404.
    """
    payload, hit = await service.stats_cached(session)
    response.headers["X-Cache"] = "HIT" if hit else "MISS"
    return TicketStats.model_validate(payload)


@router.get("/{ticket_id}", response_model=TicketRead)
async def get_ticket(
    ticket_id: uuid.UUID, session: DbSession, user: CurrentUser, response: Response
) -> TicketRead:
    """Read-through cache. A Redis outage shows up as a permanent MISS, not a 500."""
    payload, hit = await service.read_cached(session, ticket_id)
    response.headers["X-Cache"] = "HIT" if hit else "MISS"
    return TicketRead.model_validate(payload)


@router.patch("/{ticket_id}", response_model=TicketRead)
async def update_ticket(
    request: Request,
    ticket_id: uuid.UUID,
    payload: TicketUpdate,
    session: DbSession,
    user: CurrentUser,
) -> TicketRead:
    """Apply a partial update, enforcing the status machine.

    An illegal transition raises `ConflictError` -> 409 in the standard error
    shape, logged as a warning and not reported to Sentry. A rejected state
    change is not a bug.
    """
    row = await service.load(session, ticket_id)

    changes = service.apply_update(
        row,
        status=payload.status,
        priority=payload.priority,
        assignee=payload.assignee,
        resolution_note=payload.resolution_note,
    )
    await session.flush()
    # `updated_at` is maintained by the database (`onupdate=func.now()`), so
    # after an UPDATE the attribute is stale and SQLAlchemy would lazy-load it
    # on access -- which raises MissingGreenlet under async. Refresh explicitly.
    # Inserts do not need this: they get their defaults back via RETURNING.
    await session.refresh(row)

    if changes:
        await write_audit(
            "ticket.updated",
            resource_type="ticket",
            resource_id=str(ticket_id),
            http_method=request.method,
            http_path=request.url.path,
            client_ip=request.client.host if request.client else None,
            detail={"changes": changes},
        )

    await service.invalidate(ticket_id)
    log.info("ticket.updated", ticket_id=str(ticket_id), changed=sorted(changes))
    return TicketRead.model_validate(row)


@router.post("/{ticket_id}/attachment", response_model=TicketRead)
async def attach_file(
    request: Request,
    ticket_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    file: UploadFile = File(...),
) -> TicketRead:
    """Bytes to the object store, key to Postgres.

    The size check duplicates the middleware's limit on purpose: the middleware
    guards the socket, this guards the business rule.
    """
    data = await file.read()
    if len(data) > settings.max_request_body_bytes:
        raise PayloadTooLargeError(
            f"Attachment exceeds the {settings.max_request_body_bytes} byte limit.",
            detail={"size_bytes": len(data), "limit_bytes": settings.max_request_body_bytes},
        )

    row = await service.load(session, ticket_id)
    filename = file.filename or "attachment"
    key = service.attachment_key(ticket_id, filename)

    stored = await get_storage().put(
        key,
        data,
        content_type=file.content_type or "application/octet-stream",
        metadata={"ticket_id": str(ticket_id), "uploaded_by": user.id},
    )

    row.attachment_key = stored.key
    row.attachment_name = filename
    await session.flush()
    # Same reason as in `update_ticket`: this is an UPDATE, so `updated_at` has
    # to come back from the database before anything reads it.
    await session.refresh(row)

    await write_audit(
        "ticket.attachment_added",
        resource_type="ticket",
        resource_id=str(ticket_id),
        http_method=request.method,
        http_path=request.url.path,
        detail={"filename": filename, "size_bytes": stored.size, "storage_key": stored.key},
    )

    await service.invalidate(ticket_id)
    return TicketRead.model_validate(row)


@router.get("/{ticket_id}/attachment")
async def download_attachment(
    ticket_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> RedirectResponse:
    """Hand back a presigned URL so the bytes never pass through this service."""
    row = await service.load(session, ticket_id)
    if not row.attachment_key:
        raise NotFoundError(f"Ticket {ticket_id} has no attachment.")

    url = await get_storage().presigned_url(row.attachment_key)
    return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
