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

