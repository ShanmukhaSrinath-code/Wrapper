# TEST_REPORT.md — Remediation & Re-Audit

**Subject:** Common Application Base (`common-app-base`)
**Protocol:** `SOLIDIFY_BRIEF.md` — remediating the findings of the prior audit
**Date:** 2026-08-24
**Baseline:** `22319b3` — verdict **NOT READY** (1 zero-tolerance failure, 2 blockers, 5 major, 5 minor).
The original audit is preserved verbatim in [TEST_REPORT_BASELINE.md](TEST_REPORT_BASELINE.md).

**Update, 2026-08-25 (`HARDEN_BRIEF.md`):** the five defects left out of scope by
`SOLIDIFY_BRIEF.md` are now closed as well, and the base is prod-ready rather
than merely structurally sound. See [Hardening pass](#hardening-pass-2026-08-25)
below; the defect table at the end is updated in place.

---

# VERDICT: READY

Every defect in scope is fixed **dynamically** — no hand-maintained list survives
anywhere in the base. The zero-tolerance failure is cleared: adding a feature now
requires **zero** edits to `app/core/**`, to `app/main.py`, or to any other base
module.

| | Baseline | Now |
|---|---|---|
| Base edits to add a feature | **3 files, 5 lines** (2 in protected modules) | **0 files, 0 lines** |
| Hand-maintained registries | 3 (routers, tasks, models) | **0** |
| Tests | 116 | **184** (158 after the solidify pass) |
| Coverage | 86.84% | **88.91%** |
| Worker log lines that are not JSON | 17 of 31 | **0 of 6** |
| Suite passes twice, identically | yes | yes |

Nothing that previously passed was weakened. The correlation contract, readiness
honesty, audit immutability, the Trivy gate and the K8s manifests were all
re-verified after the changes.

---

# PART 1 — THE SIX FIXES

Each fix ships with a regression test that reproduced the bug **before** the fix.

## Fix 1 — Celery task autodiscovery *(was Blocker 1)*

**The bug:** `celery_app` carried `include=["app.jobs.tasks"]`. A task defined
anywhere else was never registered with the worker — yet the API returned `201`
and a `task_id` for it. The job silently never ran.

### Red

```
$ uv run pytest tests/unit/test_task_discovery.py -q
FAILED ...::test_task_in_services_package_is_discovered
FAILED ...::test_discovered_task_is_registered_with_celery
FAILED ...::test_enqueue_rejects_an_unregistered_task_name
FAILED ...::test_enqueue_accepts_a_registered_task_name
E   ImportError: cannot import name 'UnknownTaskError' from 'app.jobs'
E   ModuleNotFoundError: No module named 'app.core'
```

### Green

```
$ uv run pytest tests/unit/test_task_discovery.py -q
.....                                                                    [100%]
```

### What changed

`app/core/discovery.py` walks `PLUGIN_PACKAGES` (default `app.services`). **The
API process and the worker run the same discovery pass**, so their registries
cannot diverge — which is exactly what made the old hardcoded list dangerous.

Two further guards:

- **`enqueue()` verifies before publishing.** No task id is handed back for work
  that cannot run.
- **Discovery is fatal on import errors.** A worker that boots with half its
  tasks missing looks healthy and drops jobs.

### Proven in the real worker

A task file dropped into `app/services/`, with no list edited anywhere:

```
[tasks]
  . demo.always_fails
  . demo.process_upload
  . demo.slow_add
  . probe.discovered          <- discovered, not listed
```

The unknown-name guard, and the loud-failure behaviour:

```
UnknownTaskError: Task 'nobody.registered.this' is not registered, so it would
never run. Registered tasks: [...]. If this is a new feature, make sure its
module is importable under one of PLUGIN_PACKAGES.

PluginImportError: Plugin module 'app.services._broken_probe' failed to import
during discovery.
```

### Follow-up caught by the full suite (`fix-1b`)

The API loads tasks in its lifespan hook, but **httpx's `ASGITransport` does not
run lifespan** — so in-process tests had an empty registry and `enqueue`
correctly but unhelpfully rejected `demo.slow_add`. Two integration tests went
`202 → 500`.

Patching the fixture would have hidden the real problem: scripts, embedded uses
and one-off shells do not run a lifespan either, and all of them can enqueue.
`enqueue()` now calls `ensure_tasks_loaded()`, which performs the walk once per
process, whoever asks first. The eager lifespan load stays, so a broken plugin
still fails at *startup* rather than at first use.

---

## Fix 2 — Model autodiscovery + destructive-op guard *(was Blocker 2)*

**The bug:** `migrations/env.py` built `target_metadata` from a hand-maintained
`app/db/models/__init__.py`. A model missing from it still worked at runtime — so
nothing warned anyone — but the next autogenerate compared incomplete metadata
against the live database and proposed `op.drop_table()` for its table.

### Red

```
$ uv run pytest tests/unit/test_model_discovery.py -q
FAILED ...::test_autogenerate_guard_blocks_a_drop_table
FAILED ...::test_autogenerate_guard_blocks_a_drop_column
FAILED ...::test_autogenerate_guard_allows_drops_when_explicitly_enabled
FAILED ...::test_autogenerate_guard_ignores_additive_changes
E   ModuleNotFoundError: No module named 'app.db.migration_guard'
```

### Green

```
$ uv run pytest tests/unit/test_model_discovery.py -q
......                                                                   [100%]
```

### Two independent defences

**1. Discovery.** `migrations/env.py` calls `import_discovered_models()` before
metadata is read. The registry in `app/db/models/__init__.py` is **gone entirely**
rather than merely discouraged — the file is now documentation.

**2. A guard.** `app/core/db/migration_guard.py` rejects
`drop_table`/`drop_column`/`drop_constraint` from autogenerate unless
`ALLOW_DESTRUCTIVE=1`. Hand-written migrations are untouched: deliberate
destruction stays easy, accidental destruction stops being silent.

### Proven end to end

A model in a feature package, **no `__init__.py` edit**:

```
$ make revision m="probe feature table"
INFO  [alembic.autogenerate.compare.tables] Detected added table 'probe_thing'
INFO  [alembic.autogenerate.compare.constraints] Detected added index 'ix_probe_thing_...'
```

Then the audit's exact scenario — delete the model and autogenerate:

```
$ make revision m="should be blocked"
INFO  [alembic.autogenerate.compare.tables] Detected removed table 'probe_thing'
[error] migration.destructive_blocked  operations=["drop_table('probe_thing')"]

app.core.db.migration_guard.DestructiveMigrationError:
Autogenerate produced destructive operations and was stopped:
  - drop_table('probe_thing')
The usual cause is a model that was not imported, which makes its table look
deleted. Check that the model lives under a discovered plugin package before
assuming the drop is correct.
If the removal really is intended, re-run with ALLOW_DESTRUCTIVE=1.

make: *** [Makefile:64: revision] Error 1
```

The escape hatch works, with a warning:

```
$ ALLOW_DESTRUCTIVE=1 make revision m="remove probe table"
[warning] migration.destructive_allowed operations=["drop_table('probe_thing')"] reason='ALLOW_DESTRUCTIVE=1'
Generating ... done
```

And with the registry removed, autogenerate is clean — no phantom drops:

```
$ make revision m="sanity empty"
def upgrade() -> None:
    pass
```

---

## Fix 3 — Audit actor via contextvar *(was Major 3)*

**The bug:** `write_audit` took the actor as a keyword argument defaulting to
`"anonymous"`. Every base route remembered to pass it; the probe feature written
during the audit did not, and its rows were attributed to nobody — silently.

### Red

```
$ uv run pytest tests/unit/test_audit_actor.py -q
FAILED ...::test_actor_is_read_from_context_without_being_passed
FAILED ...::test_unbound_actor_is_honest_rather_than_confident
FAILED ...::test_write_audit_uses_the_context_actor
FAILED ...::test_write_audit_records_unresolved_when_no_actor_is_bound
FAILED ...::test_explicit_actor_still_overrides_context
E   ModuleNotFoundError: No module named 'app.audit.context'
```

### Green

```
$ uv run pytest tests/unit/test_audit_actor.py -q
.....                                                                    [100%]
```

### What changed

The actor now travels exactly like `request_id` and `trace_id`:

- `app/core/audit/context.py` holds it in a contextvar;
- the correlation middleware binds `get_current_user()` on **every** request, so
  real auth will populate it later with no further change;
- `build_audit_row()` resolves it from context;
- **nothing bound → `unresolved` plus a warning**, never a plausible-looking
  default. An unknown actor and an anonymous actor are different claims;
- the actor rides on Celery message headers, so a row written inside a task is
  attributed to whoever triggered it.

Base routes no longer pass `actor=` at all — which is what makes the README's
"no call site can forget them" claim true rather than aspirational. That claim
and the auth-seam paragraph are corrected.

### Proven, including across the process hop

```
base route:
  demo.audited | dev | dev | FIX3-ACTOR-001

worker task, enqueued as alice/[admin,ops], task body never mentions an actor:
     action     | actor_id | actor_roles |  request_id
 file.processed | alice    | admin,ops   | FIX3-HOP-001
```

---

## Fix 4 — Audit TRUNCATE guard + privilege hardening *(was Major 4)*

**The bug:** `UPDATE`/`DELETE` were blocked by **row-level** triggers, but
`TRUNCATE` is a **statement-level** event, so a single statement erased the whole
trail. And the app connected as the table **owner**, so it could simply
`DROP TRIGGER`. During the original audit a test did exactly that, successfully.

### Red — run as the application's own role

```
$ uv run pytest tests/integration/test_audit_immutability.py -q -m integration
FAILED ...::test_truncate_is_rejected
FAILED ...::test_app_role_cannot_drop_the_guard
FAILED ...::test_app_role_does_not_own_the_audit_table
FAILED ...::test_truncate_trigger_exists
E   assert 'audit_log_no_truncate' in '... audit_log_no_delete\n(1 row)'
```

Note that only `audit_log_no_delete` appears in that output — because
`test_app_role_cannot_drop_the_guard` had just **succeeded** in dropping
`audit_log_no_update`. That is the vulnerability, reproduced.

### Green

```
$ uv run pytest tests/integration/test_audit_immutability.py -q -m integration
.........                                                                [100%]
```

### Two independent layers

| Layer | What it stops |
|---|---|
| **Grants** | the runtime role has `INSERT, SELECT` on `audit_log` and nothing else |
| **Ownership** | the runtime role owns nothing, so it cannot `DROP TRIGGER` |
| **Triggers** | `BEFORE UPDATE`, `BEFORE DELETE` (row) and `BEFORE TRUNCATE` (statement) reject the operation **even for the owner** |

The role split: `appuser` owns the schema and runs migrations; `appruntime` is
what the app and worker connect as. `Settings` gained `migration_database_url` so
Alembic uses the owner DSN while `database_url` stays the runtime one.

One test asserts the *property* (rejected) rather than one specific message —
because the grant now refuses **before** the trigger is reached, and asserting the
trigger text would fail when the stronger layer catches it. A separate test runs
as the **owner** to prove the triggers still fire, so re-granting the runtime role
by mistake would not silently open the door.

The hardening does not break the app:

```
readiness ok  ·  audit write returns an audit_id  ·  file upload HTTP 201
```

---

## Fix 5 — Redis graceful degradation *(was Major 5)*

**The bug:** stopping Redis turned every cache-backed route into a 500 —
`redis.exceptions.TimeoutError` propagated straight out of `get_or_set`.

### Red

```
$ uv run pytest tests/unit/test_cache_degradation.py -q
FAILED ...::test_get_json_returns_none_instead_of_raising[dead_client0]
FAILED ...::test_get_json_returns_none_instead_of_raising[dead_client1]
FAILED ...::test_set_json_swallows_the_failure[dead_client0]
FAILED ...::test_set_json_swallows_the_failure[dead_client1]
FAILED ...::test_delete_swallows_the_failure[dead_client0]
FAILED ...::test_delete_swallows_the_failure[dead_client1]
FAILED ...::test_get_or_set_falls_through_to_the_origin[dead_client0]
FAILED ...::test_get_or_set_falls_through_to_the_origin[dead_client1]
E   redis.exceptions.ConnectionError: refused
```

### Green

```
$ uv run pytest tests/unit/test_cache_degradation.py -q
..........                                                               [100%]
```

### What changed

`get_json` / `set_json` / `delete` catch `ConnectionError` and `TimeoutError`,
log `cache.unavailable`, and treat the outage as a **miss** — so `get_or_set`
falls through to the origin and returns `(value, hit=False)`.

Deliberately narrow: **only those two transport errors degrade.** A `TypeError`
or a bad serialiser is a bug, not an outage, and still propagates —
`test_unexpected_errors_are_not_swallowed` pins that.

### Proven live

```
$ docker stop cab-redis

/health/ready  -> 503 {"status":"degraded", ..., "redis":"error: timeout after 3s"}
/demo/cached   -> 200 {"key":"demo:cached:7", ..., "cache":"MISS"}

[warning] cache.unavailable  op=get  err=Timeout connecting to server
[warning] cache.unavailable  op=set  err=Timeout connecting to server
[info   ] request.completed  status=200
```

> **On how this was verified.** My first live test still showed 500, because I had
> used `docker compose restart` — which restarts the container with the **old
> image**. The code is baked in, not bind-mounted. Rebuilding produced the result
> above. Recording it because a "the fix didn't work" reading is worthless if the
> fix was never deployed.

---

## Fix 6 — `make install` integrity check *(was Minor 8)*

**The bug:** a partially-failed `uv sync` left `.venv` without a `pyvenv.cfg`.
Every later `uv sync` printed "Checked 109 packages" and exited 0, while `uv run`
silently used the uv-managed interpreter — so `make migrate` died with
`ModuleNotFoundError: No module named 'alembic'` while the package sat on disk.

### Red — the corruption reproduced

```
$ mv .venv/pyvenv.cfg /tmp/          # exactly what a failed sync leaves behind

$ uv sync --all-groups
Resolved 110 packages in 6ms
Checked 109 packages in 155ms        # <- still claims success
```

### Green

```
$ make install
uv run python scripts/verify_venv.py

  make install did not produce a working environment.

  C:\...\Wraper\.venv\pyvenv.cfg is missing, so .venv is not a usable virtualenv.

  Fix: rm -rf .venv && make install
  (on Windows, close anything holding .venv open first -- editors, stray
  pytest.exe processes, antivirus scans.)

make: *** [Makefile:23: install] Error 1
```

Healthy case:

```
$ make install
venv OK: C:\...\Wraper\.venv\Scripts\python.exe
         10 key imports resolve
```

`scripts/verify_venv.py` runs **under `uv run`**, so it inspects the interpreter
real commands will actually use. It checks `pyvenv.cfg` exists, that the
interpreter is inside the project `.venv`, and that ten key imports resolve —
including `app.core.config`, so the app itself must be importable, and `alembic`,
which is what broke. Seven tests cover it, including a fabricated broken tree.

---

# PART 2 — MODULAR SEPARATION

**Behaviour preserved:** 158 tests passed immediately before the reorg and 158
immediately after.

## Infrastructure — one folder per block

```
services/
  app/         Dockerfile + README        (FastAPI, :8000)
  worker/      README                     (Celery; shares the app image)
  postgres/    init/*.sql + README        (:5432)
  redis/       README                     (:6379)
  minio/       README                     (:9000, :9001)
  prometheus/  prometheus.yml + README    (:9090)
  grafana/     provisioning/, dashboards/ (:3001)
  loki/        loki-config.yaml + README  (:3100)
  promtail/    promtail-config.yaml       (ships stdout -> loki)
  tempo/       tempo-config.yaml + README (:3200, :4318)

deploy/
  docker-compose.yml    <- single source of truth for how they connect
  k8s/
```

Each folder contains **only its own config**, and each has a README stating its
port, what it depends on, and its config file. [ARCHITECTURE.md](ARCHITECTURE.md)
holds the request-path diagram and the full service table.

`redis/` and `minio/` have no config file, and their READMEs say so explicitly
rather than leaving an empty file to be kept in step with nothing.

## Application — core vs services

```
app/
  core/       infrastructure. DO NOT EDIT to add a feature.
  services/   business logic. Auto-discovered.
  main.py     assembles the two
```

`demo` and `files` moved into the seam — they are examples to delete, not base
code. `health` and `docs` stayed in core, because probes and documentation are
infrastructure.

### The boundary is enforced, not merely documented

```
$ make lint
Infrastructure must not depend on business logic KEPT
Features may not import each other KEPT
Contracts: 2 kept, 0 broken.
```

Negative control — a planted `from app.services import demo` inside
`app/core/errors.py`:

```
app.core is not allowed to import app.services:
-   app.core.errors -> app.services.demo (l.246)

lint-imports exit=1
```

## The last registry removed

Routers are now discovered too. A module under `app/services/` exposing a
module-level `router` is mounted automatically:

```
"module": "app.services.demo",           "prefix": "/demo"
"module": "app.services.files",          "prefix": "/files"
"module": "app.services.widgets.router", "prefix": "/widgets"
```

With tasks and models already discovered, **adding a feature needs zero edits to
`app/core/**` or `app/main.py`.**

## Two things fixed in passing

**The Celery banner** was the only non-JSON the worker printed, so Promtail could
not parse it into fields. Suppressed with the global `-q` flag.

> I first tried `--without-banner`, which does not exist — the worker crash-looped
> and the flag list corrected me. `-q` is a *global* celery option and must
> precede the subcommand.

**Discovery's own log lines** were then the only non-JSON output, because the
`import_modules` signal fires *before* Celery's `setup_logging`.
`configure_logging()` now runs first:

```
cab-worker log lines: 6   non-JSON: 0     (baseline: 31 lines, 17 non-JSON)
```

## Behaviour preserved — verified after the move

```
make test        158 passed, 88.33% coverage, exit 0
make smoke       SMOKE PASSED, exit 0
docker compose   10/10 containers healthy from deploy/docker-compose.yml
kubeconform      9 resources, Valid: 9, Invalid: 0, Errors: 0, Skipped: 0
lint-imports     2 contracts kept
```

---

# PART 3 — FULL TEST RUN

## 1. Part-1 regression tests — all green

Every fix above shows its red run and its green run. The suite grew 116 → 158.

## 2. Part D — the headline: zero base edits

A probe feature owning **both a Celery task and a DB model**, built in
`app/services/widgets/` as a package:

```
app/services/widgets/
  __init__.py    router.py    models.py    tasks.py
```

### `git diff --stat` — the proof

```
$ git diff --stat
                                    <- empty

$ git status --porcelain
?? app/services/widgets/            <- the feature, and nothing else
```

After autogenerating and applying its migration:

```
$ git diff --stat
                                    <- still empty
$ git status --porcelain
?? app/services/widgets/
?? migrations/versions/20260824_2235_add_widget_table.py
```

**Zero edits to `app/core/**`. Zero edits to `app/main.py`. Zero edits to
anything.** The migration is a new file, not a change to an existing one.

For comparison, the same feature in the baseline audit required 3 base files and
5 lines, 2 of them in protected modules.

### It was discovered, not registered

```
router mounted : "module": "app.services.widgets.router", "prefix": "/widgets"
worker tasks   : [..., "app.services.widgets.tasks"]
migration      : Detected added table 'widget'   (no drops proposed)
```

### Every path works

```
POST /widgets {"name":"gear-alpha","teeth":24}
  201 {"id":"9308173f-...","name":"gear-alpha","teeth":24,"owner":"dev","ratio":8.0,"task_id":"a54b68a1-..."}
GET  /widgets/{id}        200  (served from cache)
POST /widgets/{id}/strip  409  {"error":"conflict","message":"Widget 'gear-alpha' is in service and cannot be stripped.","request_id":"PARTD-WIDGET-003",...}
POST /widgets {"teeth":1} 422  {"error":"validation_error",...,"detail":[{"type":"greater_than_equal","loc":["body","teeth"],...}]}
```

The `201` also proves the `ALTER DEFAULT PRIVILEGES` from Fix 4 covers a
brand-new feature table — the least-privilege role could write to it with no
extra grant.

### D3 — inherited automatically

**Audit, with the actor resolved from context — the Fix 3 payoff:**

```
      action      | actor_id | actor_roles |    request_id
 widget.created   | dev      | dev         | PARTD-WIDGET-001
 widget.reindexed | dev      | dev         | PARTD-WIDGET-001
```

`widget.reindexed` was written **inside the Celery worker**, and neither call site
passed an actor. Under the baseline code both rows would have said `anonymous`.

**Metrics, correctly templated:**

```
app_http_requests_total{handler="/widgets",method="POST",status="201"}                  1.0
app_http_requests_total{handler="/widgets/{widget_id}",method="GET",status="200"}        1.0
app_http_requests_total{handler="/widgets/{widget_id}/strip",method="POST",status="409"} 1.0
app_http_requests_total{handler="/widgets",method="POST",status="422"}                   1.0
```

**Trace with DB, Redis and job-enqueue spans:**

```
otel:fastapi      POST /widgets (+ http receive / http send x2)
otel:sqlalchemy   connect, SELECT, INSERT, connect, SELECT, connect, INSERT
otel:redis        SET
otel:redis        LPUSH                       <- Celery enqueue onto the broker
app.core.middleware.correlation  POST /widgets   <- root span
```

**Logs in Loki** keyed by `request_id`; **security headers**; **the standard error
schema** on both failure paths; **health unchanged and still honest**.

**And the base's own gates accepted it unmodified:**

```
make lint       All checks passed! + 2 import contracts kept
make typecheck  Success: no issues found in 42 source files   (38 -> 42)
make test       158 passed, 86.84% coverage (gate 80%)
```

### D4 — effort proxy

| Category | LOC |
|---|---|
| **Business logic written** (router + model + task, code lines) | **118** |
| **Migration** (autogenerated; hand-written: 0) | 29 |
| **Plumbing / infrastructure written** | **0** |
| **Base edits** | **0** |

For 118 lines carrying one business rule (`teeth / 3.0`), the feature inherited
correlation, structured logging, tracing, metrics, the error schema, security
headers, the audit trail *with the actor already resolved*, DB sessions, caching,
graceful cache degradation, background jobs, health probes and CI.

### D5 — clean removal

```
$ ALLOW_DESTRUCTIVE=1 uv run alembic downgrade -1
Running downgrade 8e10bcb202c4 -> a1f2c3d4e5b6, add widget table
$ rm -rf app/services/widgets migrations/versions/20260824_2235_add_widget_table.py

$ git status --porcelain     <- clean
$ git diff --stat            <- empty
```

After a rebuild, no residue:

```
POST /widgets  -> 404
worker tasks   : ["app.services.demo", "app.services.demo_tasks", "app.services.files"]
```

## 3. No-regression sweep

### Correlation for `TEST-123` — end to end

```
POST /demo/audited  -H "X-Request-ID: TEST-123"
  HTTP/1.1 200 OK   x-request-id: TEST-123   x-trace-id: 98f7b54372ca66cc1e4850ec67d232db

GET /demo/boom      -H "X-Request-ID: TEST-123-BOOM"
  HTTP/1.1 500      x-request-id: TEST-123-BOOM   x-trace-id: ee38957b5842ad72dc19f368709b51b3
```

| Leg | Result | Evidence |
|---|---|---|
| **Logs (Loki)** | OK | 2 lines — `request.completed`, `audit.written`, both `trace=98f7b54372ca66cc` |
| **Trace (Tempo)** | OK | 6 spans; base64 traceId decodes to exactly `98f7b54372ca66cc1e4850ec67d232db` |
| **Audit (Postgres)** | OK | `demo.audited \| dev \| 98f7b543…` and `demo.boom \| dev \| ee38957b…` |
| **Metric (Prometheus)** | OK | `/demo/audited` 200: 2.0 → 3.0 |
| **Error** | OK | correlated 500 body, both ids, no stack trace |

The audit `actor_id` is now `dev` on both rows — it was `anonymous` in the baseline.

`make smoke` reproduces this independently, querying each store directly:

```
  operation                  request_id                             log   trace  audit  metric error worker
  upload a file              43584197-d28e-45fa-80c1-c1448ba43236   OK    OK     OK     OK     --    --
  trigger a background job   55834df3-3f63-4cc9-be73-9b80d9b854e6   OK    OK     --     OK     --    OK
  trigger /demo/boom         b8ebff20-946f-4039-acc2-c7e10f67af24   OK    OK     OK     OK     OK    --

  SMOKE PASSED - one request_id joins logs, traces, audit and metrics.
```

### Readiness honesty — both dependencies

```
POSTGRES DOWN   live: 200   ready: 503 {"status":"degraded", ..., "postgres":"error: timeout after 3s"}
                recovered in 1s
REDIS DOWN      live: 200   ready: 503 {"status":"degraded", ..., "redis":"error: timeout after 3s"}
```

Liveness stayed 200 throughout both outages.

### Audit rejects UPDATE / DELETE / TRUNCATE

Nine integration tests, run as the application's own role, all green — including
`test_truncate_is_rejected` and `test_app_role_cannot_drop_the_guard`, both of
which failed before Fix 4.

### The Trivy gate is real

```
clean tree           exit = 0
planted secret       exit = 1     <- build correctly fails
after removal        exit = 0

make scan            exit = 0
common-app-base:local (debian 12.15)   Vulnerabilities: 0
```

## 4. Degradation

```
$ docker stop cab-redis
/health/ready              -> 503 (naming redis)
/demo/cached               -> 200   (base route)
/widgets/{id}              -> 200   (the new feature, degrading for free)
```

## 5. Repeatability — twice, identical

| Command | Run 1 | Run 2 |
|---|---|---|
| `make lint` | exit 0 | exit 0 |
| `make typecheck` | exit 0, 38 files | exit 0, 38 files |
| `make test` | exit 0, **158 passed**, **88.33%** | exit 0, **158 passed**, **88.33%** |
| `make smoke` | exit 0, SMOKE PASSED | exit 0, SMOKE PASSED |
| `make scan` | exit 0 | exit 0 |

No flaky check was observed.

## 6. Clean removal

Covered under D5: suite and smoke green, `git status` clean, empty diff.

---

# DEFECT LIST — STATUS

| # | Severity | Defect | Status |
|---|---|---|---|
| 1 | **Blocker** | A feature cannot own its Celery task | **Fixed** — autodiscovery + `enqueue()` guard + fatal import errors |
| 2 | **Blocker** | Model registry is a silent data-loss trap | **Fixed** — discovery + destructive-op guard; registry deleted |
| 3 | Major | Audit actor silently `anonymous` | **Fixed** — contextvar, bound by middleware, carried across the Celery hop |
| 4 | Major | `TRUNCATE` bypasses append-only | **Fixed** — statement-level trigger + least-privilege role that owns nothing |
| 5 | Major | Cache outage becomes a full outage | **Fixed** — narrow degradation to a miss; bugs still propagate |
| 6 | Major | CORS wide open by default | **Fixed** (2026-08-25) — deny by default; `*` is an explicit opt-in |
| 7 | Major | Dev credentials as field defaults, no prod guard | **Fixed** (2026-08-25) — non-local env refuses to start on a shipped default |
| 8 | Minor | `make install` not self-healing | **Fixed** — `verify_venv.py` |
| 9 | Minor | One exception → three error log records | **Fixed** (2026-08-25) — one traceback, at one place |
| 10 | Minor | Worker emits non-JSON banner lines | **Fixed** — `-q` + logging configured before discovery; 0 non-JSON |
| 11 | Minor | Chunked over-limit returns 400, not 413 | **Fixed** (2026-08-25) — 413 in the standard schema |
| 12 | Minor | No retry policy demonstrated | **Fixed** (2026-08-25) — policy on the base task, inherited by every task |

**All twelve defects are fixed.** Both blockers and three majors under
`SOLIDIFY_BRIEF.md`; the remaining two majors and three minors under
`HARDEN_BRIEF.md` on 2026-08-25.

`SOLIDIFY_BRIEF.md` specified exactly six fixes (the two blockers, majors 3–5, and
minor 8). Defects 6, 7, 9, 11 and 12 were not in its scope and were deliberately
left alone rather than silently expanded into. #10 was fixed only because the
reorg's log-shipping work made it a one-line consequence.

All five were closed on 2026-08-25 under `HARDEN_BRIEF.md`; the evidence is in
the hardening pass above.

---


---

# HARDENING PASS (2026-08-25)

`SOLIDIFY_BRIEF.md` proved the *structure*. This pass closes the five defects it
left out of scope, all of them configuration or observability rather than
architecture. Same discipline: a failing test first, then the fix.

**Result: 184 tests (was 158), 88.91% coverage, suite identical across two runs.**

| Defect | Was | Now |
|---|---|---|
| #7 prod credentials | `ENVIRONMENT=prod` booted on `apppassword` | startup **refuses**, naming every offending field |
| #6 CORS | `*` by default | deny by default; `*` must be typed |
| stale docstring | "include its router in `app/main.py`" | corrected, and pinned by a grep test |
| #9 error logs | 3 records, 3 tracebacks per exception | 1 error record, 1 traceback |
| #11 chunked over-limit | `400 "error parsing the body"` | `413 payload_too_large` in the standard schema |
| #12 retries | none | policy on the base task, inherited by every task |

## #7 - a deployed environment cannot run on the local secrets

### Red

```
$ pytest tests/unit/test_prod_credentials_guard.py -q
FAILED ...::test_deployed_environments_reject_dev_defaults[dev]
FAILED ...::test_deployed_environments_reject_dev_defaults[staging]
FAILED ...::test_deployed_environments_reject_dev_defaults[prod]
FAILED ...::test_the_guard_is_derived_not_a_hand_maintained_list
E   ImportError: cannot import name 'dev_default_secret_fields' from 'app.core.config'
```

### Green

```
$ pytest tests/unit/test_prod_credentials_guard.py -q
....                                                                     [100%]
```

### Proof against the built image

```
$ docker run --rm -e ENVIRONMENT=prod --entrypoint python common-app-base:local -c "import app.main"
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  Value error, ENVIRONMENT='prod' is still using the local development defaults
  for: postgres_app_password, postgres_password, s3_access_key, s3_secret_key.
  Set each one from your secret store (see SECRETS_PROVIDER) before deploying.
exit=1

$ docker run --rm -e ENVIRONMENT=prod -e POSTGRES_PASSWORD=real-pw \
    -e POSTGRES_APP_PASSWORD=real-app-pw -e S3_ACCESS_KEY=real-key \
    -e S3_SECRET_KEY=real-secret --entrypoint python common-app-base:local -c "..."
booted in prod

$ docker run --rm --entrypoint python common-app-base:local -c "..."
booted in local
```

The guarded field set is **derived from the model** -- a field whose name looks
like a credential and whose default is a non-empty string -- so a secret added
next year is covered the day it is added. A locator (`azure_key_vault_url`) is
not a secret and is excluded, which the test pins.

## #6 - CORS denies by default

### Red

```
$ pytest tests/unit/test_cors_default.py -q
FAILED ...::test_cors_denies_by_default              assert ['*'] == []
FAILED ...::test_the_shipped_env_example_does_not_enable_wildcard_cors
FAILED ...::test_default_config_blocks_a_cross_origin_browser   assert not True
```

### Green

```
$ pytest tests/unit/test_cors_default.py -q
.......                                                                  [100%]
```

The behavioural tests drive a real `CORSMiddleware` and check whether a browser
at a given origin would be allowed to read the response -- not just what the
settings object holds. `.env.example` ships denied too, since that is the file
people copy.

```
$ docker exec cab-app python -c "from app.core.config import settings; print(settings.cors_origins_list)"
[]
```

## Stale docstring

### Red

```
$ pytest tests/unit/test_docs_tell_the_truth.py -q
E   AssertionError: Stale 'edit main.py' instructions:
E     app/services/__init__.py:9: or a package in here, include its router in ``app/main.py``
```

### Green

```
$ pytest tests/unit/test_docs_tell_the_truth.py -q
..                                                                       [100%]
```

The test greps `app/`, `README.md` and `ARCHITECTURE.md`, so the docs cannot
drift back. The docstring now also records the one step discovery genuinely
cannot do: `make revision` + `make migrate` for a new model.

## #9 - one exception, one traceback

### Red

```
$ pytest tests/unit/test_single_error_record.py -q
E   AssertionError: expected one traceback, got ['request.failed', 'request.unhandled_exception']
E   ImportError: cannot import name 'DropAlreadyLoggedTraceback' from 'app.core.logging'
```

### Green

```
$ pytest tests/unit/test_single_error_record.py -q
..                                                                       [100%]
```

### Under uvicorn, in the container

```
$ curl -H "X-Request-ID: ONE-ERROR-1787634194" localhost:8000/demo/boom
$ docker logs cab-app | grep ONE-ERROR-1787634194

 info  audit.written                    traceback=False
 info  request.completed                traceback=False
error  request.unhandled_exception      traceback=True     <- the only one
```

Baseline was three tracebacks (middleware, handler, uvicorn). Uvicorn's copy is
dropped by a filter keyed on the **exception object**, not on a logger name or a
message string -- so an exception nobody logged still gets its stack printed.
Losing the only copy of a traceback would be a worse bug than printing one twice;
`test_an_unlogged_exception_keeps_its_traceback` pins that.

## #11 - an over-limit chunked body is 413

### Red

```
$ pytest tests/unit/test_chunked_size_limit.py -q
E   assert 500 == 413
E   AssertionError: assert 'internal_error' == 'payload_too_large'
```

### Green

```
$ pytest tests/unit/test_chunked_size_limit.py -q
....                                                                     [100%]
```

### Against the running stack -- 12 MiB, no `Content-Length`

```
HTTP 413
{"error":"payload_too_large",
 "message":"Request body exceeds the 10485760 byte limit.",
 "request_id":"0509631a-13f7-493c-8921-0823afb5c135",
 "trace_id":"32cff49d7d05468355d5d87d873709ab"}
```

Memory was always bounded; the status code was the lie. The induced disconnect
is now internal: the app's confused response to a truncated body is dropped and
the real reason is sent. The induced exception is swallowed **only** when we
induced it.

## #12 - every task retries transient failures

### Red

```
$ pytest tests/unit/test_retry_policy.py -q
E   ImportError: cannot import name 'BaseTask' from 'app.core.jobs.celery_app'
```

### Green

```
$ pytest tests/unit/test_retry_policy.py -q
.....                                                                    [100%]
```

### On the real worker

```
$ enqueue('demo.flaky', 2)
task_id 81bfd7ea-799e-40a7-baad-12fa73df09bb
result  {'attempts': 3, 'retries': 2, ...}          <- RETRY, RETRY, SUCCESS

worker log:
  flaky.attempt attempt=0
  Task demo.flaky[81bfd...] retry: Retry in 0s: ConnectionError('transient failure 1 of 2')
  flaky.attempt attempt=1
  Task demo.flaky[81bfd...] retry: Retry in 0s: ConnectionError('transient failure 2 of 2')
  flaky.attempt attempt=2                            <- succeeds

$ enqueue('demo.flaky', 99)                          # never recovers
final state FAILURE
error       ConnectionError('transient failure 4 of 99')   <- 1 attempt + 3 retries, capped
```

The policy lives on the base task class (`celery_app`'s `task_cls`), so a
feature inherits it by existing. Bugs are deliberately excluded: a `TypeError`
fails once, immediately, because retrying it reaches the identical failure three
more times.

---

# RE-VERIFICATION AFTER HARDENING

## Suite, twice, identical

```
run 1   184 passed   coverage 88.91%
run 2   184 passed   coverage 88.91%
```

## No-regression sweep

| Control | Result |
|---|---|
| Correlation smoke (logs -> trace -> audit -> metric -> error, one request_id) | `SMOKE PASSED` |
| Readiness names the failing dep (Postgres down) | `503` + `"postgres":"error: timeout after 3s"`, liveness `200` |
| Readiness names the failing dep (Redis down) | `503` + `"redis":"error: timeout after 3s"` |
| Redis down -> cached route still serves | `GET /demo/cached` -> `200` (degraded) |
| Audit rejects UPDATE / DELETE / TRUNCATE as the app role | all three `permission denied for table audit_log` |
| Audit trigger cannot be dropped by the app role | `must be owner of relation audit_log` |
| Audit still writable/readable | 174 rows, INSERT+SELECT fine |
| Trivy gate, clean tree | `exit 0` |
| Trivy gate, planted GitHub PAT | `CRITICAL: GitHub (github-pat)`, `exit 1` |
| Trivy image scan | `common-app-base:local (debian 12.15)` - 0 vulnerabilities, `exit 0` |

> Note on the secret control: a planted **AWS documentation example key** is
> *not* flagged, and that is Trivy being right rather than the gate being weak.
> The control uses a realistic GitHub PAT.

## Seam intact - the docs POC, re-run on the hardened base

```
$ git status --porcelain
?? app/services/docs/
?? migrations/versions/20260825_1040_poc_document_table.py
?? tests/unit/test_docs_poc.py

app/core edits: 0
```

Round trip on the running stack:

```
POST /documents          -> 201 {"id":"df4fcc2c-...","task_id":"f7ac7d89-..."}
GET  /documents/{id}     -> {"status":"done","word_count":1427,"cached":true}

audit_log for request_id POC2-1787634662:
  document.uploaded | dev | POC2-1787634662
  document.counted  | dev | POC2-1787634662      <- written inside the Celery task
```

The feature's task also inherits the new retry policy without asking
(`test_the_feature_task_inherits_the_retry_policy`).

### Clean removal

```
$ alembic downgrade -1 && rm -rf app/services/docs ...
$ git status --porcelain
                                          <- empty
$ pytest -q            184 passed
$ curl -X POST localhost:8000/documents   -> 404
```

# FINAL STATE

```
$ git log --oneline
f23decc fix-12: every task retries transient failures, with backoff and a cap
cd10655 fix-11: an over-limit chunked body is 413, not 400
288e5d4 fix-9: one exception, one traceback
996d5c0 docs: the plugin seam docstring said to edit main.py
a8f211e fix-6: CORS denies by default; the wildcard is opt-in
8e55450 fix-7: refuse to start a deployed environment on the local secrets
dfae256 docs: record the remediation and the READY verdict
d1d00c0 part-2: one folder per service, and a core/services boundary in the app
58db1ec fix-1b: load tasks lazily so the registry never depends on lifespan
e215b1a fix-6: make `make install` tell the truth
ab4e813 fix-5: a cache outage should cost latency, not uptime
50b78d9 fix-4: make audit immutability real, not advisory
531fed9 fix-3: carry the audit actor in a contextvar, not an argument
228f9b7 fix-2: discover models, and refuse to autogenerate a silent drop
8e13069 fix-1: discover Celery tasks instead of listing them
22319b3 (the audited baseline)
```

**Verdict: READY - and, as of the 2026-08-25 hardening pass, prod-ready.**

The distinction matters. The first verdict said the *structure* was sound: a
feature plugs in with zero base edits. It did not say the base was safe to
deploy, because `ENVIRONMENT=prod` would happily boot on a published password
and answer every browser origin on earth. Both are now impossible.

The base now holds the property it was built for: a new feature is a directory
under `app/services/`, and everything else — correlation, logging, tracing,
metrics, errors, audit with a resolved actor, caching that degrades, background
jobs, security headers, migrations that refuse to drop your data, CI — applies to
it without a single line of infrastructure being written or edited.
