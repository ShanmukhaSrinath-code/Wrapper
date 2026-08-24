"""Object storage.

Import :func:`get_storage` and the interface types. Never import an adapter
directly -- that is what keeps the provider swappable.
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
    """Return the configured storage adapter (process-wide singleton)."""
    config = config or settings

    if config.storage_provider == "azure_blob":
        from app.storage.azure_blob import AzureBlobStorage

        return AzureBlobStorage(config)

    from app.storage.minio import MinioStorage

    return MinioStorage(config)


async def ping() -> bool:
    """Readiness probe for the configured object store."""
    return await get_storage().ping()
