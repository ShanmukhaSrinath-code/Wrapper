---
name: import-poc
description: Port a standalone POC/prototype FastAPI project living in another folder into this base's app/services/ structure, replacing its own plumbing with core seams. Use whenever asked to import, migrate, integrate, wire in, or "plug in" an external POC/prototype into this codebase.
---

# Importing a POC

A POC is a source of **business logic only**. Its own logging, auth, error
handling, DB session code, cache client, storage client, HTTP client,
Dockerfile, docker-compose, and observability all get **thrown away** — this
base already provides all of it. Porting is not copy-paste; it is
inventory → rewrite → verify.

Same law as `add-feature`: zero edits to `app/core/**`, zero edits to
`app/main.py`, zero edits to `deploy/docker-compose.yml`, zero edits to the
`Makefile`. If the port seems to require one of those, **stop and report it as
a decision, do not make it silently.**

## 0. Locate the POC and do the boundary scan FIRST

Before writing anything, read the POC's `requirements.txt`/`pyproject.toml`
and its settings/config module and check for infrastructure this base does
not already provide under a different name:

| Found in POC | What it means | Action |
|---|---|---|
| `redis`, `boto3`/`minio`, `httpx` used directly | just POC plumbing this base already has | discard, use core seam — not a decision |
| A **non-Postgres** DB driver (`pymongo`, `mysqlclient`, `asyncpg` pointed at a separate instance, raw `sqlite3`) | a second database engine | **STOP.** Report it. Do not add a second DB to `app/core/db` or `deploy/docker-compose.yml` without the user choosing that explicitly. Default recommendation: remodel the data as Postgres tables via `Base`, unless there's a hard reason (e.g. genuine document store need) — that's the user's call. |
| A **non-Celery** job runner, a **non-Redis** broker/cache, its own Grafana/Prometheus/Loki config | competing observability/infra | **STOP.** This base already ships Grafana/Loki/Prometheus/Tempo in `deploy/docker-compose.yml` regardless of what the POC has or lacks — never add a second copy. Report if the POC's dashboards/alerts contain logic worth preserving; otherwise they're dropped, not merged. |
| Its own auth (JWT issuing, session cookies, API keys) | a real auth mechanism | **STOP and ask.** `app/core/security/current_user.py` is a stub by design; wiring real auth is a `app/core/**` change and out of scope for a feature port. |
| A frontend folder (React/Vue/static) | out of this base's scope entirely | Leave it where it is or hand it back to the user; this base is API-only. Do not try to merge frontend tooling into this repo. Note in the report where it lives and that it should point its API base URL at this app, and that its origin needs adding to `CORS_ALLOW_ORIGINS`. |

Anything not in this table (plain REST routes, SQLAlchemy/Pydantic models,
background jobs, simple outbound calls) is a normal port — proceed.

## 1. Inventory

List, per POC module: routes (path + method), DB models, background jobs,
outbound HTTP calls, and anything reading env vars directly. This becomes the
checklist for step 3 — nothing should be ported twice, nothing dropped
silently without saying so in the final report.

## 2. Group into feature packages

One POC domain concept → one `app/services/<feature>/` package (not one
package per POC file). Follow the layout from the `add-feature` skill:

```
app/services/<feature>/
  __init__.py
  models.py
  router.py
  tasks.py
  service.py     # optional
```

If the POC is one flat `main.py`, split it along its actual domain nouns —
don't create `app/services/poc/` as a dumping ground.

## 3. Mechanical rewrite table

Apply every one of these while porting; do not leave a POC-native version
"for now":

| POC pattern | Replace with |
|---|---|
| `logging.getLogger(__name__)` / `print()` | `from app.core.logging import get_logger` |
| `raise HTTPException(404, ...)` (or any hand-rolled error JSON) | `app.core.errors.NotFoundError` / `ConflictError` / etc. |
| its own `redis.Redis(...)` client | `from app.core import cache` |
| `boto3.client("s3", ...)` or local disk writes | `from app.core.storage import get_storage` |
| `requests`/raw `httpx.Client()` calls | `from app.core.http import request` |
| its own Celery app, or a different task queue (RQ, arq, plain threads) | `app.core.jobs.celery_app` + `enqueue()` |
| no identity, or a bespoke `get_current_user` | `user: CurrentUser` on every route that needs one |
| manual `session.commit()`/`db.close()` | `DbSession` dependency — it commits/rolls back for you |
| its own request-id / correlation-id middleware | delete outright — `CorrelationMiddleware` already covers it |
| its own CORS/security-header middleware | delete — already in `app/core/middleware/security.py` |
| its own rate limiter | delete — already in `app/core/middleware/ratelimit.py`, tune via `RATE_LIMIT_*` env vars if the POC needs different limits |
| `pydantic.BaseSettings` reading its own env vars | fold feature-specific env vars into `app/core/config.py`'s `Settings` only if truly infra-level; otherwise keep them local constants in the feature package — do not create a second settings object |
| its own Dockerfile / requirements.txt | discarded — the feature ships inside the existing `app` image; add any *new* third-party dependency to this repo's `pyproject.toml` |

## 4. Models and migration

Point every model at `app.core.db.Base`. Then:

```bash
make revision m="import <feature> from POC"
make migrate
```

Read the generated migration. If it wants to `DROP` something, that almost
always means a model failed to import (check for a typo'd import path), not
that a real drop is warranted — do not set `ALLOW_DESTRUCTIVE=1` reflexively.

## 5. Tests

Port the POC's test intent, not its test code verbatim — its fixtures assume
its own app instance. Use `app_client` from `tests/conftest.py`:

```python
@pytest.mark.asyncio
async def test_<behavior>(app_client):
    response = await app_client.post("/<feature>", json={...})
    assert response.status_code == 201
```

Put unit tests in `tests/unit/test_<feature>.py`. If the POC's test asserted
against its own logging/error format, drop that assertion — the shape is now
whatever `app/core/errors.py` emits.

## 6. Verify, then report

```bash
make lint
make typecheck
make test
git status --porcelain
```

The diff should be exactly: the new `app/services/<feature>/` package(s), a
migration file, and tests. Anything else — especially inside `app/core/**`,
`app/main.py`, `deploy/docker-compose.yml`, or `Makefile` — means a boundary
was crossed; stop and call it out rather than committing it.

Finish with a short report, always including:

1. **Ported automatically** — routes/models/tasks that moved over cleanly.
2. **Dropped by design** — POC plumbing that this base already provides for
   free (its logging, correlation, CORS, rate limiting, etc.) — say what it
   was, so nothing looks silently missing.
3. **Needs your decision** — anything flagged in step 0 (a second DB, real
   auth, competing observability, a frontend) that was *not* acted on
   automatically.

## Anti-patterns to refuse

| Tempting | Do this instead |
|---|---|
| copy the POC's `docker-compose.yml` services in | nothing — this base's `deploy/docker-compose.yml` already has Postgres/Redis/MinIO/Grafana/Loki/Prometheus; a POC's copies are redundant or conflicting |
| add a Mongo/MySQL client because the POC had one | stop and ask — remodeling onto Postgres is the default, adding a second DB is the user's call |
| keep the POC's own `/healthz` or logging middleware "just in case" | delete it — `app/core/api/health.py` and `CorrelationMiddleware` already exist |
| merge a POC frontend into this repo | leave it out; this base is API-only |
| silently skip a piece that didn't map cleanly | say so explicitly in the final report |
