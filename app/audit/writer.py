"""Writing audit entries.

`write_audit` reads the correlation ids out of the logging context rather than
taking them as arguments, so callers cannot forget to pass them and a new
call site is correlated by default.

Audit rows are written in their **own transaction**. If they shared the
request's session, a business rollback would erase the record of the attempt --
exactly the case an audit trail exists to capture.
"""

from __future__ import annotations

from typing import Any

from app.audit.models import AuditLog
from app.db.session import get_sessionmaker
from app.logging import current_request_id, current_trace_id, get_logger
from app.security.current_user import Principal

log = get_logger(__name__)


async def write_audit(
    action: str,
    *,
    actor: Principal | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    outcome: str = "success",
    http_method: str | None = None,
    http_path: str | None = None,
    client_ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog | None:
    """Append one audit entry. Returns the row, or ``None`` if the write failed.

    Never raises: a failure to audit is logged loudly but must not turn a
    successful business operation into a 500.
    """
    entry = AuditLog(
        action=action,
        outcome=outcome,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        actor_id=actor.id if actor else "anonymous",
        actor_roles=",".join(actor.roles) if actor and actor.roles else None,
        request_id=current_request_id(),
        trace_id=current_trace_id(),
        http_method=http_method,
        http_path=http_path,
        client_ip=client_ip,
        detail=detail,
    )

    try:
        async with get_sessionmaker()() as session:
            session.add(entry)
            await session.commit()
    except Exception as exc:
        log.error(
            "audit.write.failed",
            audit_action=action,
            error=f"{type(exc).__name__}: {exc}",
        )
        return None

    log.info(
        "audit.written",
        audit_action=action,
        audit_id=str(entry.id),
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        outcome=outcome,
    )
    return entry
