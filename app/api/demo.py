"""Demo endpoints.

These exist to exercise the base -- cache, database, jobs, audit, errors -- and
to give the smoke test something to drive. **Delete this router when you start
writing real business logic.**
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app import cache
from app.audit import write_audit
from app.logging import current_request_id, current_trace_id
from app.security.current_user import CurrentUser

router = APIRouter(prefix="/demo", tags=["demo"])


class CachedResponse(BaseModel):
    key: str
    value: dict[str, Any]
    cache: str  # "HIT" or "MISS"
    computed_by: str


async def _expensive_computation(seed: int) -> dict[str, Any]:
    """Stand-in for real work: deliberately slow so caching is observable."""
    await asyncio.sleep(0.25)
    return {"seed": seed, "result": seed * seed, "unit": "square"}


@router.get("/cached", response_model=CachedResponse, summary="Cache-aside demo")
async def cached(
    user: CurrentUser,
    seed: int = Query(default=7, ge=0, le=10_000),
) -> CachedResponse:
    """Compute a value once, then serve it from Redis until the TTL expires.

    The response reports `HIT` or `MISS` explicitly rather than leaving the
    caller to infer it from latency.
    """
    key = f"demo:cached:{seed}"
    value, hit = await cache.get_or_set(key, lambda: _expensive_computation(seed))
    return CachedResponse(
        key=key,
        value=value,
        cache="HIT" if hit else "MISS",
        computed_by=user.id,
    )


@router.delete("/cached", summary="Invalidate a cached demo value")
async def invalidate(
    user: CurrentUser,
    seed: int = Query(default=7, ge=0, le=10_000),
) -> dict[str, Any]:
    removed = await cache.delete(f"demo:cached:{seed}")
    return {"invalidated": removed, "seed": seed, "by": user.id}


class AuditedResponse(BaseModel):
    audit_id: str | None
    action: str
    request_id: str | None
    trace_id: str | None


@router.post("/audited", response_model=AuditedResponse, summary="Write an audit row")
async def audited(
    request: Request, user: CurrentUser, note: str = Query(default="")
) -> AuditedResponse:
    """Append one audit row for this request.

    The correlation ids are not passed in -- `write_audit` reads them from the
    request context, which is what keeps every call site correlated by default.
    """
    entry = await write_audit(
        "demo.audited",
        actor=user,
        resource_type="demo",
        resource_id="audited",
        http_method=request.method,
        http_path=request.url.path,
        client_ip=request.client.host if request.client else None,
        detail={"note": note} if note else None,
    )
    return AuditedResponse(
        audit_id=str(entry.id) if entry else None,
        action="demo.audited",
        request_id=current_request_id(),
        trace_id=current_trace_id(),
    )
