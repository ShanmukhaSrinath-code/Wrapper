# Architecture

One deployable application, a worker that shares its image, and eight
infrastructure services. Each service lives in its own folder under
[`services/`](services/) with only its own config; [
`deploy/docker-compose.yml`](deploy/docker-compose.yml) is the single source of
truth for how they connect.

---

## The request path

```
                          ┌──────────────┐
   client ──── HTTP ─────▶│  app  :8000  │
                          │   FastAPI    │
                          └──┬───┬───┬───┘
                             │   │   │
          ┌──────────────────┘   │   └──────────────────┐
          ▼                      ▼                      ▼
   ┌─────────────┐        ┌─────────────┐        ┌─────────────┐
   │ postgres    │        │ redis       │        │ minio       │
   │   :5432     │        │   :6379     │        │ :9000/:9001 │
   │ data +      │        │ cache +     │        │ objects     │
   │ audit_log   │        │ job broker  │        │             │
   └─────────────┘        └──────┬──────┘        └─────────────┘
          ▲                      │ enqueue
          │                      ▼
          │               ┌─────────────┐
          └── audit ──────│   worker    │  (same image as app,
                          │   Celery    │   different command)
                          └─────────────┘

   observability (every process writes to stdout / OTLP):

   stdout ──▶ promtail ──▶ loki :3100 ────┐
   /metrics ◀── prometheus :9090 ─────────┼──▶ grafana :3001
   OTLP :4318 ──▶ tempo :3200 ────────────┘
   uncaught errors ──▶ sentry (external, optional)
```

## Services

| Service | Port(s) | Depends on | Config | Folder |
|---|---|---|---|---|
| **app** | 8000 | postgres, redis, minio | environment | [`services/app/`](services/app/) |
| **worker** | — | redis, postgres, minio | environment | [`services/worker/`](services/worker/) |
| **postgres** | 5432 | — | env + `init/*.sql` | [`services/postgres/`](services/postgres/) |
| **redis** | 6379 | — | CLI flags | [`services/redis/`](services/redis/) |
| **minio** | 9000, 9001 | — | environment | [`services/minio/`](services/minio/) |
| **loki** | 3100 | — | `loki-config.yaml` | [`services/loki/`](services/loki/) |
| **promtail** | — | loki, docker socket | `promtail-config.yaml` | [`services/promtail/`](services/promtail/) |
| **prometheus** | 9090 | app, loki | `prometheus.yml` | [`services/prometheus/`](services/prometheus/) |
| **grafana** | 3001 | prometheus, loki, tempo | `provisioning/`, `dashboards/` | [`services/grafana/`](services/grafana/) |
| **tempo** | 3200, 4318 | — | `tempo-config.yaml` | [`services/tempo/`](services/tempo/) |

Nine services start by default. **Tempo is opt-in** under the `tracing` profile:

```bash
docker compose -f deploy/docker-compose.yml up -d                    # 9 services
OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318 \
  docker compose -f deploy/docker-compose.yml --profile tracing up -d  # + tempo
```

Without Tempo, spans are still generated — the `trace_id` on a log line or an
error response is real — they are simply not stored.

---

## The correlation contract

One `request_id` joins every system. The middleware
([`app/core/middleware/correlation.py`](app/core/middleware/correlation.py))
accepts an inbound `X-Request-ID` (validated, so a hostile header cannot forge
log lines) or mints a UUID4, opens an OpenTelemetry span, and binds
`request_id`, `trace_id`, `span_id` **and the acting principal** into
contextvars.

Because they live in context rather than being passed around, nothing downstream
can forget them:

| Signal | How it is correlated | Where to look |
|---|---|---|
| **Logs** | every JSON line carries `request_id`/`trace_id`, including uvicorn, SQLAlchemy and Celery lines | `{service="app"} \| json \| request_id = "<id>"` |
| **Traces** | the middleware's span is the request's root span | Tempo, or *View trace* on a log line |
| **Audit** | `write_audit()` reads the ids **and the actor** from context | `SELECT * FROM audit_log WHERE request_id = '<id>'` |
| **Metrics** | labelled by route template/method/status **only** — ids would destroy cardinality | Prometheus / Grafana |
| **Errors** | every error response is `{error, message, request_id, trace_id}`; Sentry events carry both as tags | Sentry, filtered by `request_id` |
| **Jobs** | ids and actor ride on Celery message headers and are rebound in the worker | `docker logs cab-worker \| grep <id>` |

`make smoke` asserts every leg of this and exits non-zero if one is missing.

---

## Inside the app: core vs services

```
app/
  core/       infrastructure -- DO NOT EDIT to add a feature
  services/   business logic -- auto-discovered
  main.py     assembles the two
```

[`app/core/`](app/core/) owns config, logging, discovery, observability, errors,
db, cache, storage, audit, jobs, middleware, security and the health/docs
endpoints. [`app/services/`](app/services/) is the plugin seam.

**`app.core` may never import `app.services`.** Infrastructure that depends on
business logic is not reusable. An import-linter contract in `pyproject.toml`
enforces it and `make lint` runs it.

### Nothing is registered by hand

Three registries used to exist, and each failed silently when someone forgot it.
All three are now discovery
([`app/core/discovery.py`](app/core/discovery.py)), walking `PLUGIN_PACKAGES`
(default `app.services`):

| Drop in `app/services/` | Effect |
|---|---|
| a module-level `router = APIRouter(...)` | mounted on the app |
| a `@celery_app.task` | registered with the worker |
| a SQLAlchemy model | added to `Base.metadata`, seen by Alembic |

Discovery is **fatal on import errors**: a worker that boots with half its tasks
missing looks healthy and drops jobs, which is worse than not booting.

Two further guards close the failure modes that made the old registries
dangerous:

- `enqueue()` refuses a task name nobody registered, so the API cannot return
  `201` and a `task_id` for work that will never run;
- Alembic's `process_revision_directives` rejects any autogenerated
  `drop_table`/`drop_column` unless `ALLOW_DESTRUCTIVE=1`, so a missed model can
  never silently propose dropping its own table.

**Adding a feature therefore requires zero edits to `app/core/**` or to
`app/main.py`.**

---

## Data integrity

The audit log is append-only, enforced by the database and by privilege — not by
convention.

| Layer | What it stops |
|---|---|
| Grants | the runtime role has `INSERT, SELECT` on `audit_log` and nothing else |
| Ownership | the runtime role owns nothing, so it cannot `DROP TRIGGER` |
| Triggers | `BEFORE UPDATE`, `BEFORE DELETE` (row) and `BEFORE TRUNCATE` (statement) reject the operation even for the owner |

Two roles: **`appuser`** owns the schema and runs migrations; **`appruntime`** is
what the app and worker connect as. See
[`services/postgres/README.md`](services/postgres/README.md).

---

## Failure behaviour

| Dependency down | What happens |
|---|---|
| **Redis** | routes still return 200. Cache reads/writes catch connection and timeout errors, log `cache.unavailable`, and degrade to a miss. `/health/ready` returns 503 naming redis, so traffic drains. |
| **Postgres** | `/health/ready` returns 503 naming postgres; `/health/live` stays 200, so a slow database does not get healthy pods killed. |
| **MinIO** | `/health/ready` returns 503 naming storage. |
| **Tempo/Loki/Prometheus** | no effect on request handling — spans and logs are best-effort. |

`/health/live` never consults a dependency. `/health/ready` consults all of them.

---

## Swap points

| Seam | Today | Production |
|---|---|---|
| **Secrets** | `EnvSecrets` | `AzureKeyVaultSecrets` — set `SECRETS_PROVIDER=azure_key_vault` |
| **Identity** | `get_current_user()` returns a stub principal | Entra ID + Casbin — change one file, no route signature moves |
| **Object storage** | MinIO | real S3 — same adapter, different endpoint and credentials |

**Authentication is deliberately not implemented.** Every business route already
depends on `CurrentUser`, and the correlation middleware already binds whatever
`get_current_user()` returns into the audit context, so real auth drops in
without touching routes or audit call sites.
