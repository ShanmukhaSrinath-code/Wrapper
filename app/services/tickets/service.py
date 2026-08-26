"""Ticket logic, kept out of the router.

The router's job is HTTP: parse, call, serialise. Everything that could be got
wrong -- which status transitions are legal, what the cache keys are, when they
are invalidated -- lives here so it can be tested without a request.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core import cache
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.services.tickets.models import Ticket
from app.services.tickets.schemas import TicketRead

log = get_logger(__name__)

#: How long a single ticket and the stats roll-up stay cached. Short, because
#: this data is written often and staleness is more annoying than a DB hit.
TICKET_TTL_SECONDS = 60
STATS_TTL_SECONDS = 30

#: Legal moves. Anything absent is a 409, not a 400: the request is
#: well-formed, it just conflicts with the ticket's current state.
TRANSITIONS: dict[str, set[str]] = {
    "open": {"in_progress", "closed"},
    "in_progress": {"resolved", "open"},
    "resolved": {"closed", "in_progress"},
    "closed": set(),
}

#: Statuses that mean "no longer being worked on".
TERMINAL_STATUSES = frozenset({"resolved", "closed"})


def ticket_cache_key(ticket_id: uuid.UUID | str) -> str:
    return f"ticket:{ticket_id}"


STATS_CACHE_KEY = "tickets:stats"


async def invalidate(ticket_id: uuid.UUID | str) -> None:
    """Drop the read caches touched by a write.

    Invalidate, do not update: a write that recomputes the cache has to get the
    serialisation exactly right in two places. Deleting is idempotent and can
    only ever cost one extra database read.
    """
    await cache.delete(ticket_cache_key(ticket_id), STATS_CACHE_KEY)


def check_transition(current: str, requested: str) -> None:
    """Raise unless `current -> requested` is a legal move."""
    if current == requested:
        return
    allowed = TRANSITIONS.get(current, set())
    if requested not in allowed:
        raise ConflictError(
            f"Cannot move a ticket from {current!r} to {requested!r}.",
            detail={
                "current_status": current,
                "requested_status": requested,
                "allowed": sorted(allowed),
            },
        )


async def load(session: AsyncSession, ticket_id: uuid.UUID) -> Ticket:
    """Fetch a ticket or raise the 404 the base already knows how to render."""
    row = await session.scalar(select(Ticket).where(Ticket.id == ticket_id))
    if row is None:
        raise NotFoundError(f"No ticket with id {ticket_id}.")
    return row


async def read_cached(session: AsyncSession, ticket_id: uuid.UUID) -> tuple[dict[str, Any], bool]:
    """Cache-aside read. Returns `(payload, cache_hit)`.

    No try/except around the cache call: a Redis outage degrades to a miss
    inside `app.core.cache`, which is exactly the behaviour wanted here.
    """

    async def _produce() -> dict[str, Any]:
        row = await load(session, ticket_id)
        return TicketRead.model_validate(row).model_dump(mode="json")

    return await cache.get_or_set(
        ticket_cache_key(ticket_id), _produce, ttl_seconds=TICKET_TTL_SECONDS
    )


def list_query(
    status: str | None = None,
    priority: str | None = None,
    assignee: str | None = None,
) -> Select[tuple[Ticket]]:
    """Build the filtered list query. Separated out so the count and the page
    provably share the same predicate."""
    query = select(Ticket)
    if status is not None:
        query = query.where(Ticket.status == status)
    if priority is not None:
        query = query.where(Ticket.priority == priority)
    if assignee is not None:
        query = query.where(Ticket.assignee == assignee)
    return query


async def _count_by(session: AsyncSession, column: InstrumentedAttribute[str]) -> dict[str, int]:
    """One `GROUP BY` as a plain dict, ready to be JSON-encoded into the cache."""
    rows = (await session.execute(select(column, func.count()).group_by(column))).all()
    return {str(value): int(count) for value, count in rows}


async def compute_stats(session: AsyncSession) -> dict[str, Any]:
    """Aggregate in the database, not in Python.

    Three grouped counts beat pulling every row across the wire, and this is the
    query the cache in front of it is protecting.
    """
    by_status = await _count_by(session, Ticket.status)
    by_priority = await _count_by(session, Ticket.priority)
    unassigned = await session.scalar(
        select(func.count()).select_from(Ticket).where(Ticket.assignee.is_(None))
    )
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "by_priority": by_priority,
        "unassigned": unassigned or 0,
    }


async def stats_cached(session: AsyncSession) -> tuple[dict[str, Any], bool]:
    async def _produce() -> dict[str, Any]:
        return await compute_stats(session)

    return await cache.get_or_set(STATS_CACHE_KEY, _produce, ttl_seconds=STATS_TTL_SECONDS)


def apply_update(
    row: Ticket,
    *,
    status: str | None,
    priority: str | None,
    assignee: str | None,
    resolution_note: str | None,
) -> dict[str, Any]:
    """Mutate `row` in place and return what actually changed.

    Returning the diff rather than the whole row keeps the audit detail small
    and makes "what did this request do" answerable from one log line.
    """
    changes: dict[str, Any] = {}

    if status is not None and status != row.status:
        check_transition(row.status, status)
        changes["status"] = {"from": row.status, "to": status}
        row.status = status
        # A resolution timestamp that outlives the resolution would lie, so it
        # is cleared when a ticket is reopened.
        row.resolved_at = datetime.now(UTC) if status in TERMINAL_STATUSES else None

    if priority is not None and priority != row.priority:
        changes["priority"] = {"from": row.priority, "to": priority}
        row.priority = priority

    if assignee is not None and assignee != row.assignee:
        changes["assignee"] = {"from": row.assignee, "to": assignee}
        row.assignee = assignee

    if resolution_note is not None and resolution_note != row.resolution_note:
        changes["resolution_note"] = {"set": True}
        row.resolution_note = resolution_note

    return changes


def attachment_key(ticket_id: uuid.UUID, filename: str) -> str:
    """Namespaced, id-unique object key.

    Path separators are stripped so a crafted filename cannot escape the
    prefix -- same defence as `app/services/files.py`.
    """
    safe = filename.replace("\\", "/").rsplit("/", 1)[-1][:200] or "attachment"
    return f"tickets/{ticket_id}/{safe}"
