"""Writing audit entries.

`write_audit` reads the correlation ids **and the acting principal** out of
context rather than taking them as arguments, so callers cannot forget to pass
them and a new call site is correlated and attributed by default.

The actor used to be a keyword argument defaulting to "anonymous"; a feature
that omitted it produced audit rows attributed to nobody. See
`app.audit.context`.

Audit rows are written in their **own transaction**. If they shared the
request's session, a business rollback would erase the record of the attempt --
exactly the case an audit trail exists to capture.
"""

from __future__ import annotations

from typing import Any

from app.audit.context import UNRESOLVED_ACTOR_ID, current_actor_id, current_actor_roles
from app.audit.models import AuditLog
from app.db.session import get_sessionmaker
from app.logging import current_request_id, current_trace_id, get_logger
from app.security.current_user import Principal

log = get_logger(__name__)


def build_audit_row(
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
) -> AuditLog:
    """Build the row, resolving the actor from context.

    Separated from the write so the attribution rules can be tested without a
    database, and so a caller that batches rows can reuse them.

    `actor` is an escape hatch for system or impersonated principals. Leave it
    unset and the request's principal is used.
    """
    resolved_id = actor.id if actor is not None else current_actor_id()
    resolved_roles = (
        ",".join(actor.roles) if actor is not None and actor.roles else current_actor_roles()
    )

    if actor is None and resolved_id == UNRESOLVED_ACTOR_ID:
        # Loud, because an unattributed audit row is a real gap -- but not fatal,
        # since losing the whole entry would be worse than losing the actor.
        log.warning(
            "audit.actor_unresolved",
            action=action,
            detail_hint="No principal bound to this context; recording 'unresolved'.",
        )

    return AuditLog(
        action=action,
        outcome=outcome,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        actor_id=resolved_id,
        actor_roles=resolved_roles,
        request_id=current_request_id(),
        trace_id=current_trace_id(),
        http_method=http_method,
        http_path=http_path,
        client_ip=client_ip,
        detail=detail,
    )


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
    entry = build_audit_row(
        action,
        actor=actor,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
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
