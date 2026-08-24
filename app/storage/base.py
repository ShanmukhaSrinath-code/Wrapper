"""The object-storage interface.

Application code depends on :class:`Storage` and never on boto3 directly. That
keeps the object store faked in unit tests and replaceable without touching a
single call site.

The contract deals in **keys and bytes**. Blobs never go into Postgres; the
database stores the key, and the object store owns the content.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StoredObject:
    """What a successful upload produced."""

    key: str
    size: int
    content_type: str
    etag: str | None = None


class StorageError(Exception):
    """Any provider failure, normalised so callers need not know the backend."""


class ObjectNotFoundError(StorageError):
    """The requested key does not exist."""


class Storage(abc.ABC):
    """Content-addressed blob store."""

    @abc.abstractmethod
    async def ensure_ready(self) -> None:
        """Create the bucket/container if missing. Safe to call repeatedly."""

    @abc.abstractmethod
    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        """Store ``data`` at ``key``, overwriting any existing object."""

    @abc.abstractmethod
    async def get(self, key: str) -> bytes:
        """Return the object's bytes, or raise :class:`ObjectNotFoundError`."""

    @abc.abstractmethod
    async def delete(self, key: str) -> None:
        """Remove ``key``. Deleting a missing key is not an error."""

    @abc.abstractmethod
    async def exists(self, key: str) -> bool:
        """Whether ``key`` is present."""

    @abc.abstractmethod
    async def presigned_url(self, key: str, *, expires_in: int | None = None) -> str:
        """A time-limited URL a client can fetch directly, bypassing this service."""

    @abc.abstractmethod
    async def ping(self) -> bool:
        """Readiness probe for the backing store."""
