"""MinIO / S3 adapter.

Uses boto3, so the same adapter works unchanged against MinIO locally and real
S3 in production -- only the endpoint and credentials differ.

boto3 is synchronous. Calls run in a worker thread via ``asyncio.to_thread`` so
they never block the event loop; wrapping a blocking SDK is cheaper and far
less fragile than adding a second async S3 library.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.logging import get_logger
from app.storage.base import ObjectNotFoundError, Storage, StorageError, StoredObject

log = get_logger(__name__)

#: Error codes MinIO/S3 use for "it isn't there".
_NOT_FOUND = {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}


class MinioStorage(Storage):
    """S3-compatible object storage."""

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._bucket = config.s3_bucket
        self._client = self._build_client(config.s3_endpoint_url)
        # Presigned URLs must be signed for the address the *browser* will use,
        # which inside compose is not the address this service talks to.
        self._public_client = (
            self._client
            if config.s3_public_endpoint_url == config.s3_endpoint_url
            else self._build_client(config.s3_public_endpoint_url)
        )

    def _build_client(self, endpoint: str) -> Any:
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=self._config.s3_access_key,
            aws_secret_access_key=self._config.s3_secret_key,
            region_name=self._config.s3_region,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},  # MinIO needs path-style
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=15,
            ),
        )

    # -- helpers ------------------------------------------------------------
    async def _call(self, fn: str, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(partial(getattr(self._client, fn), **kwargs))
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND:
                raise ObjectNotFoundError(kwargs.get("Key", self._bucket)) from exc
            raise StorageError(f"S3 {fn} failed: {code}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"S3 {fn} failed: {type(exc).__name__}") from exc

    # -- Storage ------------------------------------------------------------
    async def ensure_ready(self) -> None:
        try:
            await self._call("head_bucket", Bucket=self._bucket)
            return
        except (ObjectNotFoundError, StorageError):
            pass
        try:
            await asyncio.to_thread(partial(self._client.create_bucket, Bucket=self._bucket))
            log.info("storage.bucket.created", bucket=self._bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            # A concurrent starter (e.g. the worker) may have won the race.
            if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise StorageError(f"Could not create bucket {self._bucket}: {code}") from exc

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        result = await self._call(
            "put_object",
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata=metadata or {},
        )
        etag = str(result.get("ETag", "")).strip('"') or None
        log.info("storage.put", storage_key=key, size=len(data), content_type=content_type)
        return StoredObject(key=key, size=len(data), content_type=content_type, etag=etag)

    async def get(self, key: str) -> bytes:
        result = await self._call("get_object", Bucket=self._bucket, Key=key)
        body = await asyncio.to_thread(result["Body"].read)
        return bytes(body)

    async def delete(self, key: str) -> None:
        await self._call("delete_object", Bucket=self._bucket, Key=key)
        log.info("storage.delete", storage_key=key)

    async def exists(self, key: str) -> bool:
        try:
            await self._call("head_object", Bucket=self._bucket, Key=key)
        except ObjectNotFoundError:
            return False
        return True

    async def presigned_url(self, key: str, *, expires_in: int | None = None) -> str:
        expiry = expires_in or self._config.s3_presign_expiry_seconds
        return str(
            await asyncio.to_thread(
                partial(
                    self._public_client.generate_presigned_url,
                    "get_object",
                    Params={"Bucket": self._bucket, "Key": key},
                    ExpiresIn=expiry,
                )
            )
        )

    async def ping(self) -> bool:
        await self._call("head_bucket", Bucket=self._bucket)
        return True
