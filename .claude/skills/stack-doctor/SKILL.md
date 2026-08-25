---
name: stack-doctor
description: Diagnose the local stack when something looks broken — empty Grafana panels, missing traces, readiness 503, tests skipping, worker not running tasks, app refusing to boot. Use when the user reports that a component "isn't working", "shows nothing", or "is down".
---

# Diagnosing the stack

Most reports of "X isn't working" in this base are one of a handful of expected
states. Check those first — do not start changing code.

## Step 0: is anything actually running?

```bash
docker compose -f deploy/docker-compose.yml ps
curl -s localhost:8000/health/ready
```

`/health/ready` **names the failing dependency**, which is usually the whole
diagnosis:

```json
{"status":"degraded","checks":{"postgres":"ok","redis":"error: timeout after 3s","storage":"ok"}}
```

If Docker itself is not running, nothing else in this file applies.

---

## "Grafana shows nothing / no 200s"

**Almost always: the app is idle.** Every metric panel is rate-based
(`rate(app_http_requests_total[1m])`), so with no traffic every series is
legitimately `0`.

Confirm the data exists before touching anything:

```bash
curl -sG localhost:9090/api/v1/query --data-urlencode 'query=sum by (status) (app_http_requests_total)'
```

Non-zero counters + zero rates = idle, not broken. Then generate traffic:

```bash
for i in $(seq 1 90); do curl -s -o /dev/null localhost:8000/demo/cached; done
```

Also check: time range set to **Last 1 hour** (counters reset when the app
container restarts, so instant queries only show post-restart traffic), and the
"Total requests (30m)" stat panel — it uses `increase()` and reads true while
idle.

Ruled out already, do not re-investigate: the datasources are provisioned and
every metric panel names `uid: prometheus` explicitly, so the Loki-is-default
trap does not apply here.

## "Traces are missing / Tempo is empty"

**Expected locally.** Trace *export* is off by default:
`OTEL_EXPORTER_OTLP_ENDPOINT` is blank and Tempo sits behind the `tracing`
compose profile. Spans are still created, so `trace_id` appears in logs, audit
rows and error responses — correlation works without Tempo.

To actually ship them (note **4318**, the OTLP/HTTP port — 4317 is gRPC and the
exporter here is HTTP):

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318 \
  docker compose -f deploy/docker-compose.yml --profile tracing up -d app worker tempo
```

Symptom of getting the port wrong: repeated
`Transient error ... ConnectionResetError ... while exporting span batch`.

## "Sentry isn't capturing"

No DSN locally. Startup logs `sentry.disabled` with the reason. Set `SENTRY_DSN`.

## "Most of the tests are skipping"

Integration and e2e tests skip **with a reason** when the compose stack is
unreachable, so a missing stack never masquerades as a passing suite. Run
`make up` (and `make migrate`) first.

## "The app won't start"

Read the error — this base fails loudly on purpose:

| Error | Cause |
|---|---|
| `ValidationError: ENVIRONMENT='prod' is still using the local development defaults for: ...` | working as designed. Supply real secrets, or use `ENVIRONMENT=local` |
| `PluginImportError: Plugin module '...' failed to import` | a module under `app/services/` raises on import. Fix the module; the loud failure is deliberate |
| `CORS_ALLOW_CREDENTIALS=true requires explicit CORS_ALLOW_ORIGINS` | browsers reject credentials + wildcard; name the origins |

## "A task never runs"

- `enqueue()` raises `UnknownTaskError` when the name is not registered — that
  error lists every registered name, which is usually the typo.
- Check the worker picked it up: `docker logs cab-worker | grep tasks_loaded`.
- The task module must be importable under `PLUGIN_PACKAGES` (default
  `app.services`).
- Rebuild after adding code: `docker compose -f deploy/docker-compose.yml up -d --build app worker`.
  The containers run a **built image**, not your working tree.

## "A route 404s that should exist"

Routers are discovered from module-level `router` objects under
`app/services/`. Check: the attribute is named exactly `router`, it is an
`APIRouter`, and the containing module imports cleanly. Then rebuild the
container. Startup logs one `router.mounted` line per feature — grep for it.

## "The table doesn't exist"

Discovery makes a model visible to Alembic; it does not create tables. Run
`make revision m="..."` then `make migrate`.

## "Autogenerate wants to drop my table"

`DestructiveMigrationError` is the guard doing its job. The usual cause is a
model that was not imported, which makes its table look deleted. Verify the
model is under a discovered package **before** considering
`ALLOW_DESTRUCTIVE=1`.

## "Cached endpoint returns 200 but Redis is down"

By design: a Redis outage degrades to a cache **miss**, so the route reads from
the origin and still returns `200`. Readiness reports Redis down at the same
time. Both are correct simultaneously — availability is not readiness.

---

## Rules for this diagnosis

- **Confirm the expected-state explanations above before editing code.** Most of
  these reports are configuration or idleness, not defects.
- Read the container logs; they are JSON and carry `request_id`.
- After changing app code, **rebuild** — the stack runs an image.
- If it really is a defect, write the failing test first, then fix it.
