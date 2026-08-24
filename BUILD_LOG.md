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

