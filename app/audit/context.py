"""The acting principal, carried in a contextvar.

``request_id`` and ``trace_id`` are bound to the request context by the
correlation middleware, which is why no call site can forget them. The **actor**
used not to be: ``write_audit(actor=...)`` was a keyword argument that defaulted
to ``"anonymous"`` when omitted, so a feature that forgot it produced audit rows
attributed to nobody -- with no warning.

The actor now travels the same way as the ids. Bind it once per request and
every audit row for that request is attributed correctly, including rows written
deep in a service layer that never sees the request object.

When real authentication replaces the stub, nothing here changes: the middleware
already binds whatever ``get_current_user()`` returns.
"""

from __future__ import annotations

import contextvars

from app.security.current_user import Principal

#: Recorded when nobody has been bound. Deliberately not "anonymous": an
#: unknown actor and an anonymous actor are different claims, and an audit trail
#: that guesses is worse than one that admits ignorance.
UNRESOLVED_ACTOR_ID = "unresolved"

_actor: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "audit_actor", default=None
)


def bind_actor(principal: Principal | None) -> None:
    """Record who is acting for the remainder of this context."""
    _actor.set(principal)


def clear_actor() -> None:
    _actor.set(None)


def current_actor() -> Principal | None:
    """The bound principal, or ``None`` if nothing was bound."""
    return _actor.get()


def current_actor_id() -> str:
    """The bound principal's id, or :data:`UNRESOLVED_ACTOR_ID`."""
    actor = _actor.get()
    return actor.id if actor is not None else UNRESOLVED_ACTOR_ID


def current_actor_roles() -> str | None:
    actor = _actor.get()
    if actor is None or not actor.roles:
        return None
    return ",".join(actor.roles)
