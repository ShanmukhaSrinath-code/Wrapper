# Common Application Base

A reusable FastAPI base. The point of it: **you write business logic, you inherit
the plumbing.** Correlation, logging, tracing, metrics, audit, errors, caching,
object storage, background jobs, health probes, security headers, CI and the
container build are already here and already wired together.

If you are an AI assistant working in this repo, read this file first. It is
short on purpose.

---

## The one rule

```
app/core/      <- infrastructure. DO NOT EDIT to add a feature.
app/services/  <- business logic goes HERE. Everything is auto-discovered.
```

Adding a feature must require **zero** edits to `app/core/**` and **zero** edits
to `app/main.py`. If you find yourself editing either one to make a feature
work, stop: you have almost certainly missed a seam that already exists. Ask
before crossing the boundary.

This is enforced mechanically, not by convention — an import-linter contract
fails the build if `app.core` imports `app.services`. `make lint` runs it.

## What is auto-discovered

Drop a module or a package into `app/services/` and define:

| Define | And you get |
|---|---|
| `router = APIRouter(...)` | mounted on the app — no edit to `main.py` |
| `@celery_app.task(...)` | registered with the worker, and it inherits the retry policy |
| a SQLAlchemy model | in `Base.metadata`, so Alembic autogenerate sees it |

Discovery walks the packages named by `PLUGIN_PACKAGES` (default `app.services`)
and **fails loudly at startup** if a module raises on import. A half-loaded
plugin set is worse than a stopped process.

## The one step discovery does NOT do

**A new model does not create its table.** Run:

```bash
make revision m="add the thing table"   # autogenerate
make migrate                            # apply
```

Discovery makes the model *visible* to Alembic; nothing in this base writes DDL
behind your back. Autogenerate also **refuses to emit a drop** unless
`ALLOW_DESTRUCTIVE=1` — if you see that error, the usual cause is a model that
was not imported, not a table that should really go.

---

## Writing a feature

Use the `add-feature` skill (`.claude/skills/add-feature/`) for the full recipe.
The short version — a package under `app/services/`:

```
app/services/thing/
  __init__.py
  models.py     # SQLAlchemy models
  router.py     # module-level `router = APIRouter(prefix="/things")`
  tasks.py      # @celery_app.task functions
```

Two conventions that matter:

- **Take `user: CurrentUser` on any route that will ever need an identity.**
  Auth is a stub today (`get_current_user()` returns a `dev` principal). When
  real auth lands, only `app/core/security/current_user.py` changes and no
  route signature moves.
- **Raise `AppError` subclasses** (`NotFoundError`, `ConflictError`, …) for
  expected failures. They become the right status code in the standard error
  shape, are logged as warnings, and are *not* sent to Sentry — a 404 is not a
  bug.

What you must never hand-roll, because it is already applied to your code:
correlation ids, log format, trace spans, `/metrics` counters, the error JSON
shape, security headers, DB session lifecycle, cache degradation, audit
attribution, health probes, task retries.

## Import these, not the implementations

```python
from app.core.db.session import DbSession          # route dependency
from app.core.db import Base                       # model base
from app.core import cache                         # get_json / set_json / get_or_set
from app.core.storage import get_storage           # the Storage interface
from app.core.jobs import enqueue                  # verifies the task is registered
from app.core.audit import write_audit             # actor + ids come from context
from app.core.errors import NotFoundError          # and friends
from app.core.logging import get_logger            # log = get_logger(__name__)
from app.core.security.current_user import CurrentUser
```

Never import `boto3`, `redis`, or a driver directly from a feature. Depend on
the interface so the backend stays swappable and fakeable.

---

## Commands

```bash
make install   # uv sync + verify the venv actually works
make up        # start the whole stack (10 services)
make migrate   # apply migrations
make test      # full suite, 80% coverage gate
make lint      # ruff + format check + the core/services boundary
make typecheck # mypy
make smoke     # prove one request_id joins logs, trace, audit and metrics
make down      # stop the stack, remove volumes
```

**Before claiming a change is done, run `make lint` and `make test`.** Both are
fast and both are what CI runs.

## Local URLs

| What | Where |
|---|---|
| API / Swagger | <http://localhost:8000> · `/docs` |
| Grafana | <http://localhost:3001> (provisioned; anonymous viewer) |
| Prometheus | <http://localhost:9090> |
| MinIO console | <http://localhost:9001> (`minioadmin`/`minioadmin`) |

---

## Things that look broken and are not

- **Grafana panels flat at zero.** Every metric panel is rate-based; an idle app
  shows zeros. The "Total requests (30m)" stat is the one that reads true while
  idle. Generate traffic to see the rest.
- **Sentry disabled.** No DSN locally, logged as `sentry.disabled`. Deliberate.
- **Tempo empty / traces "missing".** Trace *export* is off locally: Tempo is
  behind the `tracing` compose profile and `OTEL_EXPORTER_OTLP_ENDPOINT` is
  blank. Spans are still created, so `trace_id` correlation works regardless.
  To ship them: `OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318` plus
  `--profile tracing`.
- **Integration/e2e tests "skipping".** They skip with a reason when the compose
  stack is down, so a missing stack never looks like a passing suite. Run
  `make up` first.
- **`demo.py`, `demo_tasks.py`, `files.py` in `app/services/`.** Scaffolding that
  proves the plumbing and feeds `make smoke`. Delete them when real features
  land — but note ~20 tests and the smoke script drive them, so delete the
  tests too.

## Deploying

The base refuses to start outside `local` on the shipped credentials:

```
ENVIRONMENT=prod is still using the local development defaults for:
postgres_password, s3_secret_key, ...
```

That is intentional. Supply real secrets. Also note `CORS_ALLOW_ORIGINS`
defaults to **deny** — `*` is available but has to be typed.

## House style

- Comments explain **why**, not what. Match the density of the surrounding code.
- Type hints on everything in `app/` (`disallow_untyped_defs` is on).
- Line length 100, `ruff format`.
- Tests are reproduce-first for bugs: write the failing test, then fix.

More detail: [README.md](README.md) for usage, [ARCHITECTURE.md](ARCHITECTURE.md)
for the service map, [BUILD_LOG.md](BUILD_LOG.md) for why things are the way they
are, [TEST_REPORT.md](TEST_REPORT.md) for what has been verified.
