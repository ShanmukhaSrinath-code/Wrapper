"""Demo endpoints.

These exist to exercise the base -- cache, database, jobs, audit, errors -- and
to give the smoke test something to drive. **Delete this router when you start
writing real business logic.**
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app import cache
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
