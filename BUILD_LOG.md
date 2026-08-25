# BUILD LOG — Common Application Base

Every phase records what was built, the exact verification command, and the real
output of its Acceptance Gate.

## Phase 0 — Scaffold & tooling — **PASS**

**Built:** target repo tree; `pyproject.toml` (uv, Python 3.12, ruff + mypy +
pytest + coverage config); `Makefile` (`install run test lint up down smoke` and
more); `.env.example`; `.gitignore`; `.pre-commit-config.yaml`;
`app/config.py` with Pydantic `Settings` plus the `Secrets` interface
(`EnvSecrets` now, `AzureKeyVaultSecrets` stub as the Key Vault swap point);
`README.md`.

**Environment note:** the host had no `uv` and no `make`. Installed
`uv 0.12.5` (pip) and `GNU Make 4.4.1` (winget `ezwinports.make`). Registry
writes are blocked in this sandbox, so their directories must be added to
`PATH` manually — see README *Prerequisites*.

### Gate 1 — `make install && make lint`

```
$ make install
uv python install 3.12
Installed Python 3.12.14 in 8.79s
uv sync --all-groups
 + fastapi==0.121.3 ... + sqlalchemy==2.0.52 + structlog==26.1.0 + uvicorn==0.52.4
 (139 packages installed)

$ make lint
uv run ruff check .
All checks passed!
uv run ruff format --check .
16 files already formatted
```

### Gate 2 — settings import cleanly

```
$ uv run python -c "import app.config; ..."
python 3.12.14
app_name = common-app-base
database_url = postgresql+asyncpg://appuser:apppassword@localhost:5432/appdb
redis_url = redis://localhost:6379/0
secrets provider = EnvSecrets
```

## Phase 1 — FastAPI skeleton + health — **PASS**

**Built:** `app/main.py` app factory (`create_app`) with a lifespan hook;
`app/api/health.py` exposing `/health/live` (no dependency I/O, so a slow
database can never get a pod killed) and `/health/ready`, which runs a
*registry* of dependency checks — later phases call
`register_readiness_check("postgres", ...)` and this module never learns about
Postgres/Redis/MinIO directly; `app/security/current_user.py`, the auth seam:
a `Principal` model, a `STUB_PRINCIPAL` (`id="dev"`, `roles=["dev"]`), and a
`CurrentUser` dependency alias with the `TODO: replace with Entra ID + Casbin`.

### Gate — health endpoints and OpenAPI

```
$ make run
$ curl -s -w "\nHTTP %{http_code}\n" localhost:8000/health/live
{"status":"ok","service":"common-app-base","version":"0.1.0"}
HTTP 200

$ curl -s -w "\nHTTP %{http_code}\n" localhost:8000/health/ready
{"status":"ok","service":"common-app-base","checks":{}}
HTTP 200

$ curl -s -o /dev/null -w "HTTP %{http_code}\n" localhost:8000/docs
HTTP 200

$ curl -s localhost:8000/openapi.json | head -c 120
{"openapi":"3.1.0","info":{"title":"common-app-base","description":"Common Application Base — clone this and add busin
```

### Gate — the auth seam resolves

```
$ uv run python -c "from app.security.current_user import get_current_user; ..."
principal: {'id': 'dev', 'name': 'Local Developer', 'roles': ['dev'], 'tenant_id': None}
has_role(dev): True
```

## Phase 2 — Docker + compose — **PASS**

**Built:** `deploy/docker/Dockerfile` — multi-stage (`builder` resolves deps
into a self-contained `/app/.venv` with `uv sync --frozen`, `runtime` copies
that venv onto `python:3.12-slim-bookworm`). Dependencies are copied before
source so a code edit never invalidates the dependency layer. Runs as the
non-root `app` user (uid 1001) and carries a `HEALTHCHECK` pointed at
`/health/live`. Plus `.dockerignore` and
`deploy/compose/docker-compose.yml` with the `app` service.

### Gate — health checks pass **against the container**

```
$ docker compose -f deploy/compose/docker-compose.yml up -d --build
 Image common-app-base:local Built
 Container cab-app Started

$ docker compose ps
NAME      IMAGE                   SERVICE   STATUS
cab-app   common-app-base:local   app       Up 10 seconds (healthy)   0.0.0.0:8000->8000/tcp

$ curl -s -w "\nHTTP %{http_code}\n" localhost:8000/health/live
{"status":"ok","service":"common-app-base","version":"0.1.0"}
HTTP 200

$ curl -s -w "\nHTTP %{http_code}\n" localhost:8000/health/ready
{"status":"ok","service":"common-app-base","checks":{}}
HTTP 200

$ docker exec cab-app id
uid=1001(app) gid=1001(app) groups=1001(app)      # non-root

$ docker images common-app-base:local
common-app-base:local  412MB
```

## Phase 3 — PostgreSQL + migrations — **PASS**

**Built:** `postgres:16-alpine` in compose (healthcheck + named volume);
`app/db/base.py` (`DeclarativeBase` with an explicit constraint naming
convention so autogenerate emits stable names, plus `UUIDPrimaryKeyMixin` /
`TimestampMixin`); `app/db/session.py` (lazily-built async engine with
`pool_pre_ping`, the `DbSession` dependency that commits on success and rolls
back on error, `ping()` and `dispose_engine()`); `app/db/models/example.py`;
Alembic (`alembic.ini`, `migrations/env.py` reading the DSN from
`app.config.settings` — never from the ini — and running through the async
engine) and the initial migration. `/health/ready` now pings Postgres.

### Gate 1 — `alembic upgrade head`

```
$ make revision m="initial example table"
INFO  [alembic.autogenerate.compare.tables] Detected added table 'example'
INFO  [alembic.autogenerate.compare.constraints] Detected added index 'ix_example_name' on '('name',)'
Generating migrations/versions/20260824_1132_initial_example_table.py ... done

$ make migrate
INFO  [alembic.runtime.migration] Running upgrade  -> 375b31581a92, initial example table
```

### Gate 2 — scripted insert + select round-trips

```
ping(): True
inserted id  : e6163c01-2ac3-45dc-a258-56ac25cb378b
selected id  : e6163c01-2ac3-45dc-a258-56ac25cb378b
name         : phase-3-check
description  : scripted insert+select
created_at   : 2026-08-24 06:03:03.935775+00:00
round-trip OK: True
```

### Gate 3 — readiness reports the DB, and *actually* fails when it is down

```
$ curl localhost:8000/health/ready
{"status":"ok","service":"common-app-base","checks":{"postgres":"ok"}}   HTTP 200

$ docker stop cab-postgres && curl localhost:8000/health/ready
{"status":"degraded","service":"common-app-base","checks":{"postgres":"error: timeout after 3s"}}
HTTP 503

$ curl localhost:8000/health/live          # liveness must NOT follow the DB down
{"status":"ok","service":"common-app-base","version":"0.1.0"}            HTTP 200

$ docker start cab-postgres && curl localhost:8000/health/ready
{"status":"ok","service":"common-app-base","checks":{"postgres":"ok"}}   HTTP 200
```

## Phase 4 — Redis + caching — **PASS**

**Built:** `redis:7-alpine` in compose; `app/cache/client.py` with a lazily
created shared async client, `get_json`/`set_json`/`delete`, and the
cache-aside helper `get_or_set(key, producer, ttl)` which returns
`(value, hit)` — so the caller reports HIT/MISS as fact rather than inferring
it from latency. A poisoned (non-JSON) key is deleted and treated as a miss
instead of raising. `app/api/demo.py` exposes `GET /demo/cached` and
`DELETE /demo/cached`, both already depending on the `CurrentUser` auth seam.
`/health/ready` now pings Redis too.

### Gate 1 — first call MISS, subsequent calls HIT

```
$ curl -X DELETE "localhost:8000/demo/cached?seed=7"
{"invalidated":0,"seed":7,"by":"dev"}

$ curl "localhost:8000/demo/cached?seed=7"        # call 1
{"key":"demo:cached:7","value":{"seed":7,"result":49,"unit":"square"},"cache":"MISS","computed_by":"dev"} | 0.256321s

$ curl "localhost:8000/demo/cached?seed=7"        # call 2
{"key":"demo:cached:7","value":{"seed":7,"result":49,"unit":"square"},"cache":"HIT","computed_by":"dev"}  | 0.012599s

$ curl "localhost:8000/demo/cached?seed=7"        # call 3
{"key":"demo:cached:7","value":{"seed":7,"result":49,"unit":"square"},"cache":"HIT","computed_by":"dev"}  | 0.004114s
```

Latency corroborates the reported flag: 256 ms computed → 4 ms served.

### Gate 2 — the value really is in Redis, with a TTL

```
$ docker exec cab-redis redis-cli KEYS 'demo:*'
demo:cached:7
$ docker exec cab-redis redis-cli GET 'demo:cached:7'
{"seed": 7, "result": 49, "unit": "square"}
$ docker exec cab-redis redis-cli TTL 'demo:cached:7'
48
```

### Gate 3 — readiness shows Redis, and fails when it is down

```
$ curl localhost:8000/health/ready
{"status":"ok","service":"common-app-base","checks":{"redis":"ok","postgres":"ok"}}

$ docker stop cab-redis && curl localhost:8000/health/ready
{"status":"degraded","service":"common-app-base","checks":{"postgres":"ok","redis":"error: timeout after 3s"}}

$ docker start cab-redis && curl localhost:8000/health/ready
{"status":"ok","service":"common-app-base","checks":{"redis":"ok","postgres":"ok"}}
```

## Phase 5 — Structured logging + Correlation + Loki/Grafana — **PASS**

**Built:** `app/logging.py` (structlog → JSON, correlation ids held in
`contextvars` and merged into every line, stdlib loggers — uvicorn, SQLAlchemy,
Celery — routed through the same pipeline so *their* lines carry the ids too);
`app/middleware/correlation.py`, the Correlation Contract as one pure-ASGI
middleware added outermost; `app/observability.py` (TracerProvider installed
even with no exporter, so `trace_id` is always real); `loki` + `promtail` +
`grafana` in compose with the Loki and Prometheus datasources provisioned.

**Bug found and fixed during the gate.** The first run emitted
double-encoded logs — the whole JSON object nested inside `"event"` — because
structlog rendered the line *and* the stdlib `ProcessorFormatter` rendered it
again. Fixed by ending structlog's chain with
`ProcessorFormatter.wrap_for_formatter` so exactly one render happens. Caught
only because the gate inspected the actual bytes rather than trusting that
logging "worked".

**Port note.** Host port 3000 was already taken by an unrelated `open-webui`
container, so Grafana publishes on **3001**, overridable via `GRAFANA_PORT`.

### Gate 1 — `X-Request-ID` returned, propagated, and validated

```
$ curl -D - "localhost:8000/demo/cached?seed=42"
HTTP/1.1 200 OK
x-request-id: 9bc89a98-f1b1-443c-b755-664ea77e740e
x-trace-id:   b76729e40172cfcde80507c6de4ce4f3

$ curl -H "X-Request-ID: my-own-id-12345" ...      # inbound id honoured
x-request-id: my-own-id-12345

$ curl -H 'X-Request-ID: bad;id$(injection)' ...   # bad id rejected, fresh UUID minted
x-request-id: 8b53476d-4ff6-4cb5-bce2-3a8c5cc493b8
```

### Gate 2 — every log line is JSON and carries `request_id`

```json
{
  "http_method": "GET", "http_path": "/demo/cached", "http_status": 200,
  "duration_ms": 262.3, "client_ip": "172.19.0.1",
  "event": "request.completed",
  "request_id": "2c42c583-0453-42ed-af83-e34d15679744",
  "span_id":  "8128a2859e401161",
  "trace_id": "36ad1040e7ad5b41d35aba81d0929172",
  "level": "info", "logger": "app.middleware.correlation",
  "timestamp": "2026-08-24T06:13:08.941990Z",
  "service": "common-app-base", "environment": "local"
}
```

### Gate 3 — filter Loki by that `X-Request-ID` and get the request's lines

```
$ curl -G localhost:3100/loki/api/v1/query_range \
    --data-urlencode 'query={service="app"} | json | request_id = `2c42c583-...`'
status: success
streams matched: 1
  line: {"http_method": "GET", ..., "request_id": "2c42c583-0453-42ed-af83-e34d15679744", ...}
```

### Gate 4 — the same query *through Grafana* (Explore → Loki equivalent)

```
$ curl -u admin:admin localhost:3001/api/datasources
  Loki         type=loki         uid=loki         url=http://loki:3100
  Prometheus   type=prometheus   uid=prometheus   url=http://prometheus:9090

$ curl -u admin:admin -G localhost:3001/api/datasources/proxy/uid/loki/loki/api/v1/query_range \
    --data-urlencode 'query={service="app"} | json | request_id = `2c42c583-...`'
status: success | streams: 1
  request_id=2c42c583-0453-42ed-af83-e34d15679744 trace_id=36ad1040e7ad5b41d35aba81d0929172 event=request.completed status=200
```

## Phase 6 — Metrics (Prometheus) + Grafana dashboard — **PASS**

**Built:** `configure_metrics()` exposing `/metrics` via
`prometheus-fastapi-instrumentator`; `prom/prometheus:v3.1.0` in compose
scraping `app:8000` every 15s; a provisioned Grafana dashboard
(`common-app-base`) with request rate, error rate, latency p50/p95/p99,
responses by status, in-flight, total, target-up — **plus a `request_id`
textbox variable driving a Loki logs panel**, so metrics and logs sit on one
screen keyed by the same id.

**Cardinality note.** Metrics are labelled `handler`/`method`/`status` only.
Correlation ids are deliberately *not* labels — they are unbounded and would
blow up cardinality. Ids live in logs and traces; metrics stay aggregate.
`should_ignore_untemplated=True` likewise keeps unrouted 404 paths from
creating a series per URL.

**Bug found and fixed during the gate.** Metrics first appeared as
`app_http_http_requests_total` — namespace `app` + subsystem `http` on top of
library names that already begin with `http_`. Dropped the subsystem.

### Gate 1 — `/metrics` returns Prometheus text

```
$ curl -s localhost:8000/metrics | grep ^app_http
app_http_requests_total{handler="/demo/cached",method="GET",status="200"} 25.0
app_http_request_duration_seconds_bucket{handler="/demo/cached",le="0.1",method="GET"} 20.0
app_http_request_duration_seconds_bucket{handler="/demo/cached",le="0.5",method="GET"} 25.0
app_http_request_duration_seconds_bucket{handler="/demo/cached",le="+Inf",method="GET"} 25.0
```

### Gate 2 — Prometheus Targets shows the app UP

```
$ curl -s "localhost:9090/api/v1/targets?state=active"
  common-app-base      http://app:8000/metrics          health=UP  lastError=-
  loki                 http://loki:3100/metrics         health=UP  lastError=-
  prometheus           http://localhost:9090/metrics    health=UP  lastError=-
```

### Gate 3 — the dashboard shows traffic after generating requests

272 requests over ~50s spanning several scrape intervals:

```
=== request rate by handler ===
  /demo/cached           1.295 req/s
=== latency percentiles ===
  p50 = 0.0507s
  p95 = 0.0964s
  p99 = 0.2280s
=== by status code ===
  status 200: 1.295 req/s
```

### Gate 4 — dashboard is provisioned and its panels query successfully

```
$ curl -u admin:admin "localhost:3001/api/search?type=dash-db"
  uid=common-app-base    title=Common Application Base — Service Overview  folder=Common Application Base

  [timeseries] Request rate (req/s by route)
  [timeseries] Error rate (5xx as % of all requests)
  [timeseries] Latency p50 / p95 / p99
  [timeseries] Responses by status code
  [stat      ] Requests in flight / Total requests (30m) / Target up
  [logs      ] Logs for $request_id (blank = all app logs)

$ curl -u admin:admin .../api/datasources/proxy/uid/prometheus/api/v1/query
  status: success
  /demo/cached: 1.295 req/s
```

## Phase 7 — OpenTelemetry tracing + Audit — **PASS**

**Built:** OTel auto-instrumentation for FastAPI, SQLAlchemy (instrumented in
`lifespan`, since the engine is built lazily) and Redis; `app/audit/` with the
`audit_log` model and `write_audit()`; `POST /demo/audited`.

Two design points worth stating:

- **`write_audit` takes no ids.** It reads `request_id`/`trace_id` out of the
  request context, so a new call site is correlated by default and no caller
  can forget to pass them.
- **Audit rows are written in their own transaction.** Sharing the request
  session would mean a business rollback erased the record of the attempt —
  precisely the case an audit trail exists to capture. `write_audit` also never
  raises: a failed audit is logged at error, it does not 500 a successful
  operation.

**Also added:** `grafana/tempo` under an opt-in `tracing` compose profile plus
a Tempo datasource with logs↔traces links in both directions. It is *not* in
the default service set, so a plain `docker compose up` still brings up exactly
the specified services — but the Phase 14 "a trace exists" assertion is now
provable against a real trace store rather than asserted from a header.

### Gate 1 — log lines carry a real `trace_id`

```
$ curl -G localhost:3100/loki/api/v1/query_range \
    --data-urlencode 'query={service="app"} | json | trace_id = `97e12b15a8e0118645d79f5ec95c43b0`'
  lines matched: 2
    06:22:13.726643Z  event=audit.written      request_id=85a140f5-...  trace_id=97e12b15a8e0118645d79f5ec95c43b0
    06:22:13.727628Z  event=request.completed  request_id=85a140f5-...  trace_id=97e12b15a8e0118645d79f5ec95c43b0
```

### Gate 2 — the demo route inserts **exactly one** audit row with matching ids

```
$ curl -D - -X POST "localhost:8000/demo/audited?note=phase-7-gate"
x-request-id: 85a140f5-5632-4d99-8127-65890ae28722
x-trace-id:   97e12b15a8e0118645d79f5ec95c43b0

$ psql -c "SELECT * FROM audit_log WHERE request_id = '85a140f5-...'"
-[ RECORD 1 ]-------------------------------------
id          | 22f0495c-b533-4fdb-be90-a24ffb6583d3
action      | demo.audited
outcome     | success
actor_id    | dev
actor_roles | dev
request_id  | 85a140f5-5632-4d99-8127-65890ae28722   <- matches X-Request-ID
trace_id    | 97e12b15a8e0118645d79f5ec95c43b0       <- matches X-Trace-ID
http_method | POST
http_path   | /demo/audited
detail      | {"note": "phase-7-gate"}

$ psql -c "SELECT count(*) ... WHERE request_id = '85a140f5-...'"
1                                                    <- exactly one
```

### Gate 3 — append-only is enforced by the database, not by convention

```
$ psql -c "UPDATE audit_log SET outcome='tampered' WHERE request_id='85a140f5-...';"
ERROR:  audit_log is append-only: UPDATE is not permitted

$ psql -c "DELETE FROM audit_log WHERE request_id='85a140f5-...';"
ERROR:  audit_log is append-only: DELETE is not permitted

$ psql -c "SELECT outcome, count(*) ... GROUP BY outcome;"
success|1                                            <- row survived unchanged
```

### Gate 4 — the trace really exists, with DB spans inside it

```
$ curl -s "localhost:3200/api/traces/97e12b15a8e0118645d79f5ec95c43b0"
  resource batches: 3
  service.name = common-app-base
    span name='connect'                      <- SQLAlchemy instrumentation
    span name='INSERT'                       <- the audit insert
    span name='POST /demo/audited http send'
    span name='POST /demo/audited'           <- FastAPI instrumentation
    span name='POST /demo/audited'  request.id=85a140f5-5632-4d99-8127-65890ae28722
```

## Phase 8 — Error handling + Sentry — **PASS**

**Built:** `app/errors.py` — one `ErrorResponse` schema
(`error`, `message`, `request_id`, `trace_id`, optional `detail`) returned by
handlers for `AppError` (expected business failures: `NotFoundError`,
`ConflictError`, `ValidationError`, `PermissionDeniedError`,
`PayloadTooLargeError`), `RequestValidationError`, `StarletteHTTPException`,
`SQLAlchemyError` and bare `Exception`. Sentry init that no-ops without a DSN.
`GET /demo/boom` (unexpected failure) and `GET /demo/not-found` (expected one).

Deliberate distinctions:
- **Expected failures are warnings and are never sent to Sentry.** A 404 is not
  a bug; paging on it trains people to ignore alerts.
- **Database errors are logged, never returned.** Driver messages can leak
  schema and data, so the caller gets a generic `database_error`.

### Bug 1 (found by the gate) — the 500 had no `request_id`

First run returned:
```
{"error":"internal_error","message":"An unexpected error occurred. ..."}
```
No `request_id`, no `X-Request-ID` header — on the one response that needs it
most. Cause: `CorrelationMiddleware` called `clear_request_context()` *before*
re-raising, but Starlette's `ServerErrorMiddleware` sits **outside** user
middleware, so the 500 handler ran after the clear and found an empty context.
Fixed by not clearing on the exception path (the next request clears on entry).

### Bug 2 (found by the gate) — one failure produced **three** Sentry events

The explicit `capture_exception` plus Sentry's `LoggingIntegration` promoting
`log.error`/`log.exception` to events meant a single `ZeroDivisionError` was
reported three times — triple quota and triple alert noise. Fixed with
`LoggingIntegration(level=logging.INFO, event_level=None)`: logs become
breadcrumbs, `capture_exception` stays the single source of events.

### Gate 1 — `/demo/boom` returns clean JSON with the `request_id`, no stack trace

```
$ curl -D - localhost:8000/demo/boom
HTTP/1.1 500 Internal Server Error
x-request-id: 91d3fcdd-5017-4bc0-9d2e-de3cb2217cfb

{"error":"internal_error",
 "message":"An unexpected error occurred. Quote the request_id when reporting this.",
 "request_id":"91d3fcdd-5017-4bc0-9d2e-de3cb2217cfb",
 "trace_id":"366412902fc523e40c130a7c572d144f"}

leak markers (traceback / ZeroDivision / File "...") in body: 0
```

### Gate 2 — the exception is logged with its `request_id` (traceback server-side only)

```json
{"http_method":"GET","http_path":"/demo/boom","event":"request.failed",
 "request_id":"91d3fcdd-5017-4bc0-9d2e-de3cb2217cfb",
 "trace_id":"366412902fc523e40c130a7c572d144f","level":"error",
 "exception":"Traceback (most recent call last): ... ZeroDivisionError"}
```

### Gate 3 — every error class shares one schema

```
/demo/not-found  -> 404 {"error":"not_found","message":"No such demo resource.","request_id":"5e48d672-...","detail":{"looked_for":"nothing"}}
/does-not-exist  -> 404 {"error":"not_found","message":"Not Found","request_id":"bfbdad4e-..."}
?seed=notanumber -> 422 {"error":"validation_error","request_id":"89b355c8-...","detail":[{"type":"int_parsing","loc":["query","seed"],...}]}
```

### Gate 4 — with a DSN set, the event reaches Sentry tagged with the ids

Verified against a local Sentry-protocol envelope sink (no real DSN needed):

```
request_id = be31a654-aec3-48c3-824b-40ca592f4f7f
Sentry events for this one failure: 1        (was 3 before the fix)
  exception : ZeroDivisionError: division by zero
  tags      : {"service":"common-app-base",
               "request_id":"be31a654-aec3-48c3-824b-40ca592f4f7f",
               "trace_id":"5b45e4db4121ac68cad816aa343fc202"}

Sentry events after 3 EXPECTED errors (404, 404, 422): 0
```

### Gate 5 — no DSN means a clean no-op

```
{"event":"sentry.disabled","reason":"SENTRY_DSN is not set","level":"info","logger":"app.errors"}
```

## Phase 9 — File storage (MinIO, S3-compatible) — **PASS**

**Built:** `minio` in compose; `app/storage/` with the `Storage` interface
(`base.py`), the `MinioStorage` boto3 adapter and the `AzureBlobStorage` stub
that is the documented swap point; `app/db/models/stored_file.py`;
`app/api/files.py` with `POST /files`, `GET /files/{id}`,
`GET /files/{id}/content` and `GET /files/{id}/download-url` (presigned
redirect). `/health/ready` now pings storage too.

Design points:
- **Nothing imports an adapter directly** — only `get_storage()` — which is
  what keeps `STORAGE_PROVIDER=azure_blob` a config change.
- **boto3 is synchronous**, so every call goes through `asyncio.to_thread`
  rather than blocking the event loop.
- **Presigned URLs are signed with a separate public endpoint client**: inside
  compose the app reaches MinIO at `http://minio:9000`, but a browser must be
  handed `http://localhost:9000`, and the signature is endpoint-specific.
- **Upload keys are sanitised** (`filename.rsplit("/")[-1]`), so a crafted
  filename cannot escape its `uploads/YYYY/MM/DD/<uuid>/` prefix.

### Gate 1 — upload returns an id

```
$ curl -D - -F "file=@upload.txt;type=text/plain" localhost:8000/files
HTTP/1.1 201 Created
x-request-id: 90b96802-6158-421b-9b06-83a79cf66c29
{
    "id": "0f8be724-1ae5-4c1f-9819-2599be773062",
    "filename": "upload.txt",
    "content_type": "text/plain",
    "size_bytes": 77,
    "checksum_sha256": "d6702f6200f2c241ef9a344312126674937b63a155e3f5fbcdbf6a6d45676c2a",
    "uploaded_by": "dev",
    "storage_key": "uploads/2026/08/24/0f8be724-.../upload.txt"
}
```

### Gate 2 — fetching by id returns the *same bytes*

```
$ curl -o down.txt localhost:8000/files/0f8be724-.../content
HTTP/1.1 200 OK
content-disposition: attachment; filename="upload.txt"
x-checksum-sha256: d6702f6200f2c241ef9a344312126674937b63a155e3f5fbcdbf6a6d45676c2a

$ cmp upload.txt down.txt
  IDENTICAL (cmp exit 0)
  original sha256: d6702f6200f2c241ef9a344312126674937b63a155e3f5fbcdbf6a6d45676c2a
  fetched  sha256: d6702f6200f2c241ef9a344312126674937b63a155e3f5fbcdbf6a6d45676c2a
```

Presigned redirect fetched directly from MinIO, bypassing the app: HTTP 200,
bytes identical.

### Gate 3 — the DB stores the key, not the blob

```
$ psql -c "SELECT column_name, data_type FROM information_schema.columns WHERE table_name='stored_file';"
   column_name   |        data_type
-----------------+--------------------------
 storage_key     | character varying          <- the pointer
 filename        | character varying
 content_type    | character varying
 size_bytes      | bigint
 checksum_sha256 | character varying
 uploaded_by     | character varying
 id              | uuid
 created_at      | timestamp with time zone
 updated_at      | timestamp with time zone
(9 rows)                                       <- no bytea/blob column
```

And the bytes are in MinIO:

```
$ mc ls -r local/app-files
[2026-08-24 06:34:21 UTC]    77B STANDARD uploads/2026/08/24/0f8be724-.../upload.txt
```

### Gate 4 — an audit row was written for the upload, under the same `request_id`

```
action        | file.uploaded
actor_id      | dev
resource_type | file
resource_id   | 0f8be724-1ae5-4c1f-9819-2599be773062
request_id    | 90b96802-6158-421b-9b06-83a79cf66c29   <- the upload's X-Request-ID
trace_id      | 29df4a1fc1e8c4d1304d13bb7a24ee85
http_method   | POST
http_path     | /files
detail        | {"filename": "upload.txt", "size_bytes": 77, "storage_key": "...", "checksum_sha256": "d6702f62..."}
```

> **Amendment (requested after Phase 9).** The `AzureBlobStorage` stub was
> removed at the user's request — object storage is MinIO only. The `Storage`
> interface stays, because it is what keeps call sites free of boto3 and lets
> unit tests substitute a fake. MinIO speaks the S3 API, so the same adapter
> also works against real S3 with a different endpoint.

## Phase 10 — Background jobs (Celery) — **PASS**

**Built:** `app/jobs/celery_app.py` (Redis broker + result backend,
`task_acks_late` + `task_reject_on_worker_lost` so a worker crash re-queues
rather than silently dropping a job); `app/jobs/tasks.py` (`slow_add`,
`process_upload`, `always_fails`); a `worker` service in compose;
`POST /demo/job` and `GET /demo/job/{id}`.

**The correlation problem this phase actually solves.** The moment work goes
off-request, naive setups lose the thread. Here `before_task_publish` copies
`request_id`/`trace_id` onto the message headers, and `task_prerun` rebinds
them into structlog inside the worker — so a task logs under the request that
enqueued it, and no task body has to know. A task published outside a request
(a beat schedule) falls back to `task:<task_id>`, so it is still traceable to
something rather than to nothing.

### Gate 1 — enqueue returns immediately

```
$ curl -D - -X POST "localhost:8000/demo/job?a=17&b=25&delay_seconds=4"
HTTP/1.1 202 Accepted
x-request-id: a7d92201-db1e-48bc-882b-10bc9f80dd6d
{"task_id":"4a4fcdd4-...","status":"queued","request_id":"a7d92201-..."}

  enqueue latency: 255ms   (the task itself sleeps 4000ms)
```

### Gate 2 — status endpoint reaches SUCCESS with the result

```
  t+1s: {"status":"STARTED","result":null}
  t+2s: {"status":"STARTED","result":null}
  t+3s: {"status":"STARTED","result":null}
  t+4s: {"status":"SUCCESS","result":{"a":17,"b":25,"sum":42,
          "task_id":"4a4fcdd4-...",
          "request_id":"a7d92201-db1e-48bc-882b-10bc9f80dd6d",   <- the ENQUEUEING request
          "trace_id":"97a50872bf21c7c1c38da6e843c565ac"}}
```

### Gate 3 — worker logs carry the originating `request_id`

```
$ docker logs cab-worker | grep a7d92201-db1e-48bc-882b-10bc9f80dd6d
  event=task.started    request_id=a7d92201-...  task_id=4a4fcdd4..  logger=app.jobs.celery_app
  event=slow_add.begin  request_id=a7d92201-...  task_id=4a4fcdd4..  logger=app.jobs.tasks
  event=slow_add.done   request_id=a7d92201-...  task_id=4a4fcdd4..  logger=app.jobs.tasks
  event=Task demo.slow_add[...] succeeded in 4.0169s  request_id=a7d92201-...  logger=celery.app.trace
  event=task.finished   request_id=a7d92201-...  task_id=4a4fcdd4..  logger=app.jobs.celery_app

the same id appears in BOTH containers:
  cab-app:    1 lines
  cab-worker: 5 lines
```

Note the fourth line: even *Celery's own* `celery.app.trace` logger carries the
id, because stdlib logging is routed through the same structlog pipeline.

### Gate 4 — failures are correlated too

```
$ curl -X POST "localhost:8000/demo/job?fail=true"     -> request_id=cc119b39-...
  status: {"status":"FAILURE","error":"This task always fails, by design."}
  worker log:
    level=error event=task.failed  request_id=cc119b39-a690-4c1a-bed7-2c25cee3d232
```

### Gate 5 — a task can reach MinIO and Postgres

```
process_upload(file_id, storage_key) ->
  {'file_id': '0f8be724-...', 'size_bytes': 77, 'line_count': 3, 'processed_by_task': 'add8a460-...'}

audit row written by the worker:
  action      | file.processed
  request_id  | task:add8a460-aab8-47e5-af63-45395b69370f   <- no-HTTP-request fallback, as designed
  detail      | {"size_bytes": 77, "line_count": 3, ...}
```

## Phase 11 — Security (OWASP + scanning) — **PASS**

**Built:** `app/middleware/security.py` — `SecurityHeadersMiddleware` (OWASP
baseline + CSP), `RequestSizeLimitMiddleware`, and `build_cors_kwargs`;
`trivy.yaml` + `trivy-secret.yaml`; `make scan`.

Deliberate choices:
- **HSTS only over HTTPS.** Sending it on plain-HTTP localhost is meaningless,
  and harmful if a browser ever honours it for `localhost`. Uvicorn runs with
  `--proxy-headers`, so behind a TLS-terminating ingress the app sees
  `scheme=https` and emits it.
- **CSP is `default-src 'none'` for the API**, with a narrower policy for
  `/docs` and `/redoc` only (Swagger UI loads from a CDN).
- **The size limit caps the stream, not just `Content-Length`.** A chunked
  request can omit the header entirely.
- **`allow_origins=["*"]` + `allow_credentials=True` raises at startup.**
  Browsers reject that combination, so it fails loudly instead of producing
  CORS that silently never works.
- **Secret-scan allow-rules are keyed by PATH, not by value**, so the known
  local dev credentials are ignored while a real secret committed elsewhere
  still fails the scan.

### Bug 1 (found by the gate) — container was crash-looping

A multi-line `CMD [...]` broke the JSON exec form, so Docker fell back to shell
form and the container restarted forever (`/bin/sh: 1: [uvicorn,: not found`).
The first "no server header" reading was therefore meaningless — there was no
server. Fixed to a single-line `CMD` and re-verified.

### Bug 2 (found by the gate) — `Server: uvicorn` could not be removed in middleware

Uvicorn appends its banner *after* middleware, in the protocol layer, so
deleting the header in an ASGI wrapper silently did nothing. Fixed with
`--no-server-header` on the command line.

### Bug 3 (found by the gate) — error responses shipped with NO security headers

The 500 from `/demo/boom` carried only `x-request-id`. Starlette's
`ServerErrorMiddleware` is the **outermost** layer, so a response it generates
never passes back through `SecurityHeadersMiddleware`. Fixed by extracting
`apply_security_headers()` and calling it from the error renderer too.

### Gate 1 — the security header set is present

```
$ curl -D - localhost:8000/health/live
x-content-type-options: nosniff
x-frame-options: DENY
referrer-policy: strict-origin-when-cross-origin
permissions-policy: geolocation=(), microphone=(), camera=(), payment=(), usb=()
x-permitted-cross-domain-policies: none
cross-origin-opener-policy: same-origin
cross-origin-resource-policy: same-origin
cache-control: no-store
content-security-policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'
x-request-id: 183160c8-1a5f-4c85-8f7b-0f9b16966240
                                        (no `server:` header — banner suppressed)

$ curl -D - -H "X-Forwarded-Proto: https" localhost:8000/health/live
strict-transport-security: max-age=31536000; includeSubDomains     <- HSTS over TLS only
```

And on error responses, after the Bug 3 fix:

```
$ curl -D - localhost:8000/demo/boom
HTTP/1.1 500 Internal Server Error
x-request-id: 86c464ec-9414-44f0-bea2-222025746418
x-content-type-options: nosniff
x-frame-options: DENY
referrer-policy: strict-origin-when-cross-origin
content-security-policy: default-src 'none'; frame-ancestors 'none'; ...
```

### Gate 2 — request-size limit and CORS

```
$ curl -F "file=@11MiB.bin" localhost:8000/files          # limit is 10 MiB
{"error":"payload_too_large","message":"Request body exceeds the 10485760 byte limit.",
 "request_id":"7205a42d-...","trace_id":"3c3958d2..."}
  HTTP 413

$ curl -F "file=@upload.txt" localhost:8000/files          # small file unaffected
  HTTP 201

$ curl -X OPTIONS -H "Origin: http://example.com" -H "Access-Control-Request-Method: POST" localhost:8000/files
HTTP/1.1 200 OK
access-control-allow-origin: *
access-control-allow-methods: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT
access-control-max-age: 600
```

### Gate 3 — no secrets in code

```
$ grep -rnE "(password|secret|api[_-]?key|token)\s*=\s*[\"'][^\"']{6,}" app/ --include="*.py"
  none found in app/ (all credentials come from Settings/env)
```

### Gate 4 — Trivy dependency + image scan (policy: fail on HIGH/CRITICAL)

```
$ trivy fs .
┌──────────────────────────┬────────────┬─────────────────┬─────────┬───────────────────┐
│          Target          │    Type    │ Vulnerabilities │ Secrets │ Misconfigurations │
├──────────────────────────┼────────────┼─────────────────┼─────────┼───────────────────┤
│ uv.lock                  │     uv     │        0        │    -    │         -         │
│ deploy/docker/Dockerfile │ dockerfile │        -        │    -    │         0         │
└──────────────────────────┴────────────┴─────────────────┴─────────┴───────────────────┘
EXIT CODE: 0

$ trivy image common-app-base:local --severity HIGH,CRITICAL --ignore-unfixed
  TOTAL HIGH/CRITICAL (fixable): 0
  EXIT CODE: 0
```

## Phase 12 — Testing (pytest + Playwright) — **PASS**

**Built:** 116 tests across three layers.

- **Unit** (`tests/unit/`) — settings and derived DSNs, the `Secrets`
  interface, the `Storage` *contract* exercised through an in-memory fake
  (the payoff of depending on the interface: no MinIO, no network, no
  credentials), correlation context, request-id validation against hostile
  input, the error schema, security headers and the CORS guard, and the auth
  seam.
- **Integration** (`tests/integration/`) — two suites. `test_api.py` drives the
  **containerised** app over HTTP; `test_inprocess_api.py` drives the *same*
  app object through an ASGI transport against the real Postgres/Redis/MinIO.
  `test_correlation_stores.py` re-checks the Correlation Contract against Loki
  and Prometheus.
- **E2E** (`tests/e2e/`) — Playwright, headless Chromium: Swagger UI renders,
  the strict CSP does not break it, "Try it out" reaches the live API, and the
  browser can read `X-Request-ID`.

Integration and e2e tests **skip with a reason** when the stack is down, so a
missing stack can never masquerade as a passing suite.

### Bug 1 (found by the gate) — duplicate `X-Request-ID` header

`test_expected_error_returns_404_in_the_same_schema` failed with
`'02ffcff2-…, 02ffcff2-…'`. On *handled* errors both the error renderer and the
correlation middleware stamped the header, and the middleware used `append`.
Only handled errors were affected, which is exactly the kind of thing manual
curl checks miss. Fixed by switching the middleware to `MutableHeaders.setdefault`.

### Bug 2 (found by the gate) — `make test` deadlocked

Unit and e2e suites each passed alone but hung together: pytest-playwright's
**sync** fixtures cannot run inside the event loop `asyncio_mode = auto`
creates. Rewrote the e2e suite on `playwright.async_api` with deliberately
function-scoped fixtures — a session-scoped browser binds its driver
subprocess to one loop and *deadlocks* rather than failing.

### Bug 3 (found by the gate) — `create_app()` could only be called once

Building a second app died on the first request with
`DuplicateTimeseries: {'http_requests_inprogress'}` — Prometheus collectors are
process-global. A template whose factory cannot run twice breaks every
consumer's test suite. `configure_metrics` now instruments once per process and
later apps reuse the existing collectors.

### Bug 4 (found by the gate) — a Grafana panel had been silently broken

Chasing Bug 3 revealed the in-flight gauge is named `http_requests_inprogress`:
the instrumentator does **not** apply `metric_namespace` to it. The dashboard
queried `app_http_requests_inprogress`, so the "Requests in flight" panel had
been showing *No data* since Phase 6 — my Phase 6 gate checked the other panel
queries but not that one. Fixed by naming the gauge explicitly.

```
$ curl -s localhost:8000/metrics | grep inprogress
# TYPE app_http_requests_inprogress gauge
app_http_requests_inprogress{handler="/demo/cached",method="GET"} 0.0

$ curl -G localhost:9090/api/v1/query --data-urlencode 'query=sum(app_http_requests_inprogress)'
  result: [{'metric': {}, 'value': [1787561153.542, '0']}]     # was: NO DATA
```

### Gate — `make test` runs everything green, above the coverage threshold

```
$ make test
uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=80
........................................................................ [ 62%]
............................................                             [100%]

Name                            Stmts   Miss  Cover
-------------------------------------------------------------
app\api\files.py                   73     15    79%
app\api\health.py                  43      6    86%
app\audit\writer.py                18      3    83%
app\cache\client.py                40      4    90%
app\config.py                     104      3    97%
app\db\session.py                  35      1    97%
app\errors.py                      89     12    87%
app\jobs\celery_app.py             45     17    62%
app\jobs\tasks.py                  30     15    50%
app\main.py                        49     10    80%
app\middleware\correlation.py      69      8    88%
app\middleware\security.py         71      7    90%
app\observability.py               64     19    70%
app\storage\minio.py               67     23    66%
-------------------------------------------------------------
TOTAL                            1077    143    87%

19 files skipped due to complete coverage.
Required test coverage of 80% reached. Total coverage: 86.72%
116 passed in 34.45s
```

Playwright headless, run separately for clarity:

```
$ uv run pytest tests/e2e -q
......                                                                   [100%]
```

**On the residual gaps.** `app/jobs/tasks.py` (50%) and `celery_app.py` (62%)
are executed by the *worker container*, so their bodies are invisible to
coverage even though `test_enqueue_and_poll` proves they run. `storage/minio.py`
(66%) is missing its error-mapping branches. These are honest gaps, not
excluded from measurement to flatter the number.

## Phase 13 — CI/CD + Kubernetes — **PASS**

**Built:** `.github/workflows/ci.yml` — `lint → test → scan → build → deploy`,
where each stage gates the next. `deploy/k8s/` — Namespace (with the
`restricted` Pod Security Standard), ConfigMap, Secret, two Deployments (API +
worker), Service, HPA, PodDisruptionBudget, NetworkPolicy, and a
`kustomization.yaml`.

Choices worth stating:

- **The image is built, scanned, then pushed — in that order.** The build job
  loads the image locally, runs Trivy against it, smoke-tests it (including
  that `X-Request-ID` is present in the shipped artefact), and only then logs in
  and pushes. A failing scan cannot produce a pushed image.
- **Tests run against real Postgres, Redis and MinIO** as GitHub service
  containers, not mocks.
- **Three probes, three different jobs.** `startupProbe` gates the others so a
  slow boot is not read as a crash loop; `livenessProbe` hits `/health/live`
  and touches **no** dependency, so a slow database cannot get healthy pods
  killed and escalate a partial outage into a total one; `readinessProbe` hits
  `/health/ready`, which *does* check dependencies, so a pod that cannot reach
  its backing services leaves the Service instead of serving errors.
- **Memory is limited, CPU is not.** CPU throttling hurts tail latency more
  than it protects neighbours; memory is not compressible, so it is capped.
- **The Secret contains placeholders only**, with the swap to External
  Secrets / Sealed Secrets / the Key Vault CSI driver documented in-file.

### Bug found by this phase — 10 real type errors

`mypy` had never actually been run (it is in the Makefile and now in CI, but no
previous gate invoked it). It found ten genuine errors: the correlation
middleware was annotated with bare `dict`/`Callable` instead of the ASGI
`Scope`/`Receive`/`Send` types; `Response.body` is `bytes | memoryview` and was
being `.decode()`d directly; `_NullSpanExporter` did not actually implement
`SpanExporter`; and `build_cors_kwargs` returned `dict[str, object]`, which
cannot be splatted into `add_middleware`. All fixed properly — a `TypedDict`
for the CORS kwargs, real ASGI types, `bytes(...)` coercion, and a genuine
`SpanExporter` subclass — rather than silenced with `type: ignore`.

```
$ make typecheck
uv run mypy app
Success: no issues found in 33 source files
```

### Gate 1 — lint + test + scan run clean locally

```
$ make lint
All checks passed!
uv run ruff format --check .
50 files already formatted

$ make typecheck
Success: no issues found in 33 source files

$ make test
116 passed in 42.31s
Required test coverage of 80% reached. Total coverage: 86.81%

$ trivy fs .        -> 0 findings, exit 0     (recorded in Phase 11)
$ trivy image ...   -> 0 HIGH/CRITICAL, exit 0
```

Workflow structure parses and the dependency graph is correct:

```
  jobs: ['lint', 'test', 'scan', 'build', 'deploy']
    lint     needs=-                steps=6
    test     needs=lint             steps=11
    scan     needs=lint             steps=4
    build    needs=['test','scan']  steps=8
    deploy   needs=build            steps=3
```

`act` is not installed on this host, so the stages were run directly (above)
rather than in a simulated runner.

### Gate 2 — manifests render and validate

`kubectl apply --dry-run=client` needs API discovery and hangs with no cluster
context configured, so validation is done by rendering plus **offline** schema
validation with `kubeconform`, which is stricter than a client dry-run:

```
$ kubectl kustomize deploy/k8s
rendered 9 resources:
kind: Namespace                name: common-app-base
kind: ConfigMap                name: common-app-base-config
kind: Secret                   name: common-app-base-secrets
kind: Service                  name: common-app-base
kind: Deployment               name: common-app-base
kind: Deployment               name: common-app-base-worker
kind: PodDisruptionBudget      name: common-app-base
kind: HorizontalPodAutoscaler  name: common-app-base
kind: NetworkPolicy            name: common-app-base

$ kubectl kustomize deploy/k8s | kubeconform -strict -summary -kubernetes-version 1.31.0 -
Summary: 9 resources found parsing stdin - Valid: 9, Invalid: 0, Errors: 0, Skipped: 0
```

### Gate 3 — the probes point at the health endpoints

```
  common-app-base:
     startupProbe    -> /health/live
     livenessProbe   -> /health/live
     readinessProbe  -> /health/ready
  common-app-base-worker:
     livenessProbe   -> celery ... inspect ping
```

## Phase 14 — Full-stack correlation smoke test — **PASS**

**Built:** `scripts/smoke.py` (`make smoke`). It waits for the stack, performs
three real operations — upload a file, run a background job, trigger
`/demo/boom` — captures each `X-Request-ID`, then interrogates **each store
independently** (not through the API that wrote to it) and asserts the id is
there: Loki for logs, Tempo for the trace, Postgres for the audit row,
Prometheus for the counters, and the correlated error response. It exits
non-zero if any required leg is missing.

### Bug 1 (found by the gate) — the audit query silently returned nothing

The first run reported `no audit row` for every operation, even though the rows
were present. `psql` does **not** expand `:'var'` variables in a `-c` command
string. Fixed by feeding the SQL on stdin (`-f -`), keeping the id out of the
SQL text — the predicate is never built by string formatting.

### Bug 2 (found by the gate) — error responses had no `X-Trace-ID`

The boom step could never find its trace, because the 500 response carried no
`X-Trace-ID` header at all: `ServerErrorMiddleware` is outermost, so the
correlation middleware never sees that response, and the error renderer was
setting only `X-Request-ID`. The body had the trace id but the header did not —
an inconsistency in the Correlation Contract. Fixed in `app/errors.py`.

### Bug 3 (found by the gate) — the report cried wolf

A background job legitimately writes no audit row, but the table printed a red
`FAIL` for it. A check that fails for something never expected teaches people
to ignore the report, so a non-required missing check now renders `--` with an
explicit `n/a` note.

### Gate — `make smoke` exits 0 and prints the correlation table

```
1. Waiting for the stack
  ready: {"redis": "ok", "postgres": "ok", "storage": "ok"}
  tempo: available

2. Driving the stack
  upload      request_id=8da264a6-57c3-4161-a21f-c2359640ad71  file_id=427f3906-...
  job         request_id=76fbaed9-2abe-4db7-8cc6-0eec14e36a4f  task_id=d6ab8d17...
  boom        request_id=fa82d638-44f1-4c76-82dd-323a8a7959ea  HTTP 500

4. Interrogating each store independently
  loki    upload a file: 3 line(s) in Loki
  loki    trigger a background job: 1 line(s) in Loki
  loki    trigger /demo/boom: 5 line(s) in Loki
  tempo   upload a file: 9 span(s) in Tempo
  tempo   trigger a background job: 8 span(s) in Tempo
  tempo   trigger /demo/boom: 6 span(s) in Tempo
  audit   upload a file: 1 row(s): file.uploaded
  audit   trigger a background job: n/a - this operation writes no audit row
  audit   trigger /demo/boom: 1 row(s): demo.boom
  worker  trigger a background job: result.request_id=76fbaed9-..., 5 worker log line(s)
  metric  upload a file: /files: 0 -> 1
  metric  trigger a background job: /demo/job: 0 -> 1
  metric  trigger /demo/boom: /demo/boom: 1 -> 2

5. Correlation report
  operation                  request_id                             log   trace  audit  metric  error  worker
  ------------------------------------------------------------------------------------------------------------
  upload a file              8da264a6-57c3-4161-a21f-c2359640ad71   OK    OK     OK     OK      --     --
  trigger a background job   76fbaed9-2abe-4db7-8cc6-0eec14e36a4f   OK    OK     --     OK      --     OK
  trigger /demo/boom         fa82d638-44f1-4c76-82dd-323a8a7959ea   OK    OK     OK     OK      OK     --

  note: SENTRY_DSN is not set, so the Sentry leg is not asserted.
        The error leg still checks the correlated error response.
  SMOKE PASSED - one request_id joins logs, traces, audit and metrics.

SMOKE EXIT: 0
```

**On the Sentry leg.** No Sentry DSN is configured here, so `make smoke` does
not claim to have checked Sentry — it says so explicitly rather than showing a
green cell it did not earn. The Sentry path *was* verified for real in Phase 8
against a local envelope sink: one event, tagged with the matching `request_id`
and `trace_id`. With `SENTRY_DSN` set, the same events flow to a real project.

### Follow-up — cold-start performance

Verified from a genuinely clean slate (`docker compose down -v`, full rebuild,
`make migrate`, `make smoke`). It passed, but took **over 10 minutes**: each
probe retried on its own schedule and they ran sequentially, so a cold Loki and
an unscraped Prometheus made the worst cases additive. The probes touch no
shared state, so they now run concurrently via `asyncio.gather`:

```
$ make smoke
  SMOKE PASSED - one request_id joins logs, traces, audit and metrics.
SMOKE EXIT: 0   elapsed: 25s          # was > 600s
```

---

# FINAL STATE

## Definition of Done

| Requirement | Status |
|---|---|
| `docker compose up` brings up app, worker, postgres, redis, minio, loki, promtail, prometheus, grafana | **Done** — `config --services` lists exactly those nine (Tempo is an opt-in `tracing` profile) |
| All 15 gates (Phase 0–14) recorded PASS with real output | **Done** — this file |
| One `request_id` traces across logs, traces, audit, metrics and errors | **Done** — Phase 14 table, `make smoke` exit 0 |
| README documents running locally, tests, the correlation model, the swap points and the auth seam | **Done** — `README.md` |

## Final verification

```
$ make lint        -> All checks passed! / 51 files already formatted
$ make typecheck   -> Success: no issues found in 33 source files
$ make test        -> 116 passed; coverage 86.84% (gate 80%)
$ make smoke       -> SMOKE PASSED, exit 0
$ trivy fs .       -> 0 findings, exit 0
$ trivy image      -> 0 HIGH/CRITICAL, exit 0
$ kubeconform      -> Valid: 9, Invalid: 0
```

## Bugs the gates caught

Fifteen defects were found only because each gate inspected real output rather
than assuming success:

| Phase | Bug |
|---|---|
| 5 | Logs double-encoded — the whole JSON object nested inside `"event"` |
| 6 | Metrics named `app_http_http_requests_total` |
| 8 | The 500 response lost its `request_id` entirely |
| 8 | One failure produced three Sentry events |
| 11 | Container crash-looping from a multi-line `CMD` JSON form |
| 11 | `Server: uvicorn` banner not removable from middleware |
| 11 | Error responses shipped with no security headers |
| 12 | Duplicate `X-Request-ID` header on handled errors |
| 12 | `make test` deadlocked mixing sync Playwright with asyncio |
| 12 | `create_app()` could only be called once per process |
| 12 | A Grafana panel had queried a metric name that never existed |
| 13 | Ten real type errors — `mypy` had never actually been run |
| 14 | The audit assertion silently returned nothing (`psql -c` ignores `:'var'`) |
| 14 | Error responses carried no `X-Trace-ID` |
| 14 | The smoke report showed `FAIL` for a check that was never required |
| 14 | `make smoke` took >10 min on a cold stack (probes retried sequentially) |


---

# VALIDATION TRIAL — does the base actually save time?

The gates prove the base works. They do not prove it is *useful*. The real
question is: **drop business logic in, and does the plumbing apply by itself?**

So the base was used the way a team would use it. On branch `poc-trial`, a
support-ticket domain was built following **only** the README, writing **zero**
plumbing code: a model, a CRUD router with a business rule, a Celery task, and
tests.

## What was written

| File | Lines | Content |
|---|---|---|
| `app/db/models/ticket.py` | 28 | model |
| `app/api/tickets.py` | 145 | 5 endpoints + business rules |
| `app/jobs/tasks.py` | +16 | one Celery task |
| `app/db/models/__init__.py` | +1 | register the model |
| `app/main.py` | +2 | register the router |
| `tests/integration/test_tickets_poc.py` | 140 | 16 tests |

**Feature live in 152 seconds**, from first file to a healthy container serving
it. Roughly 190 lines of business logic and two lines of wiring.

## What was inherited for free — nothing written to get any of it

### Correlation on brand-new routes

```
$ curl -D - -X POST localhost:8000/tickets -d '{"title":"Printer on fire",...}'
HTTP/1.1 201 Created
x-content-type-options: nosniff
content-security-policy: default-src 'none'; frame-ancestors 'none'; ...
x-request-id: c7080467-a4b9-4f9b-b8c0-3a88153ada6d
x-trace-id:   c1016fe09dffe9a736352a45bd96ad11
```

### Audit, correlated to that request

```
action        | ticket.created
actor_id      | dev                                    <- from the auth seam
resource_id   | 91d8a4dd-ef0f-45ff-be72-1519ec2d22d1
request_id    | c7080467-a4b9-4f9b-b8c0-3a88153ada6d   <- matches the header
trace_id      | c1016fe09dffe9a736352a45bd96ad11
detail        | {"title": "Printer on fire", "priority": 1}
```

### A business rule becomes a correct HTTP error

`raise ConflictError(...)` in the router, nothing else:

```
$ curl -X POST localhost:8000/tickets/<id>/close      # second time
HTTP 409
{"error":"conflict","message":"Ticket 91d8a4dd-... is already closed.",
 "request_id":"0e0c0846-...","trace_id":"dc30242b...","detail":{"status":"closed"}}
```

### The worker inherits the request id across the process boundary

```
$ docker logs cab-worker | grep 52aa012a-89dc-492f-a7d9-c14cdcaf9ee3
  event=task.started         request_id=52aa012a-89dc-492f-a7d9-c14cdcaf9ee3
  event=ticket.notify.begin  request_id=52aa012a-89dc-492f-a7d9-c14cdcaf9ee3
  event=ticket.notify.done   request_id=52aa012a-89dc-492f-a7d9-c14cdcaf9ee3
  event=Task tickets.notify_closed[...]  request_id=52aa012a-...
  event=task.finished        request_id=52aa012a-89dc-492f-a7d9-c14cdcaf9ee3
```

### Metrics, correctly templated

Note `{ticket_id}` — the path is templated, so a million tickets produce one
series, not a million:

```
    /tickets                   status=201  count=3
    /tickets/{ticket_id}/close status=200  count=2
    /tickets/{ticket_id}/close status=409  count=1
```

### Logs queryable by request id in Loki

```
    ticket.created     91d8a4dd-ef0f-45ff-be72-1519ec2d22d1  request_id=c7080467-...
    audit.written      ticket.created                        request_id=c7080467-...
    request.completed  /tickets                              request_id=c7080467-...
```

### And also, unprompted

- **OpenAPI** documented all five endpoints automatically.
- **Validation** errors used the standard schema with per-field detail.
- **404s** used the standard schema.
- **Health** was unaffected.
- **`make revision`** autogenerated the migration, indexes included.
- **The quality gates applied to the new code**: ruff rejected a dict
  comprehension in the POC router, and mypy type-checked it.

## Two defects the trial exposed — both fixed in the base

Neither was reachable by the phase gates; both needed the base to be *used*.

**1. The test fixture was not reusable.** `app_client` lived inside
`test_inprocess_api.py`, so a new feature suite could not use it:

```
E       fixture 'app_client' not found
```

Friction in the base's single most common workflow. Promoted to
`tests/conftest.py`, so any new suite just asks for it.

**2. `make scan` failed, and CI would have failed with it.** Trivy's KSV-0109
flagged the ConfigMap:

```
$ make scan   ->  EXIT 1
KSV-0109 (HIGH): ConfigMap 'common-app-base-config' stores secrets in key(s) '{"SECRETS_PROVIDER"}'
```

A false positive — the rule matches the key *name*; the value is `env`, a
provider selector. Phase 11 scanned before the K8s manifests existed and
Phase 13 validated them with `kubeconform`, not Trivy, so nothing caught it.
Fixed with a **path-scoped** ignore carrying a written justification, and
verified narrow: a real secret in a different ConfigMap still fails.

```
=== NEGATIVE TEST: a real secret in a DIFFERENT ConfigMap ===
EXIT CODE: 1        (correct - the ignore does not hide it)
KSV-0109 (HIGH): ConfigMap 'another-config' stores secrets in key(s) '{"API_SECRET_KEY", "DB_PASSWORD"}'
```

## Regression results

| Check | Before trial | With POC feature | After revert |
|---|---|---|---|
| `make lint` | pass | pass | pass |
| `make typecheck` | 33 files | 35 files | 33 files |
| `make test` | 116 passed, 86.84% | 132 passed, 86.22% | 116 passed, 86.84% |
| `make smoke` | exit 0 | exit 0 | exit 0 |
| `make scan` | **exit 1 (bug)** | exit 0 (fixed) | exit 0 |

The POC feature lives on branch `poc-trial` as evidence. `master` carries only
the two fixes; the ticket domain is not on it.

**One caveat worth knowing:** switching branches after running a migration
strands the local database on a revision the other branch does not have
(`Can't locate revision identified by 'e3bbd4f01309'`). This is normal Alembic
behaviour, not a defect. The local fix is `make down && make up && make migrate`,
which was done — and re-proved the from-scratch path.

## Verdict

The base delivers on its purpose. A new feature gets correlation, audit,
metrics, structured logs, error shaping, security headers, tracing, OpenAPI and
migrations **without writing any of them** — roughly 190 lines of business
logic and two lines of wiring, live in under three minutes.


---

# REMEDIATION — SOLIDIFY_BRIEF.md

**Verdict: READY.** Full evidence in [TEST_REPORT.md](TEST_REPORT.md); the
baseline audit that prompted this work is preserved in
[TEST_REPORT_BASELINE.md](TEST_REPORT_BASELINE.md).

The independent audit of `22319b3` returned **NOT READY** on one zero-tolerance
criterion: adding a feature required editing two protected base modules. Both
causes were the same shape -- a hand-maintained registry that failed **silently**
when a developer forgot it.

## What changed, in one line each

| Commit | Fix | Verification |
|---|---|---|
| `8e13069` | **fix-1** Celery tasks discovered, not listed; `enqueue()` refuses unregistered names; import errors are fatal | `probe.discovered` appears in the real worker's `[tasks]` with no list edited |
| `228f9b7` | **fix-2** models discovered; autogenerate refuses `drop_*` without `ALLOW_DESTRUCTIVE=1` | deleting a model makes `make revision` exit 1 instead of emitting a drop |
| `531fed9` | **fix-3** audit actor in a contextvar, bound by middleware, carried on Celery headers | task enqueued as `alice/[admin,ops]` wrote `actor_id=alice` without mentioning an actor |
| `50b78d9` | **fix-4** statement-level TRUNCATE trigger + least-privilege runtime role | `TRUNCATE` and `DROP TRIGGER` both denied to the app role; triggers still fire for the owner |
| `ab4e813` | **fix-5** cache outages degrade to a miss | Redis stopped: `/health/ready` 503, `/demo/cached` **200** |
| `e215b1a` | **fix-6** `make install` verifies the venv it just built | removing `pyvenv.cfg` makes install exit 1 instead of falsely succeeding |
| `58db1ec` | **fix-1b** tasks load lazily, so the registry never depends on lifespan | caught by the full suite; ASGITransport runs no lifespan |
| `d1d00c0` | **part-2** one folder per service; `app/core` vs `app/services`; routers discovered too | behaviour preserved: 158 passed before **and** after |

## The result

| | Baseline `22319b3` | Now |
|---|---|---|
| Base edits to add a feature | 3 files, 5 lines (2 protected) | **0** |
| Hand-maintained registries | 3 | **0** |
| Tests / coverage | 116 / 86.84% | **158 / 88.33%** |
| Worker non-JSON log lines | 17 of 31 | **0 of 6** |

## Gate — the full protocol, twice

```
             RUN 1                                    RUN 2
lint         exit 0                                   exit 0
typecheck    exit 0, 38 files                         exit 0, 38 files
test         exit 0, 158 passed, 88.33%               exit 0, 158 passed, 88.33%
smoke        exit 0, SMOKE PASSED                     exit 0, SMOKE PASSED
scan         exit 0                                   exit 0
```

**Part D, the criterion that failed before:**

```
$ git diff --stat        # after adding a feature with a router, a task and a model
                         <- empty

$ git status --porcelain
?? app/services/widgets/
?? migrations/versions/20260824_2235_add_widget_table.py
```

PASS.

## Still open (out of this brief's scope)

Five findings from the baseline audit were not in `SOLIDIFY_BRIEF.md`'s list of
six and were deliberately left alone. Highest value first:

1. `ENVIRONMENT=prod` still boots with `postgres_password="apppassword"` -- no
   validator ties the environment to the credential defaults.
2. `cors_allow_origins` still defaults to `"*"`. The mechanism is correct; the
   default is not.
3. One unhandled exception still produces three error log records.
4. A chunked over-limit body returns 400 rather than 413 (memory is bounded).
5. No Celery retry policy is demonstrated.

---

# HARDENING PASS -- 2026-08-25

The five findings recorded above as "still open" are now closed. They were all
configuration or observability, not structure -- which is why the base was
structurally READY before it was safe to deploy.

| Commit | Defect | What changed |
|---|---|---|
| `8e55450` | #7 | `Settings` refuses to construct when `ENVIRONMENT != local` and a credential still equals its shipped default. The guarded field set is derived from the model, so a secret added later is covered automatically. |
| `a8f211e` | #6 | `CORS_ALLOW_ORIGINS` defaults to deny. `*` is an explicit opt-in, in `.env.example` too. |
| `996d5c0` | docs | `app/services/__init__.py` no longer tells you to register routers in `app/main.py` -- untrue since router discovery, and it pointed developers at a protected module. Pinned by a grep test. |
| `288e5d4` | #9 | One exception now writes one traceback. Uvicorn's duplicate is filtered by exception *object*, so an unlogged exception still prints its stack. |
| `cd10655` | #11 | An over-limit chunked body returns 413 in the standard error schema instead of a 400 blaming the client's syntax. |
| `f23decc` | #12 | Every task inherits a retry policy (transient errors only, exponential backoff, jitter, capped at 3) from the base task class. |

## Notes worth keeping

- **The prod guard is derived, not listed.** A field counts as a credential if
  its name says so and its default is a non-empty string. Locators ending in
  `_url` / `_endpoint` are excluded, so `azure_key_vault_url` is not mistaken
  for a secret.
- **Retries exclude bugs on purpose.** `TypeError` is not in `autoretry_for`:
  retrying it burns the queue to reach the identical failure three more times.
- **Jitter is not decoration.** Without it every task that failed during an
  outage retries in lockstep the moment it ends, and knocks the dependency over
  again.
- **The 413 fix swallows only the exception it caused.** The disconnect is our
  own signal; anything raised while the body was within the limit still
  propagates.
- **Trivy note:** a planted *AWS documentation example* key is not flagged --
  that is the scanner being correct, not the gate being weak. The negative
  control uses a realistic GitHub PAT, which fails the build as it should.

## Verification

- 184 tests, 88.91% coverage, two identical runs.
- Prod-boot proof against the built image: refuses with dev defaults (exit 1),
  boots with real secrets, `local` unchanged.
- No-regression sweep clean: correlation smoke, readiness naming both deps,
  Redis-down degradation, audit UPDATE/DELETE/TRUNCATE rejection, Trivy gate
  (tree + image + negative control).
- Docs POC re-run on the hardened base: zero `app/core/**` edits; removing it
  leaves an empty diff.

**Verdict: READY, and prod-ready.**
