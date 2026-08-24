"""Object storage.

Import :func:`get_storage` and the interface types. Never import the adapter
directly -- depending on the :class:`Storage` interface is what keeps this
layer testable (a fake in unit tests) and replaceable later.
"""

from __future__ import annotations

import functools

from app.config import Settings, settings
from app.storage.base import (
    ObjectNotFoundError,
    Storage,
    StorageError,
    StoredObject,
)

__all__ = [
    "ObjectNotFoundError",
    "Storage",
    "StorageError",
    "StoredObject",
    "get_storage",
    "ping",
]


@functools.lru_cache(maxsize=1)
def get_storage(config: Settings | None = None) -> Storage:
    """Return the storage adapter (process-wide singleton)."""
    config = config or settings

    from app.storage.minio import MinioStorage

    return MinioStorage(config)


async def ping() -> bool:
    """Readiness probe for the object store."""
    return await get_storage().ping()
