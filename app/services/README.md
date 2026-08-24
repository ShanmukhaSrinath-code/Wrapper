# app/services — the plugin seam

**This is where your code goes.** Everything here is discovered automatically.

| Drop in | And you get |
|---|---|
| a module with a `router = APIRouter(...)` | mounted on the app, no edit to `main.py` |
| a `@celery_app.task` | registered with the worker, no list to update |
| a SQLAlchemy model | included in `Base.metadata`, visible to Alembic autogenerate |

Discovery walks `PLUGIN_PACKAGES` (default `app.services`) recursively, so a
feature can be one module or a package with `router.py`, `tasks.py`, `models.py`.

## What you inherit for free

Correlation (`request_id`/`trace_id` on every log line, span and audit row),
structured logging, tracing, Prometheus metrics, the standard error schema,
security headers, the audit trail with the acting principal already resolved,
health probes, and CI.

## Two conventions worth keeping

- Take `user: CurrentUser` on any route that will ever need an identity, so real
  auth drops in with no signature changes.
- Raise `AppError` subclasses (`NotFoundError`, `ConflictError`, …) for expected
  failures. They become the right status code in the standard error shape, are
  logged as warnings, and are not sent to Sentry — a 404 is not a bug.

## What you must not do

Import from a feature into `app.core`. Infrastructure must not depend on
business logic; the import-linter contract will fail the build.

`demo.py`, `demo_tasks.py` and `files.py` are examples. Delete them when you
start writing real features — the base does not need them.
