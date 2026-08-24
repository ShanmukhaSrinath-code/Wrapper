"""The deferred-auth seam.

These tests pin the *shape* of the seam. When Entra ID + Casbin replace the
stub, they should still pass unchanged -- which is the point of the seam.
"""

from __future__ import annotations

from app.security.current_user import (
    STUB_PRINCIPAL,
    CurrentUser,
    Principal,
    get_current_user,
)


def test_stub_principal_shape() -> None:
    p = get_current_user()
    assert p.id == "dev"
    assert "dev" in p.roles


def test_has_role() -> None:
    p = Principal(id="u1", roles=["admin", "reader"])
    assert p.has_role("admin") is True
    assert p.has_role("writer") is False


def test_principal_defaults() -> None:
    p = Principal(id="u1")
    assert p.roles == []
    assert p.name == ""
    assert p.tenant_id is None


def test_stub_principal_is_the_returned_identity() -> None:
    assert get_current_user() is STUB_PRINCIPAL


def test_current_user_alias_is_a_dependency() -> None:
    """Routes annotate with `CurrentUser`; swapping auth must not touch them."""
    assert CurrentUser.__metadata__[0].dependency is get_current_user  # type: ignore[attr-defined]
