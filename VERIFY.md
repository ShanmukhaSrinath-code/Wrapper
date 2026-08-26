# Visual verification

One command, then four browser tabs. No `curl` required.

```bat
run.cmd
```

That builds and starts 10 services, waits for the API, applies migrations, seeds
realistic traffic, checks all 16 blocks, proves the correlation contract, and
opens the UIs. Add `--no-open` to skip the browser tabs, `--stop` to tear it all
down.

What a healthy run prints:

```
  [OK  ] FastAPI + discovery      7 ticket operations on 4 paths, auto-mounted (no edit to main.py)
  [OK  ] PostgreSQL               row read back, requester='dev' (from the principal)
  [OK  ] Redis cache              cold read = MISS, second read = HIT
  [OK  ] Cache invalidation       after a write: MISS
  [OK  ] Error contract           illegal transition -> 409 conflict, allowed=['closed', 'in_progress']
  [OK  ] 404 handling             unknown id -> 404, ids stamped on the error body
  [OK  ] MinIO (object store)     uploaded, 307 to a presigned URL, 43 bytes match
  [OK  ] Celery worker            task success, 7+5=12, inherited request_id=...
  [OK  ] Audit trail              row a3ac068e... written under request_id=...
  [OK  ] Health probes            live=200, ready=200, {'redis': 'ok', 'postgres': 'ok', 'storage': 'ok'}
  [OK  ] Security headers         all four present
  [OK  ] Prometheus /metrics      counters exposed, labelled by route template
  [OK  ] Prometheus (scrape)      target up, 1021 requests counted since the app started
  [OK  ] Grafana                  v11.4.0, datasources=['Loki', 'Prometheus', 'Tempo'], 1 dashboard(s)
  [OK  ] Loki (logs)              9 JSON log lines for request_id=seed-probe-...
  [OK  ] Tempo (traces)           trace a61dfb6c28a9... has 11 spans

  All 16 blocks reporting healthy.
  Probe request_id = seed-probe-706b64e9
  Probe trace_id   = a61dfb6c28a9...
  SMOKE PASSED - one request_id joins logs, traces, audit and metrics.
```

**Keep the two probe values on screen.** They are what you paste into Grafana.

---

## Tab 1 — Swagger UI · <http://localhost:8000/docs>

Where to click, in order. Swagger shows response **headers** as well as bodies,
which is what makes the cache visible without a terminal.

| # | Do this | Look for |
|---|---|---|
| 1 | Expand **tickets**. | Seven operations. Nothing registered them — `discover_routers()` found the module. |
| 2 | `POST /tickets` → Try it out → body `{"title":"Demo from Swagger","priority":"high","assignee":"alice"}` → Execute | **201**. In the response, `"requester": "dev"` — taken from the authenticated principal, **not** from your body. Copy the `id`. |
| 3 | `GET /tickets/{ticket_id}` with that id → Execute | **200**. Scroll to *Response headers*: **`x-cache: MISS`**. |
| 4 | Execute the *same* call again | **`x-cache: HIT`** — that was Redis, not Postgres. |
| 5 | `PATCH /tickets/{ticket_id}` body `{"status":"resolved"}` | **409**. Body says `"error":"conflict"` and `"allowed":["closed","in_progress"]` — the API tells the client what *was* legal. |
| 6 | Same PATCH with `{"status":"in_progress"}` | **200**, status changed. |
| 7 | `GET /tickets/{ticket_id}` again | **`x-cache: MISS`** — the write invalidated the cache. |
| 8 | `POST /tickets/{ticket_id}/attachment` → choose any small file | **200**, `attachment_name` set. |
| 9 | `GET /tickets/{ticket_id}/attachment` | **307** and a `location` containing `X-Amz-Signature` — a presigned MinIO URL. Bytes bypass the API. |
| 10 | `GET /tickets/stats` twice | `x-cache` **MISS** then **HIT**, and real counts by status/priority. |
| 11 | `GET /tickets/{ticket_id}` with `00000000-0000-0000-0000-000000000000` | **404**, and note `request_id` + `trace_id` **inside the error body**. Copy the `request_id` — you will use it in Tab 2. |
| 12 | `GET /tickets/{ticket_id}` with `not-a-uuid` | **422** from Pydantic, before any of our code runs. |
| 13 | Expand **demo** → `POST /demo/job` → Execute | **202** with a `task_id`. |
| 14 | `GET /demo/job/{task_id}` → Execute a couple of times | `PENDING` → `STARTED` → `SUCCESS` with a result. That ran in the **worker container**. |
| 15 | `GET /demo/boom` | **500** in the same error shape, with ids. This is the one that gets logged with a traceback and reported to Sentry. |
| 16 | Expand **health** → `GET /health/ready` | `{"status":"ok","checks":{"postgres":"ok","redis":"ok","storage":"ok"}}` |

Every response carries `x-request-id` and `x-trace-id`. That is the thread for
everything below.

---

## Tab 2 — Grafana · <http://localhost:3001/d/common-app-base>

No login (anonymous viewer is provisioned).

**Panels — what "working" looks like:**

| Panel | Healthy |
|---|---|
| Request rate (req/s by route) | several lines, labelled `/tickets`, `/tickets/{ticket_id}`, `/tickets/stats` |
| Error rate (5xx %) | a few percent — the seeding deliberately makes some 500s |
| Latency p50/p95/p99 | three lines, p95 around 100 ms locally |
| Responses by status | 200, 201, 404, 409, 500 all present |
| Requests in flight | small non-zero while traffic runs |
| Total requests (30m) | hundreds |
| Target up | **1** |

> **Flat at zero is not broken.** Every rate panel is rate-based, so an idle app
> genuinely reads zero. Re-run `run.cmd --no-open` to re-seed, or just hit
> Swagger a few times and watch it move (the dashboard refreshes every 10s).

**The correlation demo — this is the one to rehearse:**

1. Paste the **probe request_id** into the `request_id` textbox at the top.
2. The **Logs** panel at the bottom filters to just that request.
3. Expand a log line. Every field is there: `event`, `logger`, `request_id`,
   `trace_id`, `span_id`, `service`, `environment`.
4. Click the **"View trace"** button on the `trace_id` field → jumps to Tab 3.

Then repeat with the `request_id` you copied from the Swagger 404. Same trick,
live.

**To see both processes**, go to **Explore → Loki** and run:

```logql
{service=~"app|worker"} | json | request_id = "PASTE_YOUR_ID"
```

You get ~9 lines: the API handling the request, *and* the worker doing the
follow-up work minutes later, under the same id. Nothing in the feature code
passed that id across.

Other Loki queries worth showing:

```logql
{service="app", level="error"}
{service="app"} | json | event = "ticket.created"
{service="worker"}
```

> Expect a second or two of Promtail ingestion lag. If a query returns nothing,
> wait and re-run — it is not broken.

---

## Tab 3 — Traces (Grafana → Explore → Tempo)

Paste the **probe trace_id**, or use **Search** to browse recent traces.

A single `POST /tickets` looks like this:

```
POST /tickets                 19.33 ms
  ├─ INSERT (ticket row)       1.61 ms
  ├─ INSERT (audit row)        1.00 ms
  ├─ DEL    (cache invalidate) 0.54 ms
  └─ LPUSH  (Celery publish)   0.49 ms
```

**Nobody instrumented that.** SQLAlchemy and Redis are auto-instrumented, so the
database writes, the cache invalidation and the queue publish are all visible
inside one request. Best single visual in the whole demo.

From a span, Tempo links **back** to the logs (`tracesToLogsV2`). Log → trace →
log is a two-click round trip.

> `run.cmd` enables trace export (`--profile tracing`). With a plain `make up`
> Tempo is absent and traces are not shipped — `trace_id` still appears in logs
> and correlates. That is deliberate, not a fault.

---

## Tab 4 — Prometheus · <http://localhost:9090/targets>

Three targets, all **UP**: `common-app-base` (`http://app:8000/metrics`),
`loki`, `prometheus`.

Then **Graph** and paste:

```promql
sum by (handler) (rate(app_http_requests_total[1m]))
sum by (status) (rate(app_http_requests_total[1m]))
histogram_quantile(0.95, sum by (le) (rate(app_http_request_duration_seconds_bucket[5m])))
```

Look at the `handler` label values: **`/tickets/{ticket_id}`, never a real id.**
One series per id would be unbounded cardinality and would eventually kill
Prometheus. That discipline is built into the base, not left to each feature.

---

## Tab 5 — MinIO · <http://localhost:9001>

Login `minioadmin` / `minioadmin` → **Object Browser** → the app bucket.

Navigate to `tickets/<ticket-id>/` and you will see the file you uploaded in
Swagger step 8. Compare with the database:

```bat
docker compose -f deploy/docker-compose.yml exec -T postgres psql -U appuser -d appdb -c "SELECT attachment_name, attachment_key FROM ticket WHERE attachment_key IS NOT NULL LIMIT 5;"
```

Postgres holds the **key**; MinIO holds the bytes. Swapping MinIO for real S3 is
a config change — no feature code imports `boto3`.

---

## The three proofs that need a terminal

Not everything is visual. These three are worth the two lines each.

**1. The app cannot rewrite its own audit history:**

```bat
docker compose -f deploy/docker-compose.yml exec -T postgres psql -U appruntime -d appdb -c "TRUNCATE audit_log;"
docker compose -f deploy/docker-compose.yml exec -T postgres psql -U appruntime -d appdb -c "DELETE FROM audit_log;"
```

Both: `ERROR: permission denied for table audit_log`. Grants **and** triggers,
and the app does not own the table so it cannot drop the triggers.

**2. A dependency outage degrades instead of failing:**

```bat
docker compose -f deploy/docker-compose.yml stop redis
```

Now in Swagger: `GET /tickets/{id}` still returns **200** (`x-cache: MISS`
forever), while `GET /health/ready` returns **503** naming redis. Liveness stays
200, so Kubernetes would take the pod out of the load balancer rather than kill
it. Then:

```bat
docker compose -f deploy/docker-compose.yml start redis
```

**3. The architecture boundary is a build gate:**

```bat
.venv\Scripts\lint-imports.exe
```

```
Infrastructure must not depend on business logic KEPT
Features may not import each other                KEPT
Contracts: 2 kept, 0 broken.
```

An import from `app.core` into `app.services` fails CI. Layering is enforced,
not merely documented.

---

## Trivy — the supply-chain gate

```bat
make scan
```

(This target works; `make test`/`lint`/`smoke` do not, because they call `uv`,
which is not on this PATH. Use the `.venv\Scripts\...` equivalents.)

| Scan | Clean result |
|---|---|
| `trivy fs` — vuln + secret + misconfig | `uv.lock` 0 vulns · 8 config files 0 misconfigs · 0 secrets |
| `trivy image common-app-base:local` | Debian 12.15, 115 OS packages 0 · all Python packages 0 |

Both exit **0**. Four points worth making:

1. **Three scanners in one command** — dependency CVEs, leaked secrets, and
   Kubernetes/Dockerfile misconfiguration.
2. **`ignore-unfixed: true`** — a CVE with no patch is not actionable, and
   failing the build on it just teaches people to ignore the gate.
3. **Scan before push.** CI builds with `push: false, load: true`, scans, and
   only then may push. A vulnerable image cannot reach the registry. Findings
   also upload as SARIF to the GitHub Security tab.
4. **Allow-rules are path-scoped**, not global: `KSV-0109` is suppressed only
   for `deploy/k8s/configmap.yaml`, and the secret allow-rules cover only files
   that legitimately hold local dev defaults. A real secret in a real source
   file still fails the build.

To show the gate *failing*, re-run with `--severity MEDIUM`.

---

## Suggested 10-minute demo order

1. `run.cmd` — one command, 16 OK lines, `SMOKE PASSED`. Say: this is a fresh machine.
2. Swagger steps 2–4 — 201, then `x-cache` MISS → HIT.
3. Swagger step 5 — the 409 that explains itself.
4. Grafana — paste the request_id, logs filter, click **View trace**.
5. The trace waterfall — two INSERTs, a DEL, an LPUSH. "Nobody wrote this."
6. Loki `{service=~"app|worker"}` — one id, two processes.
7. `TRUNCATE audit_log` → permission denied.
8. `stop redis` → 200 with MISS, ready 503 naming redis.
9. `make scan` → 0 HIGH/CRITICAL, and *scan before push*.
10. `lint-imports` → 2 contracts kept. "The architecture cannot rot quietly."
