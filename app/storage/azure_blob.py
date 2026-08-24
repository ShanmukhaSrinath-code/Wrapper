"""SWAP POINT: Azure Blob Storage adapter.

Left as a stub on purpose so the local build carries no Azure SDK dependency.
Nothing in the application imports this module directly -- `get_storage()`
selects it when ``STORAGE_PROVIDER=azure_blob``, so enabling it is a config
change, not a refactor.

To implement::

    uv add azure-storage-blob azure-identity

then fill in the methods with ``BlobServiceClient`` (the async flavour from
``azure.storage.blob.aio``), mapping:

    bucket           -> container
    put/get/delete   -> upload_blob / download_blob / delete_blob
    presigned_url    -> generate_blob_sas + the blob URL
    ObjectNotFound   <- azure.core.exceptions.ResourceNotFoundError

Keep raising the `app.storage.base` error types so callers stay unchanged.
"""

from __future__ import annotations

from app.config import Settings
from app.storage.base import Storage, StoredObject


class AzureBlobStorage(Storage):
    """Not implemented. See the module docstring for the checklist."""

    def __init__(self, config: Settings) -> None:
        self._config = config
        raise NotImplementedError(
            "AzureBlobStorage is a stub. Install azure-storage-blob and implement "
            "it to use STORAGE_PROVIDER=azure_blob."
        )

    async def ensure_ready(self) -> None:
        raise NotImplementedError

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        raise NotImplementedError

    async def get(self, key: str) -> bytes:
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        raise NotImplementedError

    async def exists(self, key: str) -> bool:
        raise NotImplementedError

    async def presigned_url(self, key: str, *, expires_in: int | None = None) -> str:
        raise NotImplementedError

    async def ping(self) -> bool:
        raise NotImplementedError
