"""Example tasks.

Delete these when you write real ones. They exist to prove the plumbing:
correlation ids survive the hop into the worker, results come back, failures
are reported, and a task can reach Postgres and MinIO.

Task bodies are synchronous -- Celery workers are not an event loop. Where a
task needs the app's async helpers, it runs them with `asyncio.run` inside its
own loop rather than trying to share one with the API process.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.core.audit import write_audit
from app.core.jobs.celery_app import celery_app
from app.core.logging import current_request_id, current_trace_id, get_logger
from app.core.storage import get_storage

log = get_logger(__name__)


@celery_app.task(name="demo.process_upload", bind=True)
def process_upload(self: Any, file_id: str, storage_key: str) -> dict[str, Any]:
    """Pretend to post-process an uploaded file.

    Reads the object back out of MinIO, computes something cheap, and writes an
    audit row -- which lands under the *originating request's* id, not the
    task's, because `task_prerun` rebound the context.
    """
    log.info("process_upload.begin", file_id=file_id, storage_key=storage_key)

    async def _work() -> dict[str, Any]:
        data = await get_storage().get(storage_key)
        result = {
            "file_id": file_id,
            "storage_key": storage_key,
            "size_bytes": len(data),
            "line_count": data.count(b"\n") + 1,
            "processed_by_task": self.request.id,
        }
        await write_audit(
            "file.processed",
            resource_type="file",
            resource_id=file_id,
            detail=result,
        )
        return result

    result = asyncio.run(_work())
    log.info("process_upload.done", **result)
    return result


@celery_app.task(name="demo.slow_add", bind=True)
def slow_add(self: Any, a: int, b: int, delay_seconds: float = 2.0) -> dict[str, Any]:
    """A deliberately slow task, so `PENDING -> STARTED -> SUCCESS` is observable."""
    log.info("slow_add.begin", a=a, b=b, delay_seconds=delay_seconds)
    time.sleep(delay_seconds)
    result = {
        "a": a,
        "b": b,
        "sum": a + b,
        "task_id": self.request.id,
        # Echoed into the result so the smoke test can assert correlation
        # survived the hop without having to scrape worker logs.
        "request_id": current_request_id(),
        "trace_id": current_trace_id(),
    }
    log.info("slow_add.done", **result)
    return result


@celery_app.task(name="demo.always_fails")
def always_fails() -> None:
    """Fail on purpose, to prove failures are logged and reported with their ids."""
    raise RuntimeError("This task always fails, by design.")
