"""ticket table

Revision ID: c7d1e9a4b820
Revises: a1f2c3d4e5b6
Create Date: 2026-08-26 10:15:00.000000

Hand-checked after autogenerate. Two things worth noting for anyone copying
this as a template:

* The constraint and index names are not spelled out -- `op.f()` defers to the
  `NAMING_CONVENTION` in `app/core/db/base.py`, which is what keeps future
  autogenerate diffs stable.
* The `GRANT` is belt and braces. The hardening migration set
  `ALTER DEFAULT PRIVILEGES`, so a table created here by `appuser` is already
  writable by the runtime role. Granting explicitly means this migration also
  works against a database that predates that change, and it documents that a
  new table needs a decision about the least-privilege role rather than
  inheriting one silently.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "c7d1e9a4b820"
down_revision: str | None = "a1f2c3d4e5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ticket",
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("priority", sa.String(length=10), nullable=False),
        sa.Column("requester", sa.String(length=256), nullable=False),
        sa.Column("assignee", sa.String(length=256), nullable=True),
        sa.Column("attachment_key", sa.String(length=1024), nullable=True),
        sa.Column("attachment_name", sa.String(length=512), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket")),
    )
    op.create_index(op.f("ix_ticket_assignee"), "ticket", ["assignee"], unique=False)
    op.create_index(op.f("ix_ticket_priority"), "ticket", ["priority"], unique=False)
    op.create_index(op.f("ix_ticket_requester"), "ticket", ["requester"], unique=False)
    op.create_index(op.f("ix_ticket_status"), "ticket", ["status"], unique=False)

    runtime_user = settings.postgres_app_user
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{runtime_user}') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON ticket TO {runtime_user};
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ticket_status"), table_name="ticket")
    op.drop_index(op.f("ix_ticket_requester"), table_name="ticket")
    op.drop_index(op.f("ix_ticket_priority"), table_name="ticket")
    op.drop_index(op.f("ix_ticket_assignee"), table_name="ticket")
    op.drop_table("ticket")
