"""Background work for tickets.

Task bodies are synchronous -- a Celery worker is not an event loop -- so the
async helpers run inside `asyncio.run`.

Nothing here configures retries, logging or correlation. The base task class
supplies exponential backoff with jitter for transient errors, and
`task_prerun` has already rebound this process's context to the *originating
request's* `request_id`, which is why an audit row written from a worker is
attributable to the user who caused it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.audit import write_audit
from app.core.cache import close_client
from app.core.db.session import dispose_engine, get_sessionmaker
from app.core.jobs.celery_app import celery_app
from app.core.logging import current_request_id, current_trace_id, get_logger
from app.services.tickets import service
from app.services.tickets.models import Ticket

log = get_logger(__name__)

#: Priority ladder used when escalating. "urgent" is the top; there is nowhere
#: further to go, which is deliberate -- an escalation that never terminates
#: just moves the alert fatigue somewhere else.
ESCALATION = {"low": "normal", "normal": "high", "high": "urgent"}


@asynccontextmanager
async def _loop_scoped_resources() -> AsyncIterator[None]:
    """Close the loop-bound singletons before the loop this task made goes away.

    The database engine and the Redis client are process globals, created lazily
    and bound to whichever event loop was running at the time. `asyncio.run`
    gives every task invocation a **new** loop, so a connection pool left open
    by the first invocation is unusable by the second -- it fails with
    "attached to a different loop", and only ever on the second call, which
    makes it a nasty thing to find in production.

    The API process does not have this problem: it has one long-lived loop and
    disposes on shutdown. Any task that touches Postgres, Redis or audit does,
    so it goes through here. The cost is a fresh pool per invocation.
    """
    try:
        yield
    finally:
        await dispose_engine()
        await close_client()


@celery_app.task(name="tickets.notify_assignee", bind=True)
def notify_assignee(
    self: Any, ticket_id: str, assignee: str, title: str, priority: str
) -> dict[str, Any]:
    """Tell someone they own a ticket.

    Everything needed is in the arguments, so this task never reads the row it
    was told about -- see the comment at the `enqueue` call site. A real
    implementation would send mail or post to Teams here; the audit row is the
    part worth proving.
    """
    log.info("notify_assignee.begin", ticket_id=ticket_id, assignee=assignee)

    async def _work() -> dict[str, Any]:
        async with _loop_scoped_resources():
            await write_audit(
                "ticket.assignee_notified",
                resource_type="ticket",
                resource_id=ticket_id,
                detail={"assignee": assignee, "title": title, "priority": priority},
            )
        return {
            "ticket_id": ticket_id,
            "assignee": assignee,
            "notified": True,
            "task_id": self.request.id,
            # Echoed out so a test can assert correlation survived the process
            # hop without scraping worker logs.
            "request_id": current_request_id(),
            "trace_id": current_trace_id(),
        }

    result = asyncio.run(_work())
    log.info("notify_assignee.done", **result)
    return result


@celery_app.task(name="tickets.escalate_stale", bind=True)
def escalate_stale(self: Any, older_than_hours: int = 24) -> dict[str, Any]:
    """Bump the priority of open tickets nobody has touched.

    This one reaches Postgres, which is the point: it shows a worker owning its
    own session and transaction rather than borrowing the request-scoped one.
    `get_sessionmaker` is the same factory the API dependency uses, so the pool
    settings and the least-privilege role are identical.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=older_than_hours)
    log.info("escalate_stale.begin", cutoff=cutoff.isoformat())

    async def _work() -> dict[str, Any]:
        escalated: list[dict[str, str]] = []
        async with _loop_scoped_resources():
            async with get_sessionmaker()() as session:
                rows = (
                    await session.scalars(
                        select(Ticket).where(
                            Ticket.status == "open",
                            Ticket.updated_at < cutoff,
                            Ticket.priority != "urgent",
                        )
                    )
                ).all()

                for row in rows:
                    new_priority = ESCALATION.get(row.priority)
                    if new_priority is None:
                        continue
                    escalated.append(
                        {"ticket_id": str(row.id), "from": row.priority, "to": new_priority}
                    )
                    row.priority = new_priority

                # The worker commits its own unit of work; there is no
                # dependency teardown out here to do it.
                await session.commit()

            for item in escalated:
                await write_audit(
                    "ticket.escalated",
                    resource_type="ticket",
                    resource_id=item["ticket_id"],
                    detail=item,
                )
                # The read caches now disagree with the table.
                await service.invalidate(item["ticket_id"])

        return {
            "escalated_count": len(escalated),
            "escalated": escalated,
            "older_than_hours": older_than_hours,
            "task_id": self.request.id,
            "request_id": current_request_id(),
        }

    result = asyncio.run(_work())
    log.info("escalate_stale.done", escalated_count=result["escalated_count"])
    return result
