"""Metadata for an uploaded file.

The row stores the storage **key**, never the bytes. Blobs in Postgres bloat
backups, break streaming and make the object store pointless.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class StoredFile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "stored_file"

    #: Key in the object store. This is the only pointer to the content.
    storage_key: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    uploaded_by: Mapped[str] = mapped_column(String(256), index=True)
