"""Regression tests for Fix 4 -- the audit trail must survive its own operator.

The audit found that ``UPDATE`` and ``DELETE`` were correctly rejected by
triggers, but two holes remained:

* ``TRUNCATE`` erased the whole table -- it is a statement-level event, and the
  triggers were row-level only;
* the application connected as the table **owner**, so it could simply
  ``DROP TRIGGER`` and then do as it pleased.

These tests run SQL as the *application's* role, which is the threat model that
matters: whatever an SQL-injection flaw or a careless operator can reach with
the app's own credentials.
"""

from __future__ import annotations

import subprocess
import uuid

import pytest

pytestmark = pytest.mark.integration

APP_ROLE = "appruntime"  # the role the application actually connects as
OWNER_ROLE = "appuser"  # the role migrations run as
DB_NAME = "appdb"
CONTAINER = "cab-postgres"


def run_sql(sql: str, *, role: str = APP_ROLE) -> subprocess.CompletedProcess[str]:
    """Execute SQL inside the Postgres container as ``role``."""
    return subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            CONTAINER,
            "psql",
            "-U",
            role,
            "-d",
            DB_NAME,
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ],
        input=sql,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def seeded_row() -> str:
    """Insert one audit row as the app role and return its request_id."""
    rid = f"immutability-{uuid.uuid4()}"
    result = run_sql(
        "INSERT INTO audit_log (id, action, outcome, actor_id, request_id, created_at) "
        f"VALUES (gen_random_uuid(), 'test.seed', 'success', 'dev', '{rid}', now());"
    )
    assert result.returncode == 0, f"seed insert failed: {result.stderr}"
    return rid


def test_app_role_can_insert_and_select(seeded_row: str) -> None:
    """The hardening must not break what the application actually needs."""
    result = run_sql(f"SELECT action FROM audit_log WHERE request_id = '{seeded_row}';")
    assert result.returncode == 0, result.stderr
    assert "test.seed" in result.stdout


def _assert_rejected(result: subprocess.CompletedProcess[str], what: str) -> None:
    """The operation must fail -- by grant or by trigger, either is fine.

    There are deliberately two layers. The runtime role lacks the privilege, so
    Postgres refuses before the trigger is reached; if someone ever re-grants it,
    the trigger still refuses. Asserting one specific message would make this
    test fail when the *stronger* layer catches it, so assert the property.
    """
    combined = (result.stderr + result.stdout).lower()
    assert result.returncode != 0, f"{what} succeeded against the audit log"
    assert "permission denied" in combined or "append-only" in combined, combined


def test_update_is_rejected(seeded_row: str) -> None:
    _assert_rejected(
        run_sql(f"UPDATE audit_log SET action = 'tampered' WHERE request_id = '{seeded_row}';"),
        "UPDATE",
    )


def test_delete_is_rejected(seeded_row: str) -> None:
    _assert_rejected(run_sql(f"DELETE FROM audit_log WHERE request_id = '{seeded_row}';"), "DELETE")


def test_bulk_delete_is_rejected(seeded_row: str) -> None:
    _assert_rejected(run_sql("DELETE FROM audit_log;"), "bulk DELETE")


def test_triggers_still_reject_even_for_the_owner(seeded_row: str) -> None:
    """Defence in depth: the grant is not the only thing standing in the way.

    Run as the *owner*, which has every privilege. The triggers must still fire,
    so re-granting the runtime role by mistake would not silently open the door.
    """
    for statement, label in (
        ("UPDATE audit_log SET action = 'tampered';", "UPDATE"),
        ("DELETE FROM audit_log;", "DELETE"),
        ("TRUNCATE audit_log;", "TRUNCATE"),
    ):
        result = run_sql(statement, role=OWNER_ROLE)
        combined = (result.stderr + result.stdout).lower()
        assert result.returncode != 0, f"owner {label} succeeded -- trigger missing"
        assert "append-only" in combined, f"{label}: expected trigger message, got {combined}"


def test_truncate_is_rejected(seeded_row: str) -> None:
    """The hole the audit found: TRUNCATE erased the entire trail."""
    result = run_sql("TRUNCATE audit_log;")
    assert result.returncode != 0, "TRUNCATE succeeded -- the audit trail is erasable"

    # And the row is still there.
    check = run_sql(f"SELECT action FROM audit_log WHERE request_id = '{seeded_row}';")
    assert "test.seed" in check.stdout


def test_app_role_cannot_drop_the_guard(seeded_row: str) -> None:
    """Immutability enforced by a trigger the app can drop is not immutability."""
    result = run_sql("DROP TRIGGER audit_log_no_update ON audit_log;")
    assert result.returncode != 0, "app role dropped its own append-only guard"
    assert (
        "must be owner" in (result.stderr + result.stdout).lower()
        or "permission denied" in (result.stderr + result.stdout).lower()
    )


def test_app_role_does_not_own_the_audit_table() -> None:
    """Ownership is the root privilege that makes every other guard optional."""
    result = run_sql("SELECT tableowner FROM pg_tables WHERE tablename = 'audit_log';")
    assert result.returncode == 0, result.stderr
    assert APP_ROLE not in result.stdout, "app role still owns audit_log"


def test_truncate_trigger_exists() -> None:
    """Belt and braces: assert the statement-level trigger is actually there."""
    result = run_sql(
        "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgrelid = 'audit_log'::regclass;"
    )
    assert result.returncode == 0, result.stderr
    assert "audit_log_no_truncate" in result.stdout
