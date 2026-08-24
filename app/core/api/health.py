"""Liveness and readiness endpoints, wired to Kubernetes probes.

``/health/live``  -- is the process up? Never touches a dependency, so a slow
                     database can never get the pod killed.
``/health/ready`` -- can the process serve traffic? Runs every registered
                     dependency check and fails with 503 if any is down.

Later phases register their own checks via :func:`register_readiness_check`;
this module never needs to know about Postgres, Redis or MinIO directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app.core.config import settings

router = APIRouter(prefix="/health", tags=["health"])

#: name -> async callable that raises (or returns False) when the dependency is down.
ReadinessCheck = Callable[[], Awaitable[bool]]
_CHECKS: dict[str, ReadinessCheck] = {}


def register_readiness_check(name: str, check: ReadinessCheck) -> None:
    """Register a dependency probe used by ``/health/ready``."""
    _CHECKS[name] = check


def registered_checks() -> tuple[str, ...]:
    return tuple(_CHECKS)


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    checks: dict[str, str]


@router.get("/live", response_model=LivenessResponse, summary="Liveness probe")
async def live() -> LivenessResponse:
    """Always 200 while the process can respond. No dependency I/O."""
    from app import __version__

    return LivenessResponse(service=settings.app_name, version=__version__)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"model": ReadinessResponse, "description": "A dependency is unavailable"}},
)
async def ready(response: Response) -> ReadinessResponse:
    """200 when every registered dependency answers, else 503."""
    results: dict[str, str] = {}

    async def run(name: str, check: ReadinessCheck) -> None:
        try:
            ok = await asyncio.wait_for(check(), timeout=3.0)
            results[name] = "ok" if ok else "error: check returned false"
        except TimeoutError:
            results[name] = "error: timeout after 3s"
        except Exception as exc:
            results[name] = f"error: {type(exc).__name__}: {exc}"

    if _CHECKS:
        await asyncio.gather(*(run(name, check) for name, check in _CHECKS.items()))

    healthy = all(v == "ok" for v in results.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ok" if healthy else "degraded",
        service=settings.app_name,
        checks=results,
    )
