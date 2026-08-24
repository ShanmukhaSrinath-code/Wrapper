"""harden audit immutability: truncate guard + least-privilege runtime role

The append-only audit log had two holes that made its guarantee decorative:

1. ``UPDATE``/``DELETE`` were blocked by **row-level** triggers, but ``TRUNCATE``
   is a **statement-level** event -- so a single ``TRUNCATE audit_log`` erased
   the entire trail.
2. The application connected as the table **owner**, so it could simply
   ``DROP TRIGGER`` and then do whatever it liked.

This migration closes both. It adds the missing statement-level trigger, and it
splits the single database role into an owner (used by migrations) and a
least-privilege runtime role (used by the application), which is what actually
makes the triggers unremovable by the app.

Revision ID: a1f2c3d4e5b6
Revises: 66fba9fbe2c9
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.config import settings

revision: str = "a1f2c3d4e5b6"
down_revision: str | None = "66fba9fbe2c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables the application needs full read/write on. audit_log is deliberately
#: absent -- it gets INSERT + SELECT only, below.
APP_WRITABLE_TABLES = ("example", "stored_file")


def upgrade() -> None:
    runtime_user = settings.postgres_app_user
    runtime_password = settings.postgres_app_password

    # --- 1. the missing TRUNCATE guard ---------------------------------------
    # FOR EACH STATEMENT, not FOR EACH ROW: TRUNCATE does not fire row triggers,
    # which is exactly why the original pair of triggers did not catch it.
    op.execute(
        """
        CREATE TRIGGER audit_log_no_truncate
            BEFORE TRUNCATE ON audit_log
            FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_mutation();
        """
    )

    # --- 2. the least-privilege runtime role ---------------------------------
    # Idempotent so an existing volume can be upgraded in place.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{runtime_user}') THEN
                CREATE ROLE {runtime_user} LOGIN PASSWORD '{runtime_password}';
            ELSE
                ALTER ROLE {runtime_user} LOGIN PASSWORD '{runtime_password}';
            END IF;
        END
        $$;
        """
    )

    op.execute(f"GRANT CONNECT ON DATABASE {settings.postgres_db} TO {runtime_user};")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {runtime_user};")

    # Business tables: full read/write.
    for table in APP_WRITABLE_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {runtime_user};")

    # Audit log: append and read, nothing else. Note that revoking is not enough
    # on its own -- the triggers stay as defence in depth, and the role now
    # cannot drop them because it does not own the table.
    op.execute(f"GRANT SELECT, INSERT ON audit_log TO {runtime_user};")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM {runtime_user};")

    # Sequences (if any are added later) and future tables created by migrations
    # should be usable by the runtime role without another migration.
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {runtime_user};")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {runtime_user};"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {runtime_user};"
    )

    # Alembic's own bookkeeping table must stay owner-only: the app has no
    # business reading or writing migration state.
    op.execute(f"REVOKE ALL ON alembic_version FROM {runtime_user};")


def downgrade() -> None:
    runtime_user = settings.postgres_app_user

    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;")

    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {runtime_user};"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {runtime_user};"
    )
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {runtime_user};")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {runtime_user};")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {runtime_user};")
    op.execute(f"REVOKE CONNECT ON DATABASE {settings.postgres_db} FROM {runtime_user};")
    # The role itself is left in place: dropping a role that may own objects in
    # another database is not a safe thing for a downgrade to do silently.
