"""The append-only audit table.

Append-only is enforced *in the database* (see the migration's UPDATE/DELETE
trigger), not merely by convention in application code -- an audit log that the
application can quietly rewrite is not an audit log.

Every row carries the `request_id` and `trace_id` of the request that caused
it, which is what lets one id join an audit entry to its logs and its trace.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, UUIDPrimaryKeyMixin


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_log"

    # --- what happened ---
    action: Mapped[str] = mapped_column(String(128), index=True)
    outcome: Mapped[str] = mapped_column(String(32), default="success")

    # --- to what ---
    resource_type: Mapped[str | None] = mapped_column(String(128), default=None)
    resource_id: Mapped[str | None] = mapped_column(String(256), default=None)

    # --- by whom (the auth seam fills this; today it is the stub principal) ---
    actor_id: Mapped[str] = mapped_column(String(256), index=True)
    actor_roles: Mapped[str | None] = mapped_column(String(512), default=None)

    # --- correlation: the whole point ---
    request_id: Mapped[str | None] = mapped_column(String(128), default=None, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)

    # --- context ---
    http_method: Mapped[str | None] = mapped_column(String(16), default=None)
    http_path: Mapped[str | None] = mapped_column(String(512), default=None)
    client_ip: Mapped[str | None] = mapped_column(String(64), default=None)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)

    # Only created_at: an audit row is never updated, so `updated_at` would lie.
    created_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (Index("ix_audit_log_action_created_at", "action", "created_at"),)
