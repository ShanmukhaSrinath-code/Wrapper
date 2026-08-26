# The Common Application Base — how it is wired, and what it saves

A one-sitting read for a technical audience. The claim being made is narrow and
testable: **a new application built on this base starts with production plumbing
already working, and a new feature inside it costs feature code only.**

The proof is on the `poc/support-tickets` branch: a complete ticketing feature —
7 endpoints, a table, 2 background jobs, caching, file attachments, audit —
added in **712 lines across 5 modules**, with **zero lines changed** in the
infrastructure. See [POC_TICKETS.md](POC_TICKETS.md).

---

## 1. The one idea

```
app/core/      infrastructure.  ~2,970 lines.  Written once. Do not edit.
app/services/  business logic.  Auto-discovered. This is where you work.
```

Everything else follows from that split. A feature is a folder you drop into
`app/services/`; the base finds it and applies the plumbing to it.

This is not a naming convention that erodes in month three. It is a build gate:

```
$ make lint
Infrastructure must not depend on business logic KEPT
Features may not import each other        KEPT
Contracts: 2 kept, 0 broken.
```

An import from `app.core` into `app.services` **fails CI**. The architecture
cannot rot quietly.

---

## 2. What "auto-discovered" means

`app/core/discovery.py` walks the `app.services` package at startup and looks
for three things:

| You write in your folder | The base does this, with no registration step |
|---|---|
| `router = APIRouter(...)` | mounts it on the app — no edit to `main.py` |
| `@celery_app.task(...)` | registers it with the worker, with retries attached |
| a SQLAlchemy model | puts it in `Base.metadata`, so Alembic autogenerate sees it |

One design decision here is worth calling out. Python's `pkgutil` silently
swallows import errors while walking a package. The base overrides that: a
plugin that fails to import **stops the process at startup** with the real
traceback. A half-loaded application that returns 404 for a route you know you
wrote is a far more expensive failure than a container that refuses to boot.

The one thing discovery does *not* do is create your table. That still takes
`make revision` and `make migrate` — deliberately. Nothing here writes DDL
behind your back.

---

## 3. The blocks, and the wire between each pair

### The request path

```
                          ┌──────────────── Correlation ─────────────────┐
                          │  request_id · trace_id · span_id · actor      │
                          │  one contextvar set, read by everything below │
                          └───────────────────────────────────────────────┘
   client
     │  HTTP
     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ FastAPI (app/main.py)                                               │
│                                                                     │
│  Correlation ─ CORS ─ SecurityHeaders ─ RequestSizeLimit  (outer→in) │
│                              │                                      │
│                              ▼                                      │
│              your route in app/services/<feature>/router.py          │
│                              │                                      │
│    DbSession ── cache ── get_storage() ── enqueue() ── write_audit() │
└───────┬────────────┬──────────────┬────────────┬──────────┬─────────┘
        │            │              │            │          │
        ▼            ▼              ▼            ▼          ▼
   PostgreSQL     Redis          MinIO      Redis(broker)  audit_log
   (asyncpg)     (cache)        (S3 API)         │        (append-only)
                                                 ▼
                                          Celery worker
                                     (same code, same context)

  observability, applied to all of the above without being asked:
    structlog JSON → stdout → Promtail → Loki ─┐
    OpenTelemetry spans → Tempo ───────────────┼→ Grafana (one pane)
    prometheus-fastapi-instrumentator → /metrics → Prometheus ─┘
    unhandled 5xx → Sentry
```

### Component by component

| Block | Its job | How it is wired in | What you would otherwise write |
|---|---|---|---|
| **FastAPI** | HTTP, validation, OpenAPI | `create_app()` builds it; your router is discovered and mounted | app factory, router registry, middleware order |
| **PostgreSQL** | system of record | `DbSession` dependency: one session per request, commits on success, rolls back on exception | engine tuning, session lifecycle, pool pre-ping |
| **Alembic** | schema change | `target_metadata = Base.metadata`, models auto-imported first | env wiring, plus the destructive-op guard below |
| **Redis (cache)** | read-through cache | `cache.get_or_set(key, producer)` returns `(value, hit)` | client, timeouts, JSON codec, **outage → miss, not 500** |
| **Redis (broker)** | job queue | `enqueue("name", ...)` — refuses names nobody registered | broker config, the registry check |
| **Celery worker** | background work | runs the same image, discovers the same tasks | retry policy, context propagation, log format |
| **MinIO / S3** | bytes | `get_storage()` returns an interface: `put/get/delete/presigned_url` | boto3 wiring, thread offload, bucket bootstrap |
| **Audit** | who did what | `write_audit("thing.done", ...)` — actor and ids come from context | the table, the immutability, the attribution |
| **structlog** | logs | already configured; `get_logger(__name__)` | JSON renderer, stdlib bridge, uvicorn/SQLAlchemy capture |
| **OpenTelemetry** | traces | auto-instruments FastAPI, SQLAlchemy, Redis | provider, exporter, instrumentation, span naming |
| **Prometheus** | metrics | `/metrics` exposed; every route counted and timed | instrumentation, label discipline, scrape config |
| **Promtail → Loki** | log search | reads container stdout, parses the JSON | scrape config, label vs metadata decisions |
| **Tempo** | trace storage | OTLP receiver | receiver + storage config |
| **Grafana** | one pane of glass | datasources and dashboard provisioned as files | dashboards, and the log↔trace links |
| **Sentry** | error alerting | `configure_sentry()`; no DSN → no-op, logged as such | init, and the filtering that stops 404 noise |
| **Trivy** | vulnerability gate | CI scans the filesystem and the built image before it can be pushed | policy, allow-rules, SARIF upload |

### The four wires that matter most

**1. `request_id` is one value, and everything speaks it.**

```
client sends X-Request-ID (or the base mints one, and validates the format)
   → contextvar
      → every log line (structlog merges the context automatically)
      → the trace (span attribute) and the X-Trace-ID response header
      → the audit_log row (a column, not a note in a JSON blob)
      → the Celery message header → rebound in the worker process
      → Promtail lifts it into Loki as structured metadata
      → Grafana's derived field turns it into a "View trace" link
```

Practical consequence: a user reports "my upload failed at 14:32". You ask for
the request id from the error response, paste it into one Grafana box, and get
the log lines, the trace waterfall, and the audit row — including the work the
*worker* did minutes later. `make smoke` asserts exactly this end to end.

**2. The session is the transaction, and the route never manages it.**

Your route calls `session.add(...)` and `await session.flush()`. The dependency
commits when the response succeeds and rolls back on any exception. There is no
`try/except/commit` in feature code, so the class of bug where an error path
half-commits does not exist here.

**3. Errors have one shape, and expected failures are not alerts.**

`raise NotFoundError(...)` becomes a 404 in the standard JSON body, logged at
warning, **not** sent to Sentry. An unhandled exception becomes a 500 in the
same shape, logged once with its traceback, and reported. A 404 is not a bug;
treating it as one is how alerting gets ignored.

**4. Postgres does not trust the application.**

The app connects as `appruntime`, which has DML on business tables and
`SELECT, INSERT` on `audit_log` — no `UPDATE`, no `DELETE`, no `TRUNCATE`, and
no ownership, so it cannot drop the triggers that also block those. Migrations
connect as a different, owning role. An SQL-injection bug in feature code still
cannot rewrite history.

---

## 4. The safety rails, briefly

These are the parts that are hard to justify budget for at project start and
expensive to retrofit at project end.

| Rail | What it stops |
|---|---|
| Destructive-migration guard | `make revision` refuses to autogenerate a `DROP` unless `ALLOW_DESTRUCTIVE=1`. The usual cause of a phantom drop is a model that failed to import — this catches it instead of executing it. |
| Append-only audit | grants + ownership + row **and statement** level triggers. `TRUNCATE` does not fire row triggers, which is why the statement-level one exists. |
| Prod credential guard | the app **refuses to start** outside `local` on shipped default secrets, and names the offending fields. |
| CORS defaults to deny | `*` is available but has to be typed on purpose. |
| Cache degradation | Redis down is a cache miss, not an outage. |
| Task retries | transient errors retry with exponential backoff and jitter; a `RuntimeError` fails once, because it is a bug not a blip. |
| `enqueue` registry check | a route cannot return `202 Accepted` for a task name nobody registered. |
| Trivy gate | the image is scanned **before** the push step can run. |
| Docs CSP | Swagger's inline scripts are pinned by SHA-256 computed from the bytes being served, rather than allowing `unsafe-inline`. |

---

## 5. What this saves

Rough, honest estimates for one competent engineer building this from an empty
repo — not the happy-path version, the version that survives a security review.

| Capability | From scratch | On the base |
|---|---|---|
| App skeleton, config, settings validation | 2–3 days | done |
| Async DB layer, sessions, migrations, roles | 4–5 days | done |
| Structured logging + correlation across processes | 3–5 days | done |
| Tracing, wired to logs | 2–3 days | done |
| Metrics + provisioned dashboards | 3–4 days | done |
| Error contract + Sentry filtering | 2 days | done |
| Object storage abstraction + presigned URLs | 2 days | done |
| Celery with retries and context propagation | 3–4 days | done |
| Audit trail that is actually append-only | 3 days | done |
| Local stack (10 services, provisioned) | 4–5 days | done |
| CI, coverage gate, image build, vuln scanning | 3–4 days | done |
| Health probes, security headers, size limits | 2 days | done |
| **Total** | **≈ 6–8 weeks** | **`make up`** |

Two effects that outlast the initial saving, and matter more:

- **The second, third and fourth application are consistent.** Same log fields,
  same error shape, same dashboards, same deploy story. One runbook, not four.
  An engineer moving between them is productive on day one.
- **Improvements are shared.** Hardening the retry policy or tightening a header
  happens in one place. Today's POC feature inherited a retry policy nobody on
  the feature side had to think about — and would inherit tomorrow's improvement
  the same way.

The cost side, stated plainly: the team has to learn the seams, and one repo
becomes a shared dependency that needs an owner. Both are real; neither is
6 weeks per project.

---

## 6. What is *not* done yet

Say this before someone finds it. None of it blocks a POC; all of it blocks
production.

| Gap | Where it lands when it is filled |
|---|---|
| **Auth is a stub.** `get_current_user()` returns a `dev` principal. | One file — `app/core/security/current_user.py`. Every route already takes `user: CurrentUser`, so no route signature changes. That is the entire point of taking the dependency today. |
| **No rate limiting or quotas.** | New middleware; no feature code changes. |
| **Secrets provider is env-only.** | `AzureKeyVaultSecrets` is a declared stub; the interface is already in place. |
| **Authorisation is not modelled.** | Roles ride on the principal already; policy (Casbin or similar) is not written. |
| **The demo scaffolding is still in `app/services/`.** | `demo.py`, `demo_tasks.py`, `files.py` prove the plumbing and feed `make smoke`. Delete with their tests when real features land. |
| **One test is environment-dependent.** | `tests/unit/test_single_error_record.py` — a "unit" test that needs Postgres. Known, small, and worth fixing before the base is handed to a second team. |

---

## 7. Suggested slide order

1. The problem — every new service re-implements the same fortnight of plumbing, slightly differently each time.
2. The one idea — `core/` vs `services/`, enforced by CI, not by good intentions.
3. The block diagram (§3) — talk the request path once, end to end.
4. The `request_id` wire (§3, wire 1) — this is the demo that lands: one id, four systems, one Grafana box.
5. The POC — 712 lines, zero infrastructure edits, 25 unit + 16 integration tests green. Run it live.
6. The savings table (§5), then the consistency argument, which is the bigger one.
7. What is missing (§6) and what it would take. Credibility comes from this slide, not the previous one.
