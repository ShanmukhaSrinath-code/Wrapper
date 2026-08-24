"""A trivial model proving the DB wiring end to end.

Delete this when you start writing real business models -- it exists only so
migrations, the session dependency and the integration tests have something
concrete to exercise.
"""

from __future__ import annotations

from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Example(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "example"

    name: Mapped[str] = mapped_column(index=True)
    description: Mapped[str | None] = mapped_column(default=None)
