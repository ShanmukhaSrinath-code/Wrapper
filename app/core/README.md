# app/core — infrastructure

**Do not edit anything in this package to add a feature.** If adding a feature
requires a change here, that is a bug in the base, not in the feature.

| Module | What it owns |
|---|---|
| `config.py` | every setting, via Pydantic Settings; the only place the environment is read |
| `logging.py` | structlog pipeline, correlation contextvars |
| `discovery.py` | finds plugin routers, tasks and models — replaces every hand-maintained registry |
| `observability.py` | OpenTelemetry tracing, Prometheus metrics |
| `errors.py` | the single error schema, exception handlers, Sentry |
| `db/` | engine, session dependency, base model, migration guard |
| `cache/` | Redis client and cache-aside helpers (degrade on outage) |
| `storage/` | the `Storage` interface and its MinIO/S3 adapter |
| `audit/` | append-only audit writer and the actor contextvar |
| `jobs/` | the Celery app, correlation propagation, guarded `enqueue()` |
| `middleware/` | correlation, security headers, request size limit |
| `security/` | the auth seam (`get_current_user`) — auth itself is deferred |
| `api/` | infrastructure endpoints only: `/health/*`, `/docs`, `/redoc` |

## The one rule

`app.core` must never import from `app.services`. Infrastructure cannot depend
on business logic, or the base stops being reusable. An import-linter contract
in `pyproject.toml` enforces this; `make lint-imports` checks it.

Business logic goes in [`../services/`](../services/).
