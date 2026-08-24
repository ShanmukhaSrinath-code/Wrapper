# TEST_REPORT.md — Acceptance & Extensibility Audit

**Subject:** Common Application Base (`common-app-base`)
**Commit under test:** `22319b3` (branch `master`)
**Date:** 2026-08-24
**Protocol:** `TEST_BRIEF.md`
**Auditor stance:** adversarial verifier. Every PASS below is backed by pasted output from this machine.

> **Auth is out of scope.** Per the brief, no authentication/authorization testing was performed
> beyond check **A6** (the stub is a harmless, injectable seam). The absence of auth is **not**
> recorded as a defect.

---

## VERDICT

# NOT READY

One zero-tolerance failure: **D2** — adding a feature required editing two protected
base-infrastructure modules. 2 blockers, 5 major, 5 minor defects. Full reasoning at the end.
Every check passed **twice**. The correlation contract held under every probe.

---

## Environment

| Item | Value |
|---|---|
| OS | Windows 11 Pro 26200, Git Bash |
| Docker | 29.5.3 |
| uv | 0.12.5 |
| GNU Make | 4.4.1 |
| Trivy | v0.74 |
| kubeconform | present |

**Clean-clone precondition (Rule 4) satisfied:**

```
$ docker compose -f deploy/compose/docker-compose.yml --profile tracing down -v
 Volume common-app-base_postgres-data Removed        (+ redis, minio, loki, grafana, prometheus, tempo)
 Network common-app-base_default Removing

$ git clean -xfd
Removing .coverage / .mypy_cache/ / .pytest_cache/ / .ruff_cache/ / .tmp/ / .venv/ / **/__pycache__/
(22 entries)

$ git status --porcelain
(empty - clean)
```

---

## PART A — Structure & boundary audit

| # | Check | Result |
|---|---|---|
| A1 | Repo matches the target tree | **PASS** |
| A2 | All 15 gates recorded PASS | **PASS** |
| A3 | No hardcoded secrets | **DEFECT (major)** — scanner is clean, but dev credentials are literal defaults with no prod guard |
| A4 | Config centralized | **PASS** |
| A5 | Cloud swap points isolated | **PASS** (spec deviation noted) |
| A6 | Auth stub is a harmless seam | **PASS** |

### A1 — Repo structure — PASS

```
$ find . -maxdepth 2 (pruned)
app/config.py  app/logging.py  app/observability.py  app/errors.py  app/main.py
app/api  app/audit  app/cache  app/db  app/jobs  app/middleware  app/security  app/storage
deploy/compose  deploy/docker  deploy/grafana  deploy/k8s  deploy/loki  deploy/prometheus
deploy/promtail  deploy/tempo
migrations/env.py  migrations/versions  scripts/smoke.py
tests/unit  tests/integration  tests/e2e  tests/conftest.py
Makefile  BUILD_LOG.md  README.md  pyproject.toml  uv.lock
trivy.yaml  trivy-secret.yaml  .trivyignore.yaml  .pre-commit-config.yaml

$ ls .github/workflows/
ci.yml   (8566 bytes)
```

Every artefact the brief names is present.

### A2 — All 15 gates PASS — PASS

```
$ grep -nE "^## Phase" BUILD_LOG.md
6:## Phase 0 — Scaffold & tooling — **PASS**
48:## Phase 1 — FastAPI skeleton + health — **PASS**
86:## Phase 2 — Docker + compose — **PASS**
122:## Phase 3 — PostgreSQL + migrations — **PASS**
175:## Phase 4 — Redis + caching — **PASS**
228:## Phase 5 — Structured logging + Correlation + Loki/Grafana — **PASS**
303:## Phase 6 — Metrics (Prometheus) + Grafana dashboard — **PASS**
375:## Phase 7 — OpenTelemetry tracing + Audit — **PASS**
458:## Phase 8 — Error handling + Sentry — **PASS**
547:## Phase 9 — File storage (MinIO, S3-compatible) — **PASS**
646:## Phase 10 — Background jobs (Celery) — **PASS**
724:## Phase 11 — Security (OWASP + scanning) — **PASS**
840:## Phase 12 — Testing (pytest + Playwright) — **PASS**
948:## Phase 13 — CI/CD + Kubernetes — **PASS**
1060:## Phase 14 — Full-stack correlation smoke test — **PASS**
```

Phases 0–14, none skipped, none FAIL. The three `FAIL` strings elsewhere in the file are a
deliberately-failing Celery task's status payload and two rows of the historical defect table,
not gate results.

### A3 — Hardcoded secrets — DEFECT (major)

**The scanner is clean and — importantly — is proven to actually work.**

```
$ trivy fs --scanners secret --exit-code 1 --severity HIGH,CRITICAL .
trivy exit whole repo = 0
```

Negative control: a scanner that finds nothing is worthless unless it can find something.
Planted realistic (non-`EXAMPLE`) credentials in `app/_negctl_leak.py`:

```
CRITICAL: AWS (aws-access-key-id)      _negctl_leak.py:1
CRITICAL: AWS (aws-secret-access-key)  _negctl_leak.py:2
CRITICAL: GitHub (github-pat)          _negctl_leak.py:3

trivy exit with planted secret = 1     <- build correctly fails
trivy exit clean app/          = 0     <- and correctly passes once removed
```

(My first control used AWS's published `AKIAIOSFODNN7EXAMPLE` documentation key, which Trivy
allow-lists by design; that control was invalid and was re-run with realistic values.)

`grep` for literal credential assignments in first-party code returns nothing:

```
$ grep -rniE "(password|secret|api_key|apikey|token)\s*[:=]\s*[\"'][^\"']{4,}[\"']" app/ scripts/ migrations/
(no output)
```

**However** — `app/config.py` ships working credentials as *field defaults*:

```
app/config.py:44:    postgres_password: str = "apppassword"   # noqa: S105 - local compose default, overridden by env
app/config.py:61:    s3_access_key: str = "minioadmin"
app/config.py:62:    s3_secret_key: str = "minioadmin"        # noqa: S105 - local compose default, overridden by env
```

There **is** an `environment: Literal["local","dev","staging","prod"]` setting (`config.py:33`),
but **no validator ties the two together**. Proven:

```
$ ENVIRONMENT=prod uv run python -c "from app.config import Settings; s=Settings(); ..."
environment      : prod
postgres_password: apppassword
s3_secret_key    : minioadmin
database_url     : postgresql+asyncpg://appuser:apppassword@localhost:5432/appdb
```

**Failure scenario:** a team clones the base, deploys to production, and forgets one key in the
K8s Secret. The app does not fail fast — it boots successfully using the publicly-documented
password `apppassword`, and every clone of this template shares it.

**Not scored as the Rule-3 zero-tolerance "hardcoded secret"**, because these are documented
local-dev placeholders rather than real credentials (Trivy's own allow-list treats them that way).
Scored **major** because the guard that would make them safe is absent.

**Fix:** add a `model_validator(mode="after")` to `Settings` that raises when
`environment != "local"` and any credential still equals its dev default.

### A4 — Config centralized — PASS

Exactly one environment read exists in the entire application, and it is *inside* the
`Secrets` provider in `config.py`:

```
$ grep -rnE "os\.environ|os\.getenv" app/ --include=*.py
app/config.py:162:        return os.environ.get(name, default)

$ (same grep) | grep -v "app/config.py"
(no output)

44 settings fields
```

No module reads the environment behind `Settings`' back.

### A5 — Cloud swap points isolated — PASS (spec deviation noted)

```
$ ls app/storage/
__init__.py  base.py  minio.py

Storage ABC abstract methods : ['delete','ensure_ready','exists','get','ping','presigned_url','put']
MinioStorage subclass of Storage : True
MinioStorage unimplemented       : none

app/storage/__init__.py:31: def get_storage(config: Settings | None = None) -> Storage
app/config.py:58:           storage_provider: Literal["minio"] = "minio"
```

Call sites depend on the `Storage` interface, never on boto3. The secrets seam is likewise an
ABC (`Secrets` -> `EnvSecrets` | `AzureKeyVaultSecrets`) dispatched by `SECRETS_PROVIDER`.

> **Deviation from the brief, not a defect.** A5 as written expects an `azure_blob` stub alongside
> `minio`. The repository owner explicitly instructed *"in the storage, i just need minio remove
> that azure blob"*, and the adapter was deleted. Because MinIO speaks the S3 API, the same adapter
> targets real S3 by changing endpoint + credentials. The **interface** — the thing A5 actually
> tests — is intact. The same deviation applies to the `azure_blob` clause of B9.

### A6 — Auth stub is a harmless seam — PASS

The seam is 56 lines, contains no authentication or authorization logic, and returns a constant.

**Every business route already depends on it; only probes and docs do not:**

```
  AUTH-SEAM  POST   /demo/audited                 no-seam  GET  /docs
  AUTH-SEAM  GET    /demo/boom                    no-seam  GET  /docs/oauth2-redirect
  AUTH-SEAM  GET    /demo/cached                  no-seam  GET  /health/live
  AUTH-SEAM  DELETE /demo/cached                  no-seam  GET  /health/ready
  AUTH-SEAM  POST   /demo/job                     no-seam  GET  /metrics
  AUTH-SEAM  GET    /demo/job/{task_id}           no-seam  GET  /redoc
  AUTH-SEAM  GET    /demo/not-found
  AUTH-SEAM  POST   /files
  AUTH-SEAM  GET    /files/{file_id}
  AUTH-SEAM  GET    /files/{file_id}/content
  AUTH-SEAM  GET    /files/{file_id}/download-url

APIRoutes: 17   depending on get_current_user: 11
```

The split is correct: probes and docs must not require an identity.

(Resolved by walking each route's FastAPI `dependant` tree through the `_IncludedRouter`
wrappers this FastAPI version uses. My first two introspection attempts reported `0` because they
did not recurse into `original_router`; that was a **bug in my probe, not in the app**, and was
fixed in the probe — no application code was touched.)

Harmless, and genuinely injectable:

```
default principal seen by app code: id='dev' name='Local Developer' roles=['dev'] tenant_id=None
after dependency_overrides swap   : id='alice' name='' roles=['admin'] tenant_id=None

open access confirmed: GET /demo/cached with no credentials -> HTTP 200
```

Swapping one dependency changes the principal every route sees, with no route signature touched —
exactly the property the seam exists to provide.

---

## PART B — Per-block functional & failure tests

| # | Block | Result |
|---|---|---|
| B1 | Health honesty | **PASS** |
| B2 | Database | **PASS** |
| B3 | Cache | **DEFECT (major)** — MISS/HIT/TTL correct; does **not** degrade gracefully when Redis is down |
| B4 | Logging + correlation | **PASS** (minor: 17 non-JSON worker banner lines) |
| B5 | Metrics | **PASS** |
| B6 | Tracing | **PASS** |
| B7 | Audit | **PASS on UPDATE/DELETE; DEFECT (major)** — `TRUNCATE` bypasses append-only |
| B8 | Errors | **PASS** (minor: one exception produces 3 error log records) |
| B9 | Storage | **PASS** |
| B10 | Jobs | **PASS** (minor: no retry policy configured) |
| B11 | Security hardening | **DEFECT (major)** — CORS defaults to `*`; headers/TLS/scan all pass |
| B12 | Tests | **PASS** |
| B13 | K8s manifests | **PASS** |

### B1 — Health honesty — PASS

Liveness never consults a dependency; readiness does, and names the failure.

```
BASELINE          live: 200   ready: 200
  {"status":"ok","checks":{"redis":"ok","storage":"ok","postgres":"ok"}}

docker stop cab-postgres
                  live: 200   ready: 503
  {"status":"degraded","checks":{"redis":"ok","storage":"ok","postgres":"error: timeout after 3s"}}

docker start cab-postgres  -> ready recovered after 1s -> 200

docker stop cab-redis
                  live: 200   ready: 503
  {"status":"degraded","checks":{"storage":"ok","postgres":"ok","redis":"error: timeout after 3s"}}

docker start cab-redis     -> ready recovered after 1s -> 200
```

Liveness stayed 200 throughout both outages — a slow dependency will not get healthy pods killed.
**No zero-tolerance violation:** readiness never returned 200 while a required dependency was down.

> **Observation (minor, not scored):** readiness checks *connectivity*, not *schema*. Before
> `alembic upgrade`, `/health/ready` returned `200` while every DB-backed route was broken. This is
> a defensible split (migrations run as a Job/initContainer), but a pod can report Ready with no
> schema.

### B2 — Database — PASS

All four legs pass.

```
1. migrate from empty:
   Running upgrade  -> 375b31581a92, initial example table
   Running upgrade 375b31581a92 -> bbd8c5d2bed5, append-only audit log
   Running upgrade bbd8c5d2bed5 -> 66fba9fbe2c9, stored file metadata

2. alembic downgrade base -> only alembic_version remains:
    Schema |      Name       | Type
    public | alembic_version | table
   then upgrade head -> alembic_version, audit_log, example, stored_file (4 rows)
   and the append-only triggers are recreated: audit_log_no_update, audit_log_no_delete

3. insert + select round-trip through the app's own session:
   INSERTED id = db7b5338-7fc8-436f-ab8b-5b5c27cc7cf5
   SELECTED    = db7b5338-7fc8-436f-ab8b-5b5c27cc7cf5 b2-roundtrip proof row 2026-08-24 11:37:00+00
   round-trip OK

4. pool reconnects after a DB restart — a real write immediately after `docker start`:
   {"audit_id":"aa9a38a7-e909-4eb6-b302-436ea8658136","action":"demo.audited",...}  HTTP 200
```

### B3 — Cache — DEFECT (major)

**Works:** MISS -> HIT, real TTL, expiry returns to MISS.

```
DELETE /demo/cached (invalidate)
call 1: {"key":"demo:cached:7","value":{...},"cache":"MISS","computed_by":"dev"}
call 2: {"key":"demo:cached:7","value":{...},"cache":"HIT","computed_by":"dev"}
call 3: ... "cache":"HIT"

redis-cli KEYS 'demo:cached*'  -> demo:cached:7
redis-cli TTL  'demo:cached:7' -> 48s
redis-cli GET  'demo:cached:7' -> {"seed": 7, "result": 49, "unit": "square"}

EXPIRE demo:cached:7 1 ; sleep 2 -> TTL = -2 (gone)
call 4: ... "cache":"MISS"     <- correctly recomputed
```

(`computed_by":"dev"` also shows the stub principal reaching route logic.)

**Does not degrade gracefully.** With Redis stopped, a cached route returns **500**:

```
docker stop cab-redis
GET /demo/cached ->
{"error":"internal_error","message":"An unexpected error occurred...","request_id":"c3ac9ff1-...","trace_id":"7b195a0b..."}
HTTP 500

server-side cause (cab-app log):
  redis.exceptions.TimeoutError: Timeout connecting to server
```

**Failure scenario:** Redis restarts for 30 seconds during a deploy. Every cache-backed endpoint
returns 500 for the whole window, even though the origin data in Postgres is fine and reachable.
A cache is an optimisation; its loss should cost latency, not availability.

The failure is at least *safe* — correct error schema, correlated ids, no stack trace leaked — but
it is not graceful, which is what B3 requires.

**Fix:** catch `RedisError`/`TimeoutError` inside `cache.get_or_set` (and `get_json`), log a
warning, and fall through to the origin function. Readiness already reports the outage, so traffic
will drain on its own.

### B4 — Logging + correlation — PASS (one minor)

Loki, filtered by the returned `X-Request-ID`, returns exactly that request's lines:

```
query: {service="app"} | json | request_id = "TEST-123"

log lines matching request_id=TEST-123 in Loki: 2
  [info ] app.audit.writer            audit.written       rid=TEST-123 tid=11bd2299eb16d21f
  [info ] app.middleware.correlation  request.completed   rid=TEST-123 tid=11bd2299eb16d21f
```

Both carry `request_id` **and** `trace_id`, and the `trace_id` matches the response header.

Every application log line is JSON:

```
cab-app log lines: 31   non-JSON: 0
```

**Minor defect — the worker is not:**

```
cab-worker log lines: 31   non-JSON: 17
  NON-JSON: -------------- celery@4666d40c5293 v5.6.3 (recovery)
  NON-JSON: - ** ---------- .> transport:   redis://redis:6379/0
  ... (Celery's ASCII startup banner)
```

I checked whether that banner could leak the broker password rather than assuming it could not:

```
celery.apps.worker.Worker.startup_info -> conninfo=self.app.connection().as_uri()
kombu Connection.as_uri() masks by default: redis://:**@redis:6379/0
```

**No secret leak.** The impact is that Promtail ships 17 unparseable lines per worker start.
**Fix:** add `--without-banner` to the worker command. **Severity: minor.**

**No secrets in logs:** the Trivy secret scanner (proven live in A3) reports zero findings across
the tree, and no credential literal appears in any captured log line.

### B5 — Metrics — PASS

Valid exposition, verified by Prometheus' own parser:

```
$ docker exec -i cab-prometheus promtool check metrics < metrics.txt
promtool exit=0
21 metric families exposed
```

Targets are up:

```
  common-app-base health=up  url=http://app:8000/metrics
  loki            health=up  url=http://loki:3100/metrics
  prometheus      health=up  url=http://localhost:9090/metrics
```

**Cardinality — the adversarial part.** Every label name exposed:

```
generation  handler  implementation  le  major  method  minor  patchlevel  status  version
```

`handler`, `method`, `status`, `le` are the request labels; the rest belong to `python_info` and
the GC collector and are static. **No id-like label values exist:**

```
grep for request_id=/trace_id=/user=/session=/uuid= in /metrics  -> no output
grep for UUID or 32-hex label values                             -> no output
```

And `handler` is the **route template**, not the raw path — three requests with three different
path parameters collapse to one series:

```
GET /demo/job/aaaaaaaa-1111-...   GET /demo/job/bbbbbbbb-5555-...   GET /demo/job/cccccccc-9999-...

app_http_requests_total{handler="/demo/job/{task_id}",method="GET",status="200"} 3.0
series count for that route: 1
```

**Error rate moves on errors:**

```
before:  app_http_requests_total{handler="/demo/audited",method="POST",status="200"} 2.0
after :  app_http_requests_total{handler="/demo/audited",method="POST",status="200"} 3.0
         app_http_requests_total{handler="/demo/boom",method="GET",status="500"}     1.0
```

### B6 — Tracing — PASS

The `trace_id` in the response header is the real trace, verified by decoding Tempo's base64 id:

```
Tempo traceId b64 -> hex: 11bd2299eb16d21f843fe5d6f59d343e
response X-Trace-ID     : 11bd2299eb16d21f843fe5d6f59d343e
MATCH: True
```

**DB child spans** (POST /demo/audited):

```
  POST /demo/audited            kind=SPAN_KIND_INTERNAL   <- correlation middleware (root)
  connect                       kind=SPAN_KIND_CLIENT
  INSERT                        kind=SPAN_KIND_CLIENT     <- sqlalchemy instrumentation
  POST /demo/audited http send  kind=SPAN_KIND_INTERNAL   (x2)
  POST /demo/audited            kind=SPAN_KIND_SERVER     <- fastapi instrumentation
  total spans: 6
```

**Redis child spans** (GET /demo/cached):

```
  scope: app.middleware.correlation            GET /demo/cached
  scope: opentelemetry.instrumentation.redis   GET       kind=SPAN_KIND_CLIENT
  scope: opentelemetry.instrumentation.redis   SET       kind=SPAN_KIND_CLIENT
  scope: opentelemetry.instrumentation.fastapi GET /demo/cached (+ http send x2)
```

The same `trace_id` appears on every log line for the request (B4), so trace and logs join.

### B7 — Audit — PASS on the letter, DEFECT (major) on the guarantee

**Exactly one row, ids matching the response, actor from the stub:**

```
POST /demo/audited  -H "X-Request-ID: AUDIT-PROOF-1787571432"
{"audit_id":"b10310e6-...","action":"demo.audited","request_id":"AUDIT-PROOF-1787571432","trace_id":"9ff5838f987ae913023212d4f040a7a6"}

 rows |    action    |       request_id       |             trace_id             | actor_id
    1 | demo.audited | AUDIT-PROOF-1787571432 | 9ff5838f987ae913023212d4f040a7a6 | dev
```

**UPDATE and DELETE are rejected by the database, not by convention:**

```
UPDATE audit_log SET action='tampered' WHERE ...
  ERROR:  audit_log is append-only: UPDATE is not permitted
  CONTEXT:  PL/pgSQL function audit_log_reject_mutation() line 3 at RAISE

DELETE FROM audit_log WHERE request_id = '...'
  ERROR:  audit_log is append-only: DELETE is not permitted

DELETE FROM audit_log;                       (bulk, no WHERE)
  ERROR:  audit_log is append-only: DELETE is not permitted
```

**No zero-tolerance violation** — the table accepts neither an UPDATE nor a DELETE.

**But the append-only guarantee has two holes.**

**(1) `TRUNCATE` erases the entire audit trail:**

```
TRUNCATE audit_log;
  TRUNCATE TABLE                              <- succeeded

SELECT action, request_id FROM audit_log WHERE request_id = 'AUDIT-PROOF-1787571432';
  (0 rows)                                    <- the whole table is gone
```

Cause — the migration creates only row-level triggers, and `TRUNCATE` is a statement-level event:

```
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON public.audit_log FOR EACH ROW ...
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON public.audit_log FOR EACH ROW ...
(no BEFORE TRUNCATE trigger)
```

**(2) The application's own role owns the table, so it can remove the guard.** Verified inside a
rolled-back transaction:

```
BEGIN;
DROP TRIGGER audit_log_no_update ON audit_log;   -> DROP TRIGGER
DROP TRIGGER audit_log_no_delete ON audit_log;   -> DROP TRIGGER
ROLLBACK;

SELECT current_user, tableowner FROM pg_tables WHERE tablename='audit_log';
 current_user | tableowner
 appuser      | appuser
```

**Failure scenario:** an SQL-injection flaw in any future feature, or one careless operator using
the app credentials, erases the compliance trail with a single statement — and the trail's purpose
is to survive exactly that.

**Fix:** add the missing statement-level trigger, and stop running the app as the table owner.

```sql
CREATE TRIGGER audit_log_no_truncate
  BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_mutation();
```

Defence in depth: create the table as a migration/owner role and grant the runtime role only
`INSERT, SELECT`.

### B8 — Errors — PASS (one minor)

**One schema for every failure mode**, including the ones that normally bypass application
handlers (unrouted paths and method-not-allowed):

```
GET  /demo/boom          500  {"error":"internal_error",   "message":"An unexpected error occurred. Quote the request_id when reporting this.","request_id":"B8-BOOM-001","trace_id":"d80559ff..."}
POST /files (no file)    422  {"error":"validation_error", "message":"The request body or parameters failed validation.","request_id":"B8-422-001","trace_id":"ee83f6ea...","detail":[{"type":"missing","loc":["body","file"],"msg":"Field required"}]}
GET  /demo/not-found     404  {"error":"not_found",        "message":"No such demo resource.","request_id":"B8-404-001","trace_id":"5a930017...","detail":{"looked_for":"nothing"}}
GET  /no/such/route      404  {"error":"not_found",        "message":"Not Found","request_id":"B8-404-002","trace_id":"c353e1df..."}
DELETE /health/live      405  {"error":"method_not_allowed","message":"Method Not Allowed","request_id":"B8-405-001","trace_id":"a6600376..."}
```

**No stack trace leaks.** Grepping the 500 body for `traceback|File "|line N|RuntimeError|/app/`
returns `0` matches — while the traceback *is* captured server-side:

```
  [info ] audit.written                rid=B8-BOOM-001  has_traceback=False
  [error] request.failed               rid=B8-BOOM-001  has_traceback=True
  [info ] request.completed            rid=B8-BOOM-001  has_traceback=False
  [error] request.unhandled_exception  rid=B8-BOOM-001  has_traceback=True
  [error] Exception in ASGI application rid=B8-BOOM-001 has_traceback=True
```

**Sentry — verified for real, not assumed.** I ran a local HTTP collector, pointed `SENTRY_DSN` at
it, and inspected the captured envelope:

```
boom HTTP 500 rid SENTRY-PROBE-001
envelopes captured: 3
  --- sentry event ---
   level     : error
   tags      : {"service": "common-app-base", "request_id": "SENTRY-PROBE-001", "trace_id": "a3db7c6d4a1dcfa80e5934711fa83755"}
   exception : ZeroDivisionError
error events reported: 1
```

Exactly **one** event (not three) tagged with both correlation ids — the `LoggingIntegration`
de-duplication holds. Without a DSN the integration no-ops cleanly (`sentry.disabled` at info).

**Minor defect:** one unhandled exception produces **three** error-level log records each carrying a
full traceback (`request.failed`, `request.unhandled_exception`, `Exception in ASGI application`).
Sentry was de-duplicated but logging was not. At scale this triples error-log volume and
error-rate alert noise. **Fix:** log the traceback once (keep `request.failed`) and let the other
two be suppressed or logged without `exc_info`.

### B9 — Storage — PASS

**Byte-identical round-trip**, with a server-computed checksum that matches an independent hash:

```
local  sha256: 7671b43c6a0607ce8d34da82491360c5fd3304743ad2006d2d721d69f8e87cdf  (100000 bytes)

POST /files ->
{"id":"5a1f6536-...","filename":"blob.bin","size_bytes":100000,
 "checksum_sha256":"7671b43c6a0607ce8d34da82491360c5fd3304743ad2006d2d721d69f8e87cdf",
 "uploaded_by":"dev","storage_key":"uploads/2026/08/24/5a1f6536-.../blob.bin"}

GET /files/{id}/content ->
downloaded sha256: 7671b43c...87cdf  (100000 bytes)
cmp -> BYTE-IDENTICAL: yes
```

**The database stores the key, never the blob.** There is no `bytea` column, and the entire row
occupies 240 bytes for a 100 KB object:

```
 storage_key     | character varying(1024)      filename    | character varying(512)
 content_type    | character varying(255)       size_bytes  | bigint
 checksum_sha256 | character varying(64)        uploaded_by | character varying(256)
 id / created_at / updated_at
(no bytea / no large-object column)

 row_bytes | storage_key                                                      | size_bytes
       240 | uploads/2026/08/24/5a1f6536-.../blob.bin                         |     100000
```

The object really is in MinIO:

```
[2026-08-24 11:42:45 UTC]  98KiB STANDARD app-files/uploads/2026/08/24/5a1f6536-.../blob.bin
```

**Presigned URL** resolves to the *public* endpoint (not the internal `minio:9000`) and serves
identical bytes — the separate public-endpoint client works:

```
GET /files/{id}/download-url -> HTTP 307
location: http://localhost:9000/app-files/uploads/.../blob.bin?X-Amz-Algorithm=AWS4-HMAC-SHA256&...
fetching that URL -> 100000 bytes, sha256 7671b43c...87cdf  -> identical
```

**Upload writes exactly one audit row:**

```
    action     |  request_id   | resource_type |             resource_id              | actor_id | outcome
 file.uploaded | B9-UPLOAD-001 | file          | 5a1f6536-722e-4830-a020-412ecf472910 | dev      | success
```

**Over-limit upload rejected** (limit 10 MiB, sent 11 MiB):

```
{"error":"payload_too_large","message":"Request body exceeds the 10485760 byte limit.","request_id":"B9-TOOBIG-01",...}
HTTP 413
```

(`azure_blob` contract check omitted — the adapter was removed at the owner's instruction; see A5.)

### B10 — Jobs — PASS (one minor)

**Enqueue returns immediately** — the task sleeps 2s, the call returned in 414 ms:

```
POST /demo/job -H "X-Request-ID: B10-JOB-001"
{"task_id":"97468d13-...","status":"queued","request_id":"B10-JOB-001"}
enqueue latency: 414 ms

polling:  t+1s: STARTED   t+2s: SUCCESS
final: {"status":"SUCCESS","result":{"a":2,"b":3,"sum":5,
        "request_id":"B10-JOB-001","trace_id":"fd315f96415555a92de89a12137293b9"},"error":null}
```

**Correlation survived the process hop** — the originating `request_id` is in the worker's result
*and* in five worker log lines, including Celery's own:

```
  [info ] app.jobs.celery_app  task.started    rid=B10-JOB-001 tid=fd315f96415555a9
  [info ] app.jobs.tasks       slow_add.begin  rid=B10-JOB-001 tid=fd315f96415555a9
  [info ] app.jobs.tasks       slow_add.done   rid=B10-JOB-001 tid=fd315f96415555a9
  [info ] celery.app.trace     Task demo.slow_add[97468d13-...  rid=B10-JOB-001 tid=fd315f96415555a9
  [info ] app.jobs.celery_app  task.finished   rid=B10-JOB-001 tid=fd315f96415555a9
```

**Failure surfaces with its ids:**

```
enqueued demo.always_fails -> t+0s: STARTED  t+1s: FAILURE
  traceback surfaced: True
  error: RuntimeError('This task always fails, by design.')

  [info ] app.jobs.celery_app task.started  rid=B10-FAIL-001
  [error] app.jobs.celery_app task.failed   rid=B10-FAIL-001
  [error] celery.app.trace    Task demo.always_fails[...]  rid=B10-FAIL-001
  [info ] app.jobs.celery_app task.finished rid=B10-FAIL-001
```

**Minor defect — "retries per policy" cannot be demonstrated, because there is no retry policy.**
The Celery config sets `task_acks_late=True` and `task_reject_on_worker_lost=True` (so a task is
redelivered if a worker dies — the important half) but no `autoretry_for`, `max_retries` or
`retry_backoff` anywhere. `retries recorded: None`. For a template teams copy, the retry idiom
should be demonstrated once. **Severity: minor** — "never retry by default" is a defensible choice
for non-idempotent work, it is simply undocumented and unshown.

### B11 — Security hardening — DEFECT (major)

**Headers are present on success *and* on error paths** (error responses are generated by
`ServerErrorMiddleware`, which sits outside the middleware chain, so this is a real test):

```
                              200 /health/live      500 /demo/boom
x-content-type-options        nosniff               nosniff
x-frame-options               DENY                  DENY
referrer-policy               strict-origin-...     strict-origin-...
permissions-policy            geolocation=(), ...   geolocation=(), ...
cross-origin-opener-policy    same-origin           same-origin
cross-origin-resource-policy  same-origin           same-origin
content-security-policy       default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'
```

**No `Server` header** (`server headers present: 0`) — uvicorn's banner is suppressed.

**HSTS is correctly TLS-conditional, and proven to emit:**

```
plain HTTP                       -> (no strict-transport-security)     <- correct, RFC 6797
-H "X-Forwarded-Proto: https"    -> strict-transport-security: max-age=31536000; includeSubDomains
```

**Oversized input rejected**, including the chunked case where `Content-Length` is absent:

```
honest 11 MiB upload          -> HTTP 413 payload_too_large
chunked multipart, 11 MiB     -> capped mid-stream:
    [warning] request.body_too_large  received=10596066  limit=10485760
```

Memory is bounded either way.
*Minor:* the chunked path surfaces as `400 "There was an error parsing the body"` rather than
`413`, because the cap works by injecting `http.disconnect` and the multipart parser fails first.
The server logs the truth; the client gets a misleading status.

**Image scan is clean:**

```
$ make scan
make scan exit = 0
common-app-base:local (debian 12.15)   Vulnerabilities: 0
```

...and the gate is proven real, not vacuous — see the A3 negative control (planted secret -> exit 1).

**The defect: CORS is wide open by default.**

```
app/config.py:80:  cors_allow_origins: str = "*"
.env.example:55:   CORS_ALLOW_ORIGINS=*

OPTIONS /demo/cached  -H "Origin: https://evil.example.com" -H "Access-Control-Request-Method: GET"
  HTTP/1.1 200 OK
  access-control-allow-origin: *          <- disallowed origin is allowed
  access-control-allow-methods: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT
```

B11 requires "CORS blocks a disallowed origin". With shipped defaults it does not.

**The mechanism itself is correct** — this is a bad default, not broken code:

```
CORS_ALLOW_ORIGINS=https://good.example.com
  origin=https://good.example.com  -> ACAO='https://good.example.com'
  origin=https://evil.example.com  -> ACAO=None                       <- blocked

CORS_ALLOW_ORIGINS=* + CORS_ALLOW_CREDENTIALS=true
  GUARD FIRED: ValueError: CORS_ALLOW_CREDENTIALS=true requires explicit CORS_ALLOW_ORIGINS
               (browsers reject credentialed requests against a wildcard origin).
```

Mitigated by `cors_allow_credentials=False` by default plus that guard, so cookies/credentials
cannot leak. But every POC cloned from this base ships browser-reachable from any origin.

**Fix:** default `cors_allow_origins` to `""` (deny) or `http://localhost:3000`, and make `*` an
explicit opt-in.

### B12 — Tests — PASS

```
$ make test
116 passed in 53.14s
TOTAL                            1087    143    87%
Required test coverage of 80% reached. Total coverage: 86.84%
make test exit = 0
```

Playwright e2e genuinely executes headless against the live stack — it is not silently skipped:

```
$ uv run pytest tests/e2e -m e2e -v
============================= 6 passed in 15.42s ==============================
```

Two runs were byte-identical — see "Ran twice" at the end of this report.

### B13 — K8s manifests — PASS

```
$ kubectl kustomize deploy/k8s
kustomize exit=0
rendered: 9 objects, 381 lines
      1 ConfigMap   2 Deployment   1 HorizontalPodAutoscaler   1 Namespace
      1 NetworkPolicy  1 PodDisruptionBudget  1 Secret  1 Service

$ kubeconform -strict -summary -ignore-missing-schemas rendered.yaml
Summary: 9 resources found in 1 file - Valid: 9, Invalid: 0, Errors: 0, Skipped: 0
kubeconform exit=0
```

`Skipped: 0` matters — every object was validated against a real schema, none silently passed.

**Probes point where they should:**

```
livenessProbe   path: /health/live    port: http   periodSeconds: 15  failureThreshold: 3
readinessProbe  path: /health/ready   port: http   periodSeconds: 10  failureThreshold: 3
startupProbe    path: /health/live    port: http   periodSeconds: 5   failureThreshold: 30
```

Startup gets 30 failures × 5s = 150s of grace, so slow boots are not mistaken for crashes.

> `kubectl apply --dry-run=client -k deploy/k8s` could not run: kubectl contacts the API server
> even for a client dry-run (`failed to download openapi ... dial tcp [::1]:8080`) and no cluster is
> available here. The brief permits an alternative; kubeconform is the stronger offline check and
> it passed.

---

## PART C — Correlation proof

One request with `X-Request-ID: TEST-123` (plus `TEST-123-BOOM` for the error path, since the
boom leg cannot share a response with the happy path).

```
POST /demo/audited  -H "X-Request-ID: TEST-123"
  HTTP/1.1 200 OK
  x-request-id: TEST-123
  x-trace-id:  11bd2299eb16d21f843fe5d6f59d343e

GET /demo/boom      -H "X-Request-ID: TEST-123-BOOM"
  HTTP/1.1 500 Internal Server Error
  x-request-id: TEST-123-BOOM
  x-trace-id:  174bf185976fe16d719fa0e648226ca8
```

### Mapping table for `TEST-123`

| Leg | Result | Evidence |
|---|---|---|
| **Logs (Loki)** | OK | 2 lines, `{service="app"} \| json \| request_id = "TEST-123"` — `audit.written`, `request.completed`, both carrying `trace_id=11bd2299…` |
| **Trace (Tempo)** | OK | trace `11bd2299eb16d21f843fe5d6f59d343e` — 6 spans; base64 id decodes to exactly the response header |
| **Audit (Postgres)** | OK | 1 row: `demo.audited \| TEST-123 \| 11bd2299… \| dev \| success` |
| **Metric (Prometheus)** | OK | `app_http_requests_total{handler="/demo/audited",method="POST",status="200"}` 2.0 → 3.0 |
| **Error (Sentry)** | n/a | not on the happy path |

### Mapping table for `TEST-123-BOOM`

| Leg | Result | Evidence |
|---|---|---|
| **Logs (Loki)** | OK | error records with `rid=TEST-123-BOOM`, traceback server-side only |
| **Trace (Tempo)** | OK | `174bf185976fe16d719fa0e648226ca8`, returned as `X-Trace-ID` |
| **Audit (Postgres)** | OK | 1 row: `demo.boom \| TEST-123-BOOM \| 174bf185… \| dev \| failure` |
| **Metric (Prometheus)** | OK | `app_http_requests_total{handler="/demo/boom",method="GET",status="500"}` → 1.0 |
| **Error (Sentry)** | OK | verified separately with a local collector: 1 event, `tags={"request_id":"SENTRY-PROBE-001","trace_id":"a3db7c6d…"}` |

Both audit rows, side by side:

```
    action    |  request_id   |             trace_id             | actor_id | outcome
 demo.audited | TEST-123      | 11bd2299eb16d21f843fe5d6f59d343e | dev      | success
 demo.boom    | TEST-123-BOOM | 174bf185976fe16d719fa0e648226ca8 | dev      | failure
```

**No missing link. No zero-tolerance failure on correlation.**

`make smoke` independently reproduces this and exits 0 — see E2.

---

## PART D — EXTENSIBILITY TEST

This is the part that decides whether the base delivers on "just plug in core logic".

| # | Check | Result |
|---|---|---|
| D0 | Extension mechanism is documented | **PASS** (incomplete — see E3) |
| D1 | Probe feature built the documented way | **PASS** |
| D2 | Zero base edits | **FAIL — zero-tolerance** (2 protected modules edited) |
| D3 | Automatic inheritance | **PASS** (one defect: audit actor) |
| D4 | Effort proxy | **PASS** — 0 plumbing LOC, 5 registration lines |
| D5 | Clean removal | **PASS** |

### D0 — The documented extension mechanism

From `README.md` ("Where your code goes"):

```
app/
  api/            <- add your routers here; delete demo.py
  db/models/      <- add your models here; delete example.py
  jobs/tasks.py   <- add your Celery tasks
  services/       <- (create this) your business logic

Register a router in app/main.py and a model in app/db/models/__init__.py,
then make revision m="..." and make migrate.
```

Plus two conventions: take `user: CurrentUser` on any route needing identity, and raise `AppError`
subclasses for expected failures. **The mechanism is documented** — D0 passes. Its *completeness* is
scored under E3.

### D1 — The probe feature — PASS

`POST /widgets` — validates input, writes Postgres, caches in Redis, enqueues Celery, writes one
audit entry, and has a domain-error path. Built with only the documented mechanism.

Files created (feature-owned):

```
app/api/widgets.py                                    108 lines (81 code)
app/db/models/widget.py                                18 lines (11 code)
app/services/widget_jobs.py                            29 lines (22 code)
migrations/versions/20260824_1724_add_widget_table.py  42 lines (29 code, AUTOGENERATED)
```

Nothing was reimplemented — session, cache, jobs, audit, errors and the identity stub all arrive as
dependencies (`session: DbSession`, `user: CurrentUser`, `cache.set_json`, `write_audit`,
`raise ConflictError`).

The migration was fully autogenerated, including index names and a working downgrade:

```
$ make revision m="add widget table"
INFO  [alembic.autogenerate.compare.tables] Detected added table 'widget'
INFO  [alembic.autogenerate.compare.constraints] Detected added index 'ix_widget_name' on '('name',)'
INFO  [alembic.autogenerate.compare.constraints] Detected added index 'ix_widget_owner' on '('owner',)'
```

All paths work:

```
POST /widgets {"name":"gear-beta","teeth":36}
  201 {"id":"24cb8002-...","name":"gear-beta","teeth":36,"owner":"dev","ratio":12.0,"task_id":"029483e6-..."}

GET  /widgets/{id}          200 {... "ratio":12.0}                       (served from cache)
POST /widgets/{id}/strip    409 {"error":"conflict","message":"Widget 'gear-beta' is in service and cannot be stripped.","request_id":"WIDGET-PROBE-004","trace_id":"1c5e31e2..."}
POST /widgets {"teeth":1}   422 {"error":"validation_error",...,"detail":[{"type":"greater_than_equal","loc":["body","teeth"],"msg":"Input should be greater than or equal to 3","input":1,"ctx":{"ge":3}}]}
```

The domain error came back in the base's schema with correlation ids, without the feature writing
a single line of error handling.

### D2 — Zero base edits — FAIL (zero-tolerance)

```
$ git diff --stat
 app/db/models/__init__.py | 3 ++-
 app/jobs/celery_app.py    | 2 +-
 app/main.py               | 3 ++-
 3 files changed, 5 insertions(+), 3 deletions(-)

$ git diff -U0
@@ app/main.py
-from app.api import demo, docs, files, health
+from app.api import demo, docs, files, health, widgets
+    app.include_router(widgets.router)

@@ app/db/models/__init__.py                 <- PROTECTED (db/*)
+from app.db.models.widget import Widget
-__all__ = ["AuditLog", "Base", "Example", "StoredFile"]
+__all__ = ["AuditLog", "Base", "Example", "StoredFile", "Widget"]

@@ app/jobs/celery_app.py                    <- PROTECTED (jobs/*)
-    include=["app.jobs.tasks"],
+    include=["app.jobs.tasks", "app.services.widget_jobs"],
```

`app/main.py` is the designated registration place and is fine (2 lines rather than the 1 the brief
allows, but an import plus an include *is* one registration).

**The other two are edits to modules D2 lists as protected**, and both were forced. Neither is
cosmetic — each has a failure mode I reproduced.

#### Blocker 1 — a feature cannot own its Celery task (`app/jobs/celery_app.py`)

The Celery app pins its task modules to a hardcoded list with no autodiscovery:

```
app/jobs/celery_app.py:49:    include=["app.jobs.tasks"],
```

I first built the feature with its task in `app/services/widget_jobs.py` — where the README's own
tree says business logic belongs — and left base files alone. Result:

```
worker [tasks] registry:
  . demo.always_fails
  . demo.process_upload
  . demo.slow_add
                                 <- widgets.reindex absent

[error] celery.worker.consumer.consumer  Received unregistered task of type 'widgets.reindex'.
```

**And the API had already returned `201 Created` with a `task_id`.** The caller is told the job was
queued. Only if it later polls does it learn:

```
state : FAILURE
result: Task of kind 'widgets.reindex' never registered, please make sure it's imported.
```

There is **no config-only escape** — `include` is not a `Settings` field, so the fix must edit
`app/jobs/celery_app.py` or dump feature tasks into `app/jobs/tasks.py`. Both are protected.
After adding the one line, everything worked:

```
[tasks]
  . demo.always_fails    . demo.process_upload    . demo.slow_add    . widgets.reindex
```

**Fix:** replace the fixed list with autodiscovery, e.g.
`celery_app.autodiscover_tasks(["app.services", "app.jobs"], related_name=None)`, or expose it as a
`Settings` field so a feature can extend it without touching base code.

#### Blocker 2 — the model registry is a data-loss trap (`app/db/models/__init__.py`)

`migrations/env.py` builds `target_metadata` from `app.db.models`, so a model is invisible to
Alembic unless it is imported in that `__init__.py`. **The application works fine without it** —
which is what makes this dangerous. Reverting only that registration:

```
app builds OK without model registration
widget in Base.metadata after direct import: True     <- app is fine
```

But the next autogenerate — possibly run by a different developer for an unrelated change —
silently proposes to **destroy the table**:

```
$ make revision m="probe unregistered"
INFO  [alembic.autogenerate.compare.constraints] Detected removed index 'ix_widget_name' on 'widget'
INFO  [alembic.autogenerate.compare.constraints] Detected removed index 'ix_widget_owner' on 'widget'
INFO  [alembic.autogenerate.compare.tables]      Detected removed table 'widget'
```

`make migrate` would then execute `op.drop_table('widget')` against production.

Restoring the registration makes autogenerate clean again, confirming the diagnosis:

```
$ make revision m="verify clean"
def upgrade() -> None:
    pass          <- empty, as it should be
```

**Failure scenario:** a developer adds a model, forgets one import line, ships successfully (the app
works, tests pass, the table exists because they hand-wrote or generated the migration while it
*was* registered). Weeks later someone runs `make revision` for a different change and the
generated migration quietly contains a `drop_table`. Review misses it. Data is gone.

**Fix:** discover models automatically instead of by hand — walk `app/**/models` with
`pkgutil.walk_packages` in `migrations/env.py`, or have `env.py` import the assembled application so
anything reachable is registered. Failing that, make the omission loud rather than silent.

**Verdict on D2:** by the letter of the brief this is a **zero-tolerance failure** — two protected
base-infrastructure modules had to be edited. I record it as such. Both are small fixes, and after
them the base would genuinely meet the one-line-registration bar.

### D3 — Automatic inheritance — PASS (with one defect)

Without writing any plumbing, the new endpoint already had:

**Security + correlation headers:**

```
HTTP/1.1 201 Created
x-content-type-options: nosniff
x-frame-options: DENY
content-security-policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'
x-request-id: WIDGET-PROBE-001
x-trace-id: c7572c88f42f7557481ba164e7363a83
```

**Structured logs in Loki, keyed by request_id:**

```
Loki lines for WIDGET-PROBE-002: 2
  [info ] app.audit.writer            audit.written
  [info ] app.middleware.correlation  request.completed
```

**A trace with DB, Redis and job-enqueue spans:**

```
  opentelemetry.instrumentation.fastapi     POST /widgets  (+ http receive / http send x2)
  opentelemetry.instrumentation.sqlalchemy  connect, SELECT, INSERT, connect, SELECT, connect, INSERT
  opentelemetry.instrumentation.redis       SET
  opentelemetry.instrumentation.redis       LPUSH          <- Celery enqueue onto the broker
  app.middleware.correlation                POST /widgets  <- root span
```

**Prometheus metrics for the new routes, correctly templated:**

```
app_http_requests_total{handler="/widgets",method="POST",status="201"}                 1.0
app_http_requests_total{handler="/widgets/{widget_id}",method="GET",status="200"}       1.0
app_http_requests_total{handler="/widgets/{widget_id}/strip",method="POST",status="409"} 1.0
app_http_requests_total{handler="/widgets",method="POST",status="422"}                  1.0
```

**The worker ran the feature's task under the originating request_id:**

```
  [info ] app.jobs.celery_app       task.started          rid=WIDGET-PROBE-002
  [info ] app.services.widget_jobs  widget.reindex.begin  rid=WIDGET-PROBE-002
  [info ] app.services.widget_jobs  widget.reindex.done   rid=WIDGET-PROBE-002
  [info ] celery.app.trace          Task widgets.reindex[029483e6-...  rid=WIDGET-PROBE-002
  [info ] app.jobs.celery_app       task.finished         rid=WIDGET-PROBE-002
```

**Health unchanged and still honest:** `live=200 ready=200`, all three checks `ok`.

**The base's own quality gates accepted the new code with no configuration change:**

```
$ make lint       All checks passed!  /  55 files already formatted
$ make typecheck  Success: no issues found in 36 source files   (33 -> 36)
$ make test       116 passed, Total coverage: 85.33%  (gate 80% still met)
```

#### Defect (major) — the audit actor is silently wrong

The one thing the feature did **not** inherit correctly:

```
widget.created | WIDGET-PROBE-002 | widget | anonymous | success | {"name": "gear-beta", "teeth": 36}
                                             ^^^^^^^^^
```

Every base route records `actor_id = dev`; the new feature recorded `anonymous`. Cause:

```
app/audit/writer.py:  actor: Principal | None = None
                      actor_id=actor.id if actor else "anonymous"

app/api/demo.py:86    actor=user,        <- base routes remember
app/api/files.py:109  actor=user,        <- base routes remember
```

This contradicts the README's own claim:

> "**Audit** | `write_audit()` reads the ids from context — no call site can forget them"

The *correlation ids* are read from context and genuinely cannot be forgotten. **The actor cannot
be read from context and is silently defaulted.** A new feature that follows the documented pattern
produces an audit trail attributed to nobody — and an audit row with a confidently wrong actor is
worse than a missing row, because it will be believed.

**Fix:** bind the `Principal` into a contextvar where `request_id` is bound (or inside
`get_current_user`) and have `write_audit` fall back to it, keeping the explicit `actor=` argument
as an override. That makes the actor as unforgettable as the ids, which is what the README already
promises.

### D4 — Effort proxy — PASS

| Category | LOC |
|---|---|
| **Business logic written** (router + model + task, code lines only) | **114** |
| **Migration** (autogenerated, hand-written: 0) | 29 |
| **Plumbing / infrastructure written** | **0** |
| **Registration lines in base files** | **5** |
| **Infrastructure inherited for free** | **~2,147** |

Inherited without writing any of it:

```
app/config.py 215   app/logging.py 141   app/observability.py 175   app/errors.py 243
app/db/ 205   app/cache/ 114   app/storage/ 254   app/audit/ 134
app/jobs/ 220   app/middleware/ 393   app/security/ 53
```

A feature carrying one business rule (`teeth / 3.0`) inherited config, structured logging,
tracing, metrics, error handling, DB session management, caching, object storage, background jobs,
auditing and security headers for **five lines of registration and zero lines of plumbing**.

**This is the core value proposition, and it holds.** The D2 blockers are about *which* five lines
and how badly they fail when forgotten — not about the amount of work.

### D5 — Clean removal — PASS

```
$ uv run alembic downgrade -1
INFO  [alembic.runtime.migration] Running downgrade d2a6742d6fe8 -> 66fba9fbe2c9, add widget table

$ rm app/api/widgets.py app/db/models/widget.py migrations/versions/20260824_1724_add_widget_table.py
$ rm -rf app/services
$ git checkout -- app/main.py app/db/models/__init__.py app/jobs/celery_app.py

$ git diff --stat
(empty)

$ git status --porcelain
?? TEST_REPORT.md            <- this report only
```

After a rebuild, the feature is gone with no residue:

```
POST /widgets -> 404
worker [tasks]:  demo.always_fails / demo.process_upload / demo.slow_add    (widgets.reindex gone)
```

And the base is green again:

```
lint      exit=0
typecheck exit=0   Success: no issues found in 33 source files
test      exit=0   116 passed   Total coverage: 86.84%
smoke     exit=0   SMOKE PASSED - one request_id joins logs, traces, audit and metrics.
scan      exit=0
```

Features are genuinely pluggable and isolated.

---

## PART E — Developer-experience contract

| # | Check | Result |
|---|---|---|
| E1 | Cold start | **PASS** (one flake, see below) |
| E2 | `make smoke` exits 0 | **PASS** |
| E3 | Onboarding doc | **DEFECT (major)** — documented path yields a broken background job |

### E1 — Cold start — PASS

From `git clean -xfd` (venv, caches and all volumes destroyed), with only `.env.example` copied:

```
$ cp .env.example .env
$ make install
uv python install 3.12 ; uv sync --all-groups   -> 109 packages
$ make up
real  0m54.687s

$ docker compose ps
NAME             STATUS
cab-app          Up 48 seconds (healthy)
cab-grafana      Up 33 seconds (healthy)
cab-loki         Up 54 seconds (healthy)
cab-minio        Up 54 seconds (healthy)
cab-postgres     Up 54 seconds (healthy)
cab-prometheus   Up 54 seconds (healthy)
cab-promtail     Up 33 seconds
cab-redis        Up 54 seconds (healthy)
cab-worker       Up 48 seconds (healthy)

service count: 9
```

All nine services the brief names came up healthy in under a minute (promtail has no healthcheck
defined, hence no `(healthy)` suffix). Migrations then applied cleanly from empty.

> **Defect (minor, environment-triggered but a real DX trap).** The first `uv sync` died on a
> Windows file lock:
> ```
> error: Failed to install: pytest-9.1.1-py3-none-any.whl
>   Caused by: failed to persist temporary file: Access is denied. (os error 5)
> ```
> Re-running `make install` then reported **success** — `Resolved 110 packages / Checked 109
> packages` — but left `.venv` without a `pyvenv.cfg`. From then on `uv run` silently fell back to
> the *uv-managed* interpreter instead of the project venv, and `make migrate` failed with a
> misleading error:
> ```
> ModuleNotFoundError: No module named 'alembic'
> (yet .venv/Lib/site-packages/alembic exists)
> sys.executable -> ...\uv\python\cpython-3.12-...\python.exe      <- not .venv
> ```
> `rm -rf .venv && make install` fixed it permanently. The trigger is antivirus/file locking, but
> the defect is that **`make install` is not idempotent or self-healing**: a partial failure leaves a
> corrupt venv that every subsequent run declares healthy.
> **Fix:** have `install` verify the venv (`test -f .venv/pyvenv.cfg`) and recreate it if absent, or
> end with an import smoke check (`uv run python -c "import alembic, pytest, fastapi"`).

(Two orphaned `pytest.exe` processes from an earlier session also held the venv open; unrelated to
the codebase.)

### E2 — `make smoke` — PASS

```
$ make smoke
make smoke exit = 0
real  0m23.858s

  operation                  request_id                             log     trace   audit   metric  error   worker
  -----------------------------------------------------------------------------------------------------------------
  upload a file              b3b0db6e-840a-45f9-9cd1-b93a9d97770d   OK      OK      OK      OK      --      --
      log    : 3 line(s) in Loki
      trace  : 9 span(s) in Tempo
      audit  : 1 row(s): file.uploaded
      metric : /files: 8 -> 9
  trigger a background job   fd94f279-f733-4012-9e19-24d70a70d0fa   OK      OK      --      OK      --      OK
      log    : 1 line(s) in Loki
      trace  : 5 span(s) in Tempo
      audit  : n/a - this operation writes no audit row
      metric : /demo/job: 3 -> 4
      worker : result.request_id=fd94f279-f733-4012-9e19-24d70a70d0fa, 5 worker log line(s)
  trigger /demo/boom         ac0702bf-5946-4db0-b400-ae305d919cdf   OK      OK      OK      OK      OK      --
      log    : 5 line(s) in Loki
      trace  : 6 span(s) in Tempo
      audit  : 1 row(s): demo.boom
      metric : /demo/boom: 5 -> 6
      error  : HTTP 500 error=internal_error

  SMOKE PASSED - one request_id joins logs, traces, audit and metrics.
```

Each leg is queried from the store itself (Loki, Tempo, Postgres, Prometheus), not from
application memory, so it is independent evidence.

### E3 — Onboarding doc — DEFECT (major)

The README **does** have an "add a feature" section (`## Where your code goes`) and the Part D probe
followed it. But following it verbatim produced a **silently broken feature**, so the doc is
incomplete in two ways that map exactly onto the D2 blockers:

1. **Celery tasks.** The README's tree presents `app/services/` as the home for business logic and
   `app/jobs/tasks.py` for tasks, but never says that a task module must appear in the Celery
   `include` list. A task placed in `app/services/` is accepted by the API (`201` + `task_id`) and
   then rejected by the worker as unregistered. The documented path yields a job that never runs.

2. **Model registration.** The README says to register a model in `app/db/models/__init__.py`, but
   does not warn what happens if you don't — the app keeps working and a later autogenerate emits
   `drop_table`. A footgun this sharp needs a warning, not just an instruction.

Everything else in the README checked out. In particular its configuration claim is accurate — all
**44** `Settings` fields appear in `.env.example` (39 live, 5 commented), including
`MAX_REQUEST_BODY_BYTES` and `CORS_ALLOW_ORIGINS`:

```
Settings fields            : 44
documented in .env.example : 44
MISSING (0):
```

**Fix:** document task registration (or remove the need for it), and add a warning next to the
model-registration instruction.

---

## Ran twice (Rule 5)

Every gate was executed twice, on the same clean-clone environment, with the Part D feature added
and then removed in between. Results were identical.

| Command | Run 1 | Run 2 |
|---|---|---|
| `make lint` | exit 0 | exit 0 |
| `make typecheck` | exit 0, 33 files | exit 0, 33 files |
| `make test` | exit 0, **116 passed**, **86.84%** | exit 0, **116 passed**, **86.84%** |
| `make smoke` | exit 0, SMOKE PASSED | exit 0, SMOKE PASSED |
| `make scan` | exit 0 | exit 0 |
| `make build` | exit 0 | exit 0 (rebuilt 3×) |

Intermediate run with the probe feature present: `116 passed`, coverage `85.33%` (still above the
80% gate), lint clean, mypy clean on 36 files. **No flaky check was observed.** The only
non-determinism seen anywhere was the `uv sync` file-lock in E1, which is antivirus behaviour, not
test flakiness — and it is recorded as a defect because the tooling hides it rather than failing.

---

## DEFECT LIST (prioritized)

### Blockers — 2

| # | Defect | Where | Fix |
|---|---|---|---|
| **1** | **A feature cannot own its Celery task.** `include=["app.jobs.tasks"]` is hardcoded with no autodiscovery, so a task in `app/services/` is never registered. The API still returns `201` with a `task_id`; the worker logs `Received unregistered task`. Adding a feature therefore requires editing protected `app/jobs/celery_app.py`. | `app/jobs/celery_app.py:49` | `celery_app.autodiscover_tasks(["app.services", "app.jobs"])`, or make the include list a `Settings` field |
| **2** | **Model registry is a silent data-loss trap.** A model not imported in `app/db/models/__init__.py` still works at runtime, but the next `make revision` autogenerates `op.drop_table(...)` for its table. Adding a feature requires editing protected `app/db/models/__init__.py`. | `app/db/models/__init__.py`, `migrations/env.py:19` | Auto-discover models (`pkgutil.walk_packages` over `app/**/models`) in `migrations/env.py` |

Together these are the **D2 zero-tolerance failure**.

### Major — 5

| # | Defect | Where | Fix |
|---|---|---|---|
| **3** | **Audit actor silently defaults to `anonymous`.** Correlation ids come from contextvars and cannot be forgotten; the actor must be passed explicitly. A new feature following the docs writes audit rows attributed to nobody — contradicting the README's "no call site can forget" claim. | `app/audit/writer.py`, all `write_audit` call sites | Bind `Principal` to a contextvar; have `write_audit` fall back to it |
| **4** | **`TRUNCATE` bypasses append-only audit.** Row-level UPDATE/DELETE triggers exist; no statement-level TRUNCATE trigger. The app role also owns the table and can `DROP TRIGGER`. | `migrations/versions/*_append_only_audit_log.py` | Add `BEFORE TRUNCATE ... FOR EACH STATEMENT` trigger; run the app as a non-owner role with `INSERT, SELECT` only |
| **5** | **Cache outage becomes a full outage.** With Redis down, `/demo/cached` returns 500 (`redis.exceptions.TimeoutError` propagates). A cache should degrade to latency, not unavailability. | `app/cache/client.py` (`get_or_set`, `get_json`) | Catch `RedisError`/`TimeoutError`, log a warning, fall through to the origin |
| **6** | **CORS is wide open by default** (`cors_allow_origins = "*"`), so a disallowed origin is not blocked with shipped settings. The mechanism is correct and the wildcard+credentials guard works; the default is wrong for a template every POC clones. | `app/config.py:80`, `.env.example:55` | Default to deny (`""`) or `http://localhost:3000`; make `*` explicit opt-in |
| **7** | **Dev credentials are field defaults with no prod guard.** `ENVIRONMENT=prod` boots happily with `postgres_password="apppassword"` / `s3_secret_key="minioadmin"`. | `app/config.py:44,61,62` | `model_validator` rejecting dev defaults when `environment != "local"` |

### Minor — 5

| # | Defect | Where | Fix |
|---|---|---|---|
| **8** | **`make install` is not self-healing.** A partially-failed `uv sync` leaves `.venv` without `pyvenv.cfg`; later runs report success while `uv run` silently uses the wrong interpreter, surfacing as a confusing `ModuleNotFoundError`. | `Makefile:19-20` | Verify/recreate the venv, or end with an import smoke check |
| **9** | **One exception produces three error log records**, each with a full traceback (`request.failed`, `request.unhandled_exception`, `Exception in ASGI application`). Sentry was de-duplicated; logging was not. | `app/errors.py`, `app/middleware/correlation.py` | Log the traceback once; drop `exc_info` from the others |
| **10** | **Worker emits 17 non-JSON lines** (Celery ASCII banner), breaking the all-JSON invariant for log shippers. Verified **not** to leak the broker password (`Connection.as_uri()` masks it). | worker command | Add `--without-banner` |
| **11** | **Chunked over-limit bodies return `400 "error parsing the body"` instead of `413`.** The stream cap does fire (`received=10596066 limit=10485760`), so memory is bounded — but the client gets a misleading status. | `app/middleware/security.py` `RequestSizeLimitMiddleware` | Record the rejection and emit a 413 rather than relying on `http.disconnect` |
| **12** | **No retry policy is configured or demonstrated.** `task_acks_late` / `task_reject_on_worker_lost` are set (redelivery on worker loss), but no `autoretry_for` / `max_retries` anywhere, so "retries per policy" cannot be shown. | `app/jobs/celery_app.py`, `app/jobs/tasks.py` | Demonstrate the retry idiom on one example task and document the default |

### Observations (not scored)

- `/health/ready` returns 200 before migrations have run — connectivity is checked, schema is not.
  Defensible (migrations as a Job/initContainer), but a pod can be Ready with no schema.
- Pre-migration, `POST /demo/audited` returned `200` with `"audit_id": null` — `write_audit` never
  raises by design, so a failed audit write is invisible to the caller. Correct for availability,
  worth knowing for compliance.
- `azure_blob` (A5/B9) is absent because the repository owner explicitly asked for it to be removed.
  Recorded as a spec deviation, not a defect.

---

## FINAL VERDICT

# NOT READY

**One zero-tolerance failure occurred:** **D2** — adding the probe feature required editing two
modules on the brief's protected base-infrastructure list (`app/db/models/__init__.py` under `db/*`
and `app/jobs/celery_app.py` under `jobs/*`). Per the brief's scoring rule, that alone forces
NOT READY regardless of everything else.

The other four zero-tolerance conditions were **not** triggered:

- No secret or credential is hardcoded in source (dev defaults are flagged as major, not as secrets).
- A `request_id` **can** be followed across logs, trace, audit, metrics and error — proven end to
  end for `TEST-123` and again by `make smoke`.
- `/health/ready` **never** returned 200 while a required dependency was down (503 for both Postgres
  and Redis outages, naming the failure).
- The audit table accepted neither an UPDATE nor a DELETE, and Trivy reports no unignored
  HIGH/CRITICAL — with the gate proven real by a planted-secret negative control.

Every check was executed **twice** with identical results.

### The honest summary

This is a **strong base with two sharp edges**, not a shaky one. The D4 numbers are the headline:
a feature carrying a single business rule inherited ~2,147 lines of infrastructure — correlation,
structured logging, tracing, metrics, error shape, security headers, audit, DB sessions, caching,
object storage, background jobs — for **five registration lines and zero lines of plumbing**, and
passed the base's own lint, mypy and coverage gates unmodified. The correlation contract, which is
the spine of the whole template, held under every probe I could design, including adversarial ones
(cardinality explosion, forged request ids, a fake Sentry collector, byte-level storage fidelity).

What blocks it is narrow and fixable. Both blockers are the *same shape*: a hand-maintained
registry that fails **silently** when a developer forgets it — the Celery task list returns a
`201` for a job that will never run, and the model registry lets Alembic propose dropping a live
table. For a template whose entire purpose is that teams write only business logic, a footgun that
triggers when you follow the documented path is the most expensive kind of defect. Both are small
changes (autodiscovery in two places, roughly 20 lines total), plus a README warning.

Fix defects 1–3 and re-run Part D; on this evidence the base would pass.
