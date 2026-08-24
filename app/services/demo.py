"""Demo endpoints.

These exist to exercise the base -- cache, database, jobs, audit, errors -- and
to give the smoke test something to drive. **Delete this router when you start
writing real business logic.**
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.core import cache
from app.core.audit import write_audit
from app.core.errors import NotFoundError
from app.core.jobs import enqueue
from app.core.logging import current_request_id, current_trace_id
from app.core.security.current_user import CurrentUser

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


@router.get("/boom", summary="Raise an unhandled exception on purpose")
async def boom(user: CurrentUser) -> dict[str, Any]:
    """Deliberately fail, to prove errors are handled, correlated and reported.

    Raises a plain `ZeroDivisionError` -- an *unexpected* failure, so it takes
    the `internal_error` path: 500, no stack trace to the caller, logged with
    its request_id, and captured by Sentry when a DSN is configured.
    """
    await write_audit("demo.boom", resource_type="demo", outcome="failure")
    return {"never": 1 / 0}


@router.get("/not-found", summary="Raise an expected business error")
async def not_found(user: CurrentUser) -> dict[str, Any]:
    """An *expected* failure: 404 in the same error schema, warned not paged."""
    raise NotFoundError("No such demo resource.", detail={"looked_for": "nothing"})


class JobAccepted(BaseModel):
    task_id: str
    status: str
    request_id: str | None


class JobStatus(BaseModel):
    task_id: str
    status: str = Field(description="PENDING, STARTED, SUCCESS, FAILURE, ...")
    result: Any | None = None
    error: str | None = None


@router.post("/job", response_model=JobAccepted, status_code=202, summary="Enqueue a job")
async def enqueue_job(
    user: CurrentUser,
    a: int = Query(default=2),
    b: int = Query(default=3),
    delay_seconds: float = Query(default=2.0, ge=0, le=30),
    fail: bool = Query(default=False, description="Enqueue a task that always fails."),
) -> JobAccepted:
    """Enqueue and return immediately.

    The correlation ids ride along on the message headers, so the worker logs
    under this request's `request_id` -- see app/jobs/celery_app.py.
    """
    # `enqueue` refuses names nobody registered, so this route cannot return a
    # task id for work that will never run.
    task = enqueue("demo.always_fails") if fail else enqueue("demo.slow_add", a, b, delay_seconds)
    return JobAccepted(task_id=task.id, status="queued", request_id=current_request_id())


@router.get("/job/{task_id}", response_model=JobStatus, summary="Poll job status")
async def job_status(task_id: str, user: CurrentUser) -> JobStatus:
    """Return the task's state and, once finished, its result or error."""
    from celery.result import AsyncResult

    from app.core.jobs import celery_app

    async_result = AsyncResult(task_id, app=celery_app)
    state = async_result.state

    if state == "FAILURE":
        return JobStatus(task_id=task_id, status=state, error=str(async_result.result))
    if state == "SUCCESS":
        return JobStatus(task_id=task_id, status=state, result=async_result.result)
    return JobStatus(task_id=task_id, status=state)
