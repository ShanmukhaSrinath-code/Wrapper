"""Unit tests for the tickets POC.

Two kinds of test here, and the split is deliberate.

1. **Logic** -- the status machine and the update diff. Pure functions, no
   database, no HTTP, no stack. These are the tests that would catch a real
   regression in the feature.
2. **Seam** -- proof that the base actually picked the feature up. The router is
   mounted, the task is registered, the table is in the metadata Alembic reads.
   Nothing in `app/core/**` or `app/main.py` was edited to make that true, so
   these tests are really asserting a property of the base, not of the feature.

Anything needing Postgres, Redis or MinIO lives in
`tests/integration/test_tickets_flow.py` and skips loudly when the stack is
down. A "unit" test that quietly depends on a container is worse than no test.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.errors import ConflictError
from app.services.tickets import service
from app.services.tickets.models import Ticket

# --------------------------------------------------------------------------
# the status machine
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        ("open", "in_progress"),
        ("open", "closed"),
        ("in_progress", "resolved"),
        ("in_progress", "open"),
        ("resolved", "closed"),
        ("resolved", "in_progress"),
    ],
)
def test_legal_transitions_are_allowed(current: str, requested: str) -> None:
    service.check_transition(current, requested)  # must not raise


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        ("open", "resolved"),  # cannot skip the work
        ("closed", "open"),  # closed is terminal
        ("closed", "in_progress"),
        ("resolved", "open"),
    ],
)
def test_illegal_transitions_raise_conflict(current: str, requested: str) -> None:
    with pytest.raises(ConflictError) as excinfo:
        service.check_transition(current, requested)

    # The detail is part of the contract: a client gets told what *was* allowed,
    # which is the difference between a usable 409 and a rude one.
    detail = excinfo.value.detail
    assert detail["current_status"] == current
    assert detail["requested_status"] == requested
    assert requested not in detail["allowed"]


def test_a_no_op_transition_is_not_a_conflict() -> None:
    """Re-sending the current status is idempotent, not an error.

    Retries are normal -- the base retries tasks and clients retry requests --
    so a redundant write must not fail.
    """
    for status_value in service.TRANSITIONS:
        service.check_transition(status_value, status_value)


def test_closed_has_no_exits() -> None:
    assert service.TRANSITIONS["closed"] == set()


# --------------------------------------------------------------------------
# the update diff
# --------------------------------------------------------------------------


def _open_ticket() -> Ticket:
    """A model instance with no session behind it.

    Safe because nothing here touches a relationship or a server default -- the
    moment a test needs `created_at`, it needs a database and belongs in the
    integration suite.
    """
    return Ticket(
        title="Printer on fire",
        description="Again.",
        status="open",
        priority="normal",
        requester="dev",
        assignee=None,
    )


def test_apply_update_reports_only_what_changed() -> None:
    row = _open_ticket()

    changes = service.apply_update(
        row, status=None, priority="high", assignee="alice", resolution_note=None
    )

    assert set(changes) == {"priority", "assignee"}
    assert changes["priority"] == {"from": "normal", "to": "high"}
    assert row.priority == "high"
    assert row.assignee == "alice"
    assert row.status == "open"


def test_apply_update_with_nothing_to_do_returns_an_empty_diff() -> None:
    row = _open_ticket()

    changes = service.apply_update(
        row, status="open", priority="normal", assignee=None, resolution_note=None
    )

    assert changes == {}


def test_resolving_stamps_resolved_at_and_reopening_clears_it() -> None:
    row = _open_ticket()

    service.apply_update(
        row, status="in_progress", priority=None, assignee=None, resolution_note=None
    )
    service.apply_update(
        row, status="resolved", priority=None, assignee=None, resolution_note="Unplugged it."
    )
    assert row.resolved_at is not None
    assert row.resolution_note == "Unplugged it."

    service.apply_update(
        row, status="in_progress", priority=None, assignee=None, resolution_note=None
    )
    # A resolution timestamp on a ticket that is open again would be a lie.
    assert row.resolved_at is None


def test_apply_update_refuses_an_illegal_status_before_mutating_anything() -> None:
    row = _open_ticket()

    with pytest.raises(ConflictError):
        service.apply_update(
            row, status="resolved", priority="urgent", assignee=None, resolution_note=None
        )

    # The guard runs first, so the row is untouched and the transaction the
    # router is inside has nothing to roll back.
    assert row.status == "open"
    assert row.priority == "normal"


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------


def test_attachment_key_strips_path_traversal() -> None:
    ticket_id = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

    key = service.attachment_key(ticket_id, "../../../etc/passwd")

    assert key == f"tickets/{ticket_id}/passwd"
    assert ".." not in key


def test_attachment_key_handles_windows_separators_and_empty_names() -> None:
    ticket_id = uuid.uuid4()

    assert service.attachment_key(ticket_id, r"C:\temp\report.pdf").endswith("/report.pdf")
    assert service.attachment_key(ticket_id, "").endswith("/attachment")


def test_cache_keys_are_namespaced() -> None:
    ticket_id = uuid.uuid4()
    assert service.ticket_cache_key(ticket_id) == f"ticket:{ticket_id}"
    # A shared Redis is normal; an unprefixed key is how two features collide.
    assert service.STATS_CACHE_KEY.startswith("tickets:")


# --------------------------------------------------------------------------
# the seam: the base found the feature without being told about it
# --------------------------------------------------------------------------


def test_the_router_is_auto_mounted() -> None:
    from app.core.discovery import discover_routers

    mounted = dict(discover_routers())

    assert "app.services.tickets.router" in mounted
    paths = {route.path for route in mounted["app.services.tickets.router"].routes}  # type: ignore[attr-defined]
    assert "/tickets" in paths
    assert "/tickets/{ticket_id}" in paths
    assert "/tickets/stats" in paths


def test_stats_is_matched_before_the_id_route() -> None:
    """`/tickets/stats` must not be swallowed by `/tickets/{ticket_id}`.

    Starlette matches in declaration order, so this is a real ordering bug that
    a rename or a reshuffle could reintroduce -- and it would surface as a
    baffling 422 about an invalid UUID.
    """
    from app.services.tickets.router import router

    order = [route.path for route in router.routes]  # type: ignore[attr-defined]
    assert order.index("/tickets/stats") < order.index("/tickets/{ticket_id}")


def test_the_tasks_are_registered_with_the_worker() -> None:
    from app.core.jobs import celery_app, ensure_tasks_loaded

    ensure_tasks_loaded()

    assert "tickets.notify_assignee" in celery_app.tasks
    assert "tickets.escalate_stale" in celery_app.tasks


def test_the_tasks_inherit_the_base_retry_policy() -> None:
    """No retry configuration in `tasks.py`, and yet."""
    from app.core.jobs import celery_app, ensure_tasks_loaded

    ensure_tasks_loaded()
    task = celery_app.tasks["tickets.notify_assignee"]

    assert ConnectionError in task.autoretry_for
    assert task.retry_backoff is True
    assert task.retry_jitter is True
    assert task.max_retries >= 1


def test_the_table_is_visible_to_alembic() -> None:
    """The model is in `Base.metadata`, which is what autogenerate diffs.

    No import was added to any `__init__.py` to achieve this.
    """
    from app.core.db import Base
    from app.core.discovery import import_discovered_models

    import_discovered_models()

    assert "ticket" in Base.metadata.tables
    columns = Base.metadata.tables["ticket"].columns
    assert {"id", "created_at", "updated_at", "status", "priority"} <= set(columns.keys())
    # Bytes belong in the object store; the table holds the key.
    assert "attachment_key" in columns


def test_the_feature_did_not_reach_into_core() -> None:
    """A cheap stand-in for the import-linter contract.

    `make lint` enforces the real rule (`app.core` must not import
    `app.services`). This asserts the other half that matters day to day: the
    feature depends on the published seams, not on driver libraries.
    """
    import pathlib

    package = pathlib.Path("app/services/tickets")
    sources = "\n".join(p.read_text(encoding="utf-8") for p in package.glob("*.py"))

    for forbidden in ("import boto3", "import redis", "from redis", "import logging"):
        assert forbidden not in sources, f"feature reaches past the seam: {forbidden}"


def test_the_verify_router_is_mounted_too() -> None:
    """A second router in the same package is also auto-discovered.

    Worth asserting explicitly: discovery walks *modules*, not packages, so one
    feature package can expose more than one router without any registration.
    """
    from app.core.discovery import discover_routers

    mounted = dict(discover_routers())

    assert "app.services.tickets.verify" in mounted
    paths = {route.path for route in mounted["app.services.tickets.verify"].routes}  # type: ignore[attr-defined]
    assert paths == {"/verify/blocks", "/verify/ids"}
