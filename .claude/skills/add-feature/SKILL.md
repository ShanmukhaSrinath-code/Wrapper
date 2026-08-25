---
name: add-feature
description: Add a new business feature to this base — router, DB model and/or background task — using the plugin seam so zero infrastructure files are edited. Use whenever the request is to build, add or extend a feature, endpoint, table, or background job in this repo.
---

# Adding a feature

Everything lives in **one new package** under `app/services/`. You do not touch
`app/core/**` and you do not touch `app/main.py`. If it feels like you have to,
stop and re-read this file — the seam you need almost certainly exists.

## 1. Create the package

```
app/services/<feature>/
  __init__.py     # docstring only
  models.py       # SQLAlchemy models      (skip if no table)
  router.py       # module-level `router`  (skip if no endpoints)
  tasks.py        # @celery_app.task       (skip if no background work)
  service.py      # optional: logic worth separating from the router
```

Use a **package**, not a single module, as soon as the feature has more than one
concern. Naming is plain: `app/services/invoices/`, not `app/services/inv_mgr/`.

## 2. The model

```python
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.db.base import TimestampMixin, UUIDPrimaryKeyMixin


class Invoice(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "invoice"

    number: Mapped[str] = mapped_column(nullable=False, index=True)
    total_cents: Mapped[int] = mapped_column(nullable=False)
    # Large content belongs in the object store; keep the key, not the bytes.
    document_key: Mapped[str | None] = mapped_column(default=None)
```

No registry to update — discovery imports it and Alembic sees it.

## 3. The router

`prefix` must not collide with a base path (`/health`, `/metrics`, `/docs`,
`/redoc`, `/openapi.json`).

```python
from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.core.audit import write_audit
from app.core.db.session import DbSession
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.security.current_user import CurrentUser
from app.services.invoices.models import Invoice

log = get_logger(__name__)
router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(request: Request, session: DbSession, user: CurrentUser, ...):
    row = Invoice(...)
    session.add(row)
    await session.flush()          # the dependency commits on success

    await write_audit(
        "invoice.created",
        resource_type="invoice",
        resource_id=str(row.id),
        http_method=request.method,
        http_path=request.url.path,
    )
    return ...


@router.get("/{invoice_id}")
async def get_one(invoice_id: uuid.UUID, session: DbSession, user: CurrentUser):
    row = await session.scalar(select(Invoice).where(Invoice.id == invoice_id))
    if row is None:
        raise NotFoundError(f"No invoice with id {invoice_id}.")
    return ...
```

Notes that matter:

- **`user: CurrentUser` on anything that will ever need an identity.** Auth is a
  stub now; taking the dependency today means no signature changes later.
- **`raise NotFoundError(...)`, never `HTTPException`** — the `AppError` family
  becomes the standard error shape, is logged as a warning, and is not sent to
  Sentry.
- **Do not pass an actor to `write_audit`.** It reads the principal and the
  correlation ids from context. Passing one is only for system/impersonated
  principals.
- **Do not commit the session yourself.** `DbSession` commits on success and
  rolls back on exception.

## 4. The task

```python
from typing import Any

from app.core.jobs.celery_app import celery_app
from app.core.logging import get_logger

log = get_logger(__name__)


@celery_app.task(name="invoices.render_pdf", bind=True)
def render_pdf(self: Any, invoice_id: str) -> dict[str, Any]:
    ...
```

- **Task bodies are synchronous.** A Celery worker is not an event loop. To use
  the async helpers, wrap them: `asyncio.run(_work())` — see
  `app/services/demo_tasks.py`.
- **Name tasks `<feature>.<verb>`.** The name is the contract.
- **Retries are inherited.** Transient failures (`ConnectionError`,
  `TimeoutError`, `OSError`, SQLAlchemy `OperationalError`, botocore errors)
  retry with exponential backoff and jitter, capped. Do not add
  `autoretry_for` unless this task genuinely differs.
- **Enqueue with `enqueue("invoices.render_pdf", str(id))`**, not
  `task.delay()`. `enqueue` verifies the task is registered first, so a route
  cannot return `201` for work that will never run.
- Correlation crosses the process hop automatically: the task logs under the
  originating request's `request_id`, and audit rows written inside it are
  attributed to the caller.

## 5. Cache and object storage, when needed

```python
from app.core import cache
from app.core.storage import get_storage

value, hit = await cache.get_or_set(f"invoice:{id}", lambda: _load(id))
stored = await get_storage().put(f"invoices/{id}.pdf", data, content_type="application/pdf")
```

A Redis outage degrades to a **miss**, not a 500 — that is already handled, do
not add your own try/except around cache calls. Never import `redis` or `boto3`
in a feature.

## 6. Migrate

```bash
make revision m="add invoice table"
make migrate
```

Read the generated migration before applying it. If autogenerate refuses with
`DestructiveMigrationError`, do **not** set `ALLOW_DESTRUCTIVE=1` reflexively —
the usual cause is a model that failed to import, which makes its table look
deleted.

## 7. Test

Put tests in `tests/unit/test_<feature>.py`. For anything that drives HTTP, use
the `app_client` fixture from `tests/conftest.py` — it runs the real app
in-process over ASGI, so coverage sees it.

```python
@pytest.mark.asyncio
async def test_create_invoice(app_client):
    response = await app_client.post("/invoices", json={...})
    assert response.status_code == 201
```

Integration tests that need the live stack take `@pytest.mark.integration` and
skip with a reason when it is down.

## 8. Verify before claiming done

```bash
make lint       # ruff + format + the core/services boundary
make typecheck
make test       # 80% coverage gate
```

Then prove the seam held:

```bash
git status --porcelain
```

The only changes should be your feature package, its migration, and its tests.
**Any `app/core/**` or `app/main.py` edit means something went wrong** — say so
rather than committing it.

## Anti-patterns to refuse

| Tempting | Do this instead |
|---|---|
| add the router to `app/main.py` | nothing — `discover_routers()` mounts it |
| add the model to a models `__init__` | nothing — discovery imports it |
| add the task to a Celery `include` list | nothing — `autodiscover` finds it |
| `raise HTTPException(404)` | `raise NotFoundError(...)` |
| `write_audit(..., actor="someone")` | omit it; context supplies the actor |
| `try/except` around a cache call | nothing — outages already degrade to a miss |
| `import boto3` in a feature | `from app.core.storage import get_storage` |
| `logging.getLogger(__name__)` | `from app.core.logging import get_logger` |
| a second `app/core/` module "just for this" | put it in the feature package |
