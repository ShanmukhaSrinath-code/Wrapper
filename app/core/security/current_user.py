"""The authentication seam.

AUTH IS DEFERRED. This module intentionally contains **no** authentication or
authorization. It exists so every route that will one day need a caller identity
already depends on it today -- when real auth arrives, only this file changes and
no route signature moves.

TODO: replace with Entra ID (OIDC bearer validation) + Casbin (policy check).
      The replacement must keep the ``Principal`` shape and the
      ``get_current_user`` dependency name so routes stay untouched.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, Field


class Principal(BaseModel):
    """Who is making the request.

    Deliberately minimal: an id, a display name and a role list is enough for
    audit rows and for a future Casbin ``(sub, obj, act)`` policy check.
    """

    id: str = Field(description="Stable subject identifier (Entra ID `oid` later).")
    name: str = Field(default="", description="Human-readable display name.")
    roles: list[str] = Field(default_factory=list, description="Role names for authorization.")
    tenant_id: str | None = Field(default=None, description="Entra ID tenant, when available.")

    def has_role(self, role: str) -> bool:
        return role in self.roles


#: The single stub identity every request currently runs as.
STUB_PRINCIPAL = Principal(id="dev", name="Local Developer", roles=["dev"])


def get_current_user() -> Principal:
    """Return the caller's :class:`Principal`.

    TODO: replace with Entra ID + Casbin. The real implementation will take
    ``request: Request`` / a bearer credential, validate the token against the
    Entra ID JWKS, map claims onto ``Principal``, and enforce a Casbin policy.
    Until then every caller is the same stub developer principal.
    """
    return STUB_PRINCIPAL


#: Import this alias in routes: ``user: CurrentUser``.
CurrentUser = Annotated[Principal, Depends(get_current_user)]
