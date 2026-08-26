# POC: support tickets

A working feature built on the base, and a **component-by-component test guide**
you can follow live in front of an audience.

Branch layout:

| Branch | Contents |
|---|---|
| `master` | the base only — share this one |
| `poc/support-tickets` | the base **plus** this feature. Nothing in `app/core/**` or `app/main.py` differs from `master`. |

Verify that last claim yourself:

```bash
git diff --stat master..poc/support-tickets -- app/core app/main.py
# (no output)
```

---

## What the POC does

A support-ticket tracker. Chosen because it needs every block of the base at
once, not because ticketing is interesting.

| Endpoint | Exercises |
|---|---|
| `POST /tickets` | Postgres write · audit · Celery publish · cache invalidation |
| `GET /tickets` | filtered + paginated read, with an honest total |
| `GET /tickets/stats` | cached aggregate (`X-Cache: HIT`/`MISS`) |
| `GET /tickets/{id}` | read-through cache |
| `PATCH /tickets/{id}` | status machine → `409` on an illegal move · audit diff · invalidation |
| `POST /tickets/{id}/attachment` | MinIO write, key in Postgres |
| `GET /tickets/{id}/attachment` | `307` to a presigned URL |

Plus two background jobs:

| Task | Point being made |
|---|---|
| `tickets.notify_assignee` | correlation crosses the process hop; audit written from the worker |
| `tickets.escalate_stale` | a worker owning its own session, transaction and cleanup |

### The files

```
app/services/tickets/
  __init__.py    docstring only
  models.py       40 lines   the `ticket` table
  schemas.py      66 lines   wire shapes, separate from the ORM
  service.py     195 lines   status machine, cache keys, aggregates
  router.py      256 lines   7 endpoints
  tasks.py       155 lines   2 tasks
migrations/versions/20260826_1015_ticket_table.py
tests/unit/test_tickets.py              25 tests, no stack needed
tests/integration/test_tickets_flow.py  16 tests, real Postgres/Redis/MinIO/Celery
```

**Zero** lines of infrastructure changed.

---

## Run it

```bash
git checkout poc/support-tickets
make up                     # 9 services
make migrate                # creates the `ticket` table
```

Then, in one paste:

```bash
# create
TICKET=$(curl -s -X POST localhost:8000/tickets \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-001' \
  -d '{"title":"Printer on fire","priority":"high","assignee":"alice"}')
ID=$(echo "$TICKET" | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# read twice -- watch X-Cache flip
curl -si localhost:8000/tickets/$ID | grep -i x-cache   # MISS
curl -si localhost:8000/tickets/$ID | grep -i x-cache   # HIT

# an illegal transition
curl -s -X PATCH localhost:8000/tickets/$ID \
  -H 'Content-Type: application/json' -d '{"status":"resolved"}' | python -m json.tool

# a legal one -- and the cache is dropped
curl -s -X PATCH localhost:8000/tickets/$ID \
  -H 'Content-Type: application/json' -d '{"status":"in_progress"}' > /dev/null
curl -si localhost:8000/tickets/$ID | grep -i x-cache   # MISS again

# attach a file, then get a presigned URL
echo "evidence" > /tmp/evidence.txt
curl -s -X POST localhost:8000/tickets/$ID/attachment -F file=@/tmp/evidence.txt > /dev/null
curl -si localhost:8000/tickets/$ID/attachment | grep -i location

# the cached aggregate
curl -si localhost:8000/tickets/stats | grep -iE 'x-cache|^\{'
```

Swagger is at <http://localhost:8000/docs> if you would rather click.

---

## Testing each component

This is the part to rehearse. Each block gets: **the one-line proof**, and
**what to say**.

### 1. FastAPI + auto-discovery

```bash
curl -s localhost:8000/openapi.json | python -c \
 "import json,sys; print([p for p in json.load(sys.stdin)['paths'] if 'ticket' in p])"
```

> Seven ticket paths are in the schema. `app/main.py` has never heard of
> tickets — `discover_routers()` found the module-level `router`.

Negative proof — break it on purpose:

```bash
echo "import nonexistent_module" >> app/services/tickets/models.py
# No source is mounted into the container -- the image is baked -- so run
# discovery locally rather than restarting, which would just re-run old code.
.venv/Scripts/python.exe -c   "from app.core.discovery import import_discovered_models; import_discovered_models()"
git checkout app/services/tickets/models.py
```

Expect `PluginImportError: models module 'app.services.tickets.models' failed to
import`. To see the same thing kill a container, rebuild rather than restart:
`docker compose -f deploy/docker-compose.yml up -d --build app`.

> A broken plugin stops the process. It does **not** boot with a missing route.

### 2. PostgreSQL + the session dependency

```bash
docker compose -f deploy/docker-compose.yml exec postgres \
  psql -U appuser -d appdb -c \
  "SELECT id, title, status, priority, requester FROM ticket ORDER BY created_at DESC LIMIT 3;"
```

> One session per request, committed by the dependency on success. The route
> only ever calls `flush()`.

Prove the rollback half:

```bash
docker compose -f deploy/docker-compose.yml exec postgres \
  psql -U appuser -d appdb -c "SELECT count(*) FROM example;"
curl -s localhost:8000/demo/boom > /dev/null      # raises after a DB write
# count is unchanged: the failed request rolled back
```

### 3. Least-privilege role

```bash
docker compose -f deploy/docker-compose.yml exec postgres \
  psql -U appruntime -d appdb -c "DELETE FROM audit_log;"
# ERROR: permission denied for table audit_log
docker compose -f deploy/docker-compose.yml exec postgres \
  psql -U appruntime -d appdb -c "TRUNCATE audit_log;"
# ERROR: permission denied
```

> The application's own credentials cannot rewrite history. Grants **and**
> triggers, and the app does not own the table so it cannot drop the triggers.

### 4. Alembic + the destructive guard

```bash
make revision m="try to drop something"   # after commenting out a column
# DestructiveMigrationError: refusing to autogenerate DROP COLUMN ...
```

> A `DROP` is never autogenerated silently. The usual cause is a model that
> failed to import, which would otherwise look like a deleted table.

### 5. Redis cache

```bash
curl -si localhost:8000/tickets/$ID | grep -i x-cache    # MISS
curl -si localhost:8000/tickets/$ID | grep -i x-cache    # HIT
docker compose -f deploy/docker-compose.yml exec redis redis-cli KEYS 'ticket:*'
```

Now the important one — **degradation**:

```bash
docker compose -f deploy/docker-compose.yml stop redis
curl -si localhost:8000/tickets/$ID | head -1     # still HTTP/1.1 200 OK
curl -si localhost:8000/tickets/$ID | grep -i x-cache   # permanent MISS
docker compose -f deploy/docker-compose.yml start redis
```

> Redis down is a cache miss, not an outage. No feature code has a `try/except`
> around a cache call.

### 6. MinIO

```bash
curl -s -X POST localhost:8000/tickets/$ID/attachment -F file=@/tmp/evidence.txt | python -m json.tool
```

Then look in the console at <http://localhost:9001> (`minioadmin`/`minioadmin`)
under `tickets/<id>/`, and:

```bash
docker compose -f deploy/docker-compose.yml exec postgres psql -U appuser -d appdb \
  -c "SELECT attachment_name, attachment_key FROM ticket WHERE id = '$ID';"
```

> Bytes in the object store, key in Postgres. The `307` hands the client a
> presigned URL so downloads never pass through the API.

### 7. Celery worker + correlation across processes

```bash
curl -s -X POST localhost:8000/tickets \
  -H 'Content-Type: application/json' -H 'X-Request-ID: worker-demo-001' \
  -d '{"title":"Assigned ticket","assignee":"bob"}' > /dev/null

docker compose -f deploy/docker-compose.yml logs --tail=30 worker | grep worker-demo-001
```

> The worker is logging under the **API request's** id. Nothing in the feature
> passed it: the base put it on the message headers and rebound it in
> `task_prerun`.

And the audit row the worker wrote:

```bash
docker compose -f deploy/docker-compose.yml exec postgres psql -U appuser -d appdb -c \
  "SELECT action, actor_id, request_id FROM audit_log WHERE request_id='worker-demo-001';"
```

> Two rows, one id: `ticket.created` from the API, `ticket.assignee_notified`
> from the worker.

Retries:

```bash
curl -s -X POST 'localhost:8000/demo/job?fail=true' | python -m json.tool
docker compose -f deploy/docker-compose.yml logs worker | grep -i retry
```

### 8. Audit trail

```bash
docker compose -f deploy/docker-compose.yml exec postgres psql -U appuser -d appdb -c \
  "SELECT action, resource_id, detail FROM audit_log WHERE resource_type='ticket'
   ORDER BY created_at DESC LIMIT 5;"
```

> `ticket.updated` carries the **diff**, not the whole row — so "what did this
> request change" is answerable from one row.

### 9. Structured logging → Loki

```bash
docker compose -f deploy/docker-compose.yml logs --tail=5 app
```

> JSON, one object per line, every line carrying `request_id`, `trace_id`,
> `span_id`, `service`, `environment`.

In Grafana (<http://localhost:3001>) → Explore → Loki:

```logql
{service="app"} | json | request_id = `demo-001`
```

### 10. Prometheus metrics

```bash
curl -s localhost:8000/metrics | grep 'handler="/tickets'
```

> Labelled by route **template**, never by id — `/tickets/{ticket_id}`, not
> `/tickets/3bc4…`. That is the difference between a working Prometheus and a
> melted one.

In Grafana → the provisioned dashboard, or Prometheus at <http://localhost:9090>:

```promql
sum by (handler) (rate(app_http_requests_total[1m]))
histogram_quantile(0.95, sum by (le) (rate(app_http_request_duration_seconds_bucket[5m])))
```

Generate traffic first or the rate panels read zero — they are rate-based and an
idle app really is zero.

### 11. OpenTelemetry traces

```bash
curl -si localhost:8000/tickets/$ID | grep -i x-trace-id
```

> A real trace id on every response, and it is the same value in the logs and
> the audit row. Locally the *export* is off, so Tempo is empty until you run
> `--profile tracing` with `OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318` —
> correlation works either way. That is deliberate, not broken.

### 12. Errors and Sentry

```bash
curl -s localhost:8000/tickets/00000000-0000-0000-0000-000000000000 | python -m json.tool
curl -s -X PATCH localhost:8000/tickets/$ID -H 'Content-Type: application/json' \
  -d '{"status":"resolved"}' | python -m json.tool     # 409, from `open`
curl -s localhost:8000/demo/boom | python -m json.tool  # 500
```

> One error shape for all three. The 404 and the 409 are logged as **warnings**
> and never reach Sentry; the 500 is logged once with its traceback and is
> reported. Locally Sentry has no DSN and says so in the logs — also deliberate.

### 13. Security headers, size limit, CORS

```bash
curl -sI localhost:8000/health/live | grep -iE 'content-security|x-frame|x-content|referrer'
head -c 11000000 /dev/urandom > /tmp/big.bin   # limit is 10 MiB
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/tickets/$ID/attachment -F file=@/tmp/big.bin
curl -si -X OPTIONS localhost:8000/tickets -H 'Origin: http://evil.example' \
  -H 'Access-Control-Request-Method: POST' | head -1
```

> `413` from the middleware, and CORS defaults to deny.

### 14. Health probes

```bash
curl -s localhost:8000/health/ready | python -m json.tool
docker compose -f deploy/docker-compose.yml stop redis
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/health/ready   # 503
curl -s localhost:8000/health/ready | python -m json.tool              # names redis
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/health/live     # still 200
docker compose -f deploy/docker-compose.yml start redis
```

> Liveness does no I/O, so a dependency outage does not get the pod killed;
> readiness names the failing dependency, so it gets taken out of the load
> balancer instead.

### 15. The whole correlation contract, in one command

```bash
make smoke
```

> Drives an upload, a job and an error, then independently asserts the same
> `request_id` shows up in Loki, in the trace, in `audit_log`, and in the
> Prometheus counters. This is the demo to run if you only run one.

### 16. The architecture boundary

```bash
make lint
```

> `Contracts: 2 kept, 0 broken.` An import from `app.core` into `app.services`
> fails the build. The layering is a gate, not a guideline.

### 17. The test suites

```bash
make test                              # everything, 80% coverage gate
.venv/Scripts/python.exe -m pytest tests/unit/test_tickets.py -q          # 25, no stack
.venv/Scripts/python.exe -m pytest tests/integration/test_tickets_flow.py -q  # 16, real stack
make typecheck                         # mypy, strict on app/
make scan                              # Trivy: filesystem + image
```

Integration tests **skip with a reason** when the stack is down, so a missing
stack can never look like a passing suite.

---

## Two bugs this POC found, and what they teach

Worth mentioning out loud: writing the POC surfaced two real traps. Both were in
*feature* code, both are the kind of thing a base cannot prevent for you, and
both are now commented at the site so the next person does not repeat them.

**1. `updated_at` after an UPDATE.** The column is maintained by the database
(`onupdate=func.now()`). After `flush()` on an update, SQLAlchemy marks the
attribute stale; reading it triggers a lazy load, and lazy IO under async raises
`MissingGreenlet` — which surfaces as a 500. Fix: `await session.refresh(row)`
after an update flush. Inserts are fine, because they get their defaults back
via `RETURNING`.

**2. Loop-bound singletons in a Celery task.** The engine and the Redis client
are process globals bound to the loop that created them, and `asyncio.run` gives
each task run a *new* loop. The first invocation works; the second fails with
"attached to a different loop". Fix: the `_loop_scoped_resources()` context
manager in `tasks.py` disposes both before the loop closes. Note the failure
mode — it only appears on the **second** call, which is why the integration test
that runs a task through the real worker is worth having.

Both were caught by the integration suite before anything was committed, which
is the argument for having one.

---

## Numbers for the slide

| | |
|---|---|
| Feature code | 712 lines across 5 modules |
| Infrastructure changed | **0 lines** |
| Endpoints | 7 |
| Background tasks | 2 |
| Tests added | 25 unit + 16 integration, all passing |
| Gates | ruff ✅ · ruff format ✅ · import-linter 2/2 ✅ · mypy ✅ |
