"""The `ticket` table.

Only the attachment *key* is stored, never the bytes -- same rule as
`StoredFile`. The status/priority columns are plain strings rather than a
Postgres ENUM: adding a value to an ENUM needs a migration and a lock, and the
set of ticket states is exactly the kind of thing that changes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.db.base import TimestampMixin, UUIDPrimaryKeyMixin


class Ticket(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ticket"

    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")

    #: One of `service.STATUSES`. Transitions are enforced in the service layer.
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    priority: Mapped[str] = mapped_column(String(10), default="normal", index=True)

    #: Set from the authenticated principal, not from the request body -- a
    #: client must not be able to file a ticket as somebody else.
    requester: Mapped[str] = mapped_column(String(256), index=True)
    assignee: Mapped[str | None] = mapped_column(String(256), default=None, index=True)

    #: Key in the object store. Null until something is attached.
    attachment_key: Mapped[str | None] = mapped_column(String(1024), default=None)
    attachment_name: Mapped[str | None] = mapped_column(String(512), default=None)

    resolution_note: Mapped[str | None] = mapped_column(Text, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
