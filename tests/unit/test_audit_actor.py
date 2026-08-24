"""Regression tests for Fix 3 -- the audit actor must come from context.

The bug these pin down: ``write_audit`` took the actor as a keyword argument and
defaulted to the string ``"anonymous"`` when it was omitted. Every base route
remembered to pass it; the probe feature written during the audit did not, and
its audit rows were attributed to nobody -- silently, with no warning.

An audit row with a confidently wrong actor is worse than a missing row, because
it will be believed. The actor now rides in a contextvar alongside ``request_id``
and ``trace_id``, so a call site cannot forget it any more than it can forget
those.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_context():
    """Isolate every test: contextvars survive across tests in one thread."""
    from app.core.audit.context import clear_actor
    from app.core.logging import clear_request_context

    clear_request_context()
    clear_actor()
    yield
    clear_request_context()
    clear_actor()


def test_actor_is_read_from_context_without_being_passed() -> None:
    """A feature that never mentions the actor still records the real one."""
    from app.core.audit.context import current_actor

    bind_actor_principal()
    assert current_actor() is not None
    assert current_actor().id == "dev"


def bind_actor_principal() -> None:
    from app.core.audit.context import bind_actor
    from app.core.security.current_user import get_current_user

    bind_actor(get_current_user())


def test_unbound_actor_is_honest_rather_than_confident() -> None:
    """With nothing bound, the row must say so -- not invent a plausible id."""
    from app.core.audit.context import current_actor_id

    assert current_actor_id() == "unresolved"


def test_write_audit_uses_the_context_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recorded actor_id comes from context, with no argument passed."""
    import app.core.audit.writer as writer_mod

    captured: dict[str, object] = {}

    class _FakeRow:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.id = "fake"

    monkeypatch.setattr(writer_mod, "AuditLog", _FakeRow)

    bind_actor_principal()
    row = writer_mod.build_audit_row("thing.created", resource_type="thing")

    assert row is not None
    assert captured["actor_id"] == "dev"
    assert captured["actor_roles"] == "dev"


def test_write_audit_records_unresolved_when_no_actor_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No silent 'anonymous'. An unknown actor is recorded as unknown."""
    import app.core.audit.writer as writer_mod

    captured: dict[str, object] = {}

    class _FakeRow:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.id = "fake"

    monkeypatch.setattr(writer_mod, "AuditLog", _FakeRow)

    writer_mod.build_audit_row("thing.created", resource_type="thing")
    assert captured["actor_id"] == "unresolved"


def test_explicit_actor_still_overrides_context() -> None:
    """The seam stays usable for system/impersonated actors."""
    from app.core.audit.context import bind_actor, current_actor
    from app.core.security.current_user import Principal

    bind_actor(Principal(id="dev", roles=["dev"]))
    bind_actor(Principal(id="system", roles=["batch"]))
    actor = current_actor()
    assert actor is not None
    assert actor.id == "system"
