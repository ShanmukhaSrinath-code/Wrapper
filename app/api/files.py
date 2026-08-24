"""File upload and download.

The split of responsibilities is the point of this module:

* **object store** holds the bytes,
* **Postgres** holds the metadata and the key,
* **audit** records who uploaded what, under which `request_id`.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from fastapi import APIRouter, File, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.audit import write_audit
from app.config import settings
from app.db.models.stored_file import StoredFile
from app.db.session import DbSession
from app.errors import NotFoundError, PayloadTooLargeError
from app.logging import get_logger
from app.security.current_user import CurrentUser
from app.storage import ObjectNotFoundError, get_storage

log = get_logger(__name__)

router = APIRouter(prefix="/files", tags=["files"])


class FileMetadata(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str | None
    uploaded_by: str
    created_at: datetime
    storage_key: str = Field(description="Key in the object store. The bytes are not in the DB.")

    model_config = {"from_attributes": True}


def _build_key(file_id: uuid.UUID, filename: str) -> str:
    """Date-partitioned key.

    The id is what makes the key unique; the filename is appended only so an
    operator browsing a bucket can recognise objects. Path separators are
    stripped so a crafted filename cannot escape its prefix.
    """
    safe = filename.replace("\\", "/").rsplit("/", 1)[-1][:200] or "upload"
    today = datetime.now().strftime("%Y/%m/%d")
    return f"uploads/{today}/{file_id}/{safe}"


@router.post(
    "",
    response_model=FileMetadata,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file",
)
async def upload(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    file: UploadFile = File(...),
) -> FileMetadata:
    """Store the bytes in the object store and the metadata (key only) in Postgres."""
    data = await file.read()

    if len(data) > settings.max_request_body_bytes:
        raise PayloadTooLargeError(
            f"File exceeds the {settings.max_request_body_bytes} byte limit.",
            detail={"size_bytes": len(data), "limit_bytes": settings.max_request_body_bytes},
        )

    file_id = uuid.uuid4()
    key = _build_key(file_id, file.filename or "upload")
    checksum = hashlib.sha256(data).hexdigest()
    content_type = file.content_type or "application/octet-stream"

    storage = get_storage()
    await storage.ensure_ready()
    stored = await storage.put(
        key,
        data,
        content_type=content_type,
        metadata={"uploaded_by": user.id, "file_id": str(file_id)},
    )

    row = StoredFile(
        id=file_id,
        storage_key=stored.key,
        filename=file.filename or "upload",
        content_type=content_type,
        size_bytes=stored.size,
        checksum_sha256=checksum,
        uploaded_by=user.id,
    )
    session.add(row)
    await session.flush()

    await write_audit(
        "file.uploaded",
        resource_type="file",
        resource_id=str(file_id),
        http_method=request.method,
        http_path=request.url.path,
        client_ip=request.client.host if request.client else None,
        detail={
            "filename": row.filename,
            "size_bytes": row.size_bytes,
            "content_type": content_type,
            "storage_key": stored.key,
            "checksum_sha256": checksum,
        },
    )

    return FileMetadata.model_validate(row)


@router.get("/{file_id}", response_model=FileMetadata, summary="Get file metadata")
async def get_metadata(file_id: uuid.UUID, session: DbSession, user: CurrentUser) -> FileMetadata:
    row = await session.scalar(select(StoredFile).where(StoredFile.id == file_id))
    if row is None:
        raise NotFoundError(f"No file with id {file_id}.")
    return FileMetadata.model_validate(row)


@router.get("/{file_id}/content", summary="Download file contents")
async def download(file_id: uuid.UUID, session: DbSession, user: CurrentUser) -> Response:
    """Stream the bytes back through the service."""
    row = await session.scalar(select(StoredFile).where(StoredFile.id == file_id))
    if row is None:
        raise NotFoundError(f"No file with id {file_id}.")

    try:
        data = await get_storage().get(row.storage_key)
    except ObjectNotFoundError as exc:
        # Metadata without an object means the two stores have diverged --
        # worth an explicit log rather than a bare 404.
        log.error("file.object_missing", file_id=str(file_id), storage_key=row.storage_key)
        raise NotFoundError(f"File {file_id} has no stored content.") from exc

    return Response(
        content=data,
        media_type=row.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{row.filename}"',
            "X-Checksum-SHA256": row.checksum_sha256 or "",
        },
    )


@router.get("/{file_id}/download-url", summary="Redirect to a presigned URL")
async def download_url(
    file_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> RedirectResponse:
    """Hand the client a time-limited URL so the bytes bypass this service."""
    row = await session.scalar(select(StoredFile).where(StoredFile.id == file_id))
    if row is None:
        raise NotFoundError(f"No file with id {file_id}.")

    url = await get_storage().presigned_url(row.storage_key)
    return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
