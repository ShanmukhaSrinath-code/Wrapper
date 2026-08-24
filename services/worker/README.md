# worker — the Celery worker

Runs background jobs. **Shares the app image** rather than defining its own, so
tasks execute against byte-identical code and dependencies; only the command
differs.

| | |
|---|---|
| Image | `services/app/Dockerfile` (the same build) |
| Command | `celery -A app.core.jobs.celery_app:celery_app worker --without-banner` |
| Port | none (it consumes from Redis) |
| Depends on | redis (broker + result backend), postgres, minio |

`--without-banner` is deliberate: Celery's ASCII banner is the only thing the
worker prints that is not JSON, and Promtail cannot parse it into fields.

Tasks are **discovered**, not listed — anything under `app/services/` exposing a
`@celery_app.task` is registered. The worker and the API run the same discovery
pass, so their registries cannot diverge.
