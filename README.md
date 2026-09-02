# Common Application Base

A reusable FastAPI service template. Clone it, delete the demo routes, and write
only business logic — configuration, database, cache, object storage, background
jobs, logging, metrics, tracing, auditing, error reporting, security headers,
tests, CI and Kubernetes manifests are already wired together **and correlated**.

How the services fit together: [ARCHITECTURE.md](ARCHITECTURE.md).
Per-phase build history with real gate output: [BUILD_LOG.md](BUILD_LOG.md).
Independent acceptance audit: [TEST_REPORT.md](TEST_REPORT.md).

---

## Quick start

```bash
cp .env.example .env
make install     # install Python 3.12 + all dependencies (uv)
./run.sh        # start the full stack, migrate, print every URL
make migrate     # apply database migrations
make smoke       # prove every component is correlated
```

Then:

| What | Where |
|---|---|
| API | <http://localhost:8000> |
| Swagger UI / ReDoc | <http://localhost:8000/docs> · <http://localhost:8000/redoc> |
| Grafana | <http://localhost:3001> (`admin` / `admin`) |
| Prometheus | <http://localhost:9090> |
| Loki | <http://localhost:3100> |
| MinIO console | <http://localhost:9001> (`minioadmin` / `minioadmin`) |

`docker compose up` starts: **app, worker, postgres, redis, minio, loki,
promtail, prometheus, grafana**. Traces are stored by an opt-in profile:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318 \
  docker compose -f deploy/docker-compose.yml --profile tracing up -d
```

### Prerequisites

- Docker + Docker Compose
- [`uv`](https://docs.astral.sh/uv/) on `PATH` (it installs Python 3.12 itself)
- GNU Make

> On Windows, install both with
> `winget install ezwinports.make` and `pip install uv`, then make sure
> `%LOCALAPPDATA%\Microsoft\WinGet\Packages\ezwinports.make_*\bin` and
> `%APPDATA%\Python\Python3xx\Scripts` are on `PATH`.

---

## The correlation model

This is the backbone of the template, and the reason `make smoke` exists.

A single middleware ([`app/middleware/correlation.py`](app/middleware/correlation.py))
runs outermost on every request. It:

1. accepts an inbound `X-Request-ID` (validated — length and alphabet capped, so
   a hostile header cannot forge log lines) or mints a UUID4;
2. opens an OpenTelemetry span, so a real `trace_id` and `span_id` exist;
3. binds all three into **structlog's contextvars**;
4. returns `X-Request-ID` and `X-Trace-ID` on the response.

Because the ids live in contextvars rather than being passed around, everything
downstream inherits them automatically:

| Component | How it is correlated | Where to look |
|---|---|---|
| **Logs** | Every JSON line carries `request_id` / `trace_id` — including uvicorn, SQLAlchemy and Celery lines, because stdlib logging is routed through the same pipeline | Grafana → Explore → Loki:<br>`{service="app"} \| json \| request_id = "<id>"` |
| **Traces** | The middleware's span is the request's root span | Tempo (`tracing` profile), or click *View trace* on a log line |
| **Audit** | `write_audit()` reads the ids **and the acting principal** from context — no call site can forget them | `SELECT * FROM audit_log WHERE request_id = '<id>'` |
| **Metrics** | Labelled by route/method/status **only**. Ids are deliberately *not* labels: they are unbounded and would destroy cardinality | Prometheus / the provisioned Grafana dashboard |
| **Errors** | Every error response is `{error, message, request_id, trace_id}`; Sentry events are tagged with both | Sentry, filtered by the `request_id` tag |
| **Jobs** | Ids ride on the Celery message headers and are rebound inside the worker, so a task logs under the request that enqueued it | `docker logs cab-worker \| grep <id>` |

Given one `request_id` you can find the logs, the trace, the audit row, the
metric movement and the error. `make smoke` asserts exactly that and prints a
table; it exits non-zero if any leg is missing.

```
  operation                  request_id                             log   trace  audit  metric  error  worker
  upload a file              8da264a6-57c3-4161-a21f-c2359640ad71   OK    OK     OK     OK      --     --
  trigger a background job   76fbaed9-2abe-4db7-8cc6-0eec14e36a4f   OK    OK     --     OK      --     OK
  trigger /demo/boom         fa82d638-44f1-4c76-82dd-323a8a7959ea   OK    OK     OK     OK      OK     --
```

---

## Where your code goes

```
app/
  core/       <- infrastructure. DO NOT EDIT to add a feature.
  services/   <- your business logic goes HERE. Everything is auto-discovered.
```

Drop a module (or a package) into [`app/services/`](app/services/):

| Define | And you get |
|---|---|
| `router = APIRouter(...)` | mounted on the app — no edit to `main.py` |
| `@celery_app.task` | registered with the worker — no list to update, and it inherits the retry policy |
| a SQLAlchemy model | in `Base.metadata`, seen by Alembic autogenerate |

Then `make revision m="..."` and `make migrate`. **Adding a feature requires
zero edits to `app/core/**` or `app/main.py`** — there are no registries to
forget. See [ARCHITECTURE.md](ARCHITECTURE.md).

The migration step is the one thing discovery does not do for you, and that is
deliberate: discovery makes your model *visible* to Alembic, it does not touch
your database. Nothing in this base writes DDL behind your back.

Everything else — correlation, error shape, security headers, health probes,
metrics, audit — applies to your new code automatically. Two conventions are
worth keeping:

- Take `user: CurrentUser` on any route that will ever need an identity, so
  real auth drops in with no signature changes.
- Raise `AppError` subclasses (`NotFoundError`, `ConflictError`, …) for expected
  failures. They become the right status code in the standard error shape, are
  logged as warnings, and are *not* sent to Sentry — a 404 is not a bug.

---

## Common targets

Run `make help` for the full list.

| Target | What it does |
|---|---|
| `make install` | Install Python 3.12 and every dependency group |
| `make lint` / `make fmt` | ruff check + format + import-boundary contracts |
| `make typecheck` | mypy |
| `make run` | Run the API locally with reload |
| `make up` / `make down` | Start / tear down the compose stack |
| `make migrate` | Apply Alembic migrations |
| `make revision m="..."` | Autogenerate a migration |
| `make test` | Full suite with the 80% coverage gate |
| `make scan` | Trivy dependency + image scan |
| `make smoke` | Full-stack correlation proof |

---

## Health and probes

| Endpoint | Behaviour | Used by |
|---|---|---|
| `/health/live` | Always 200 while the process responds. Touches **no** dependency | `livenessProbe`, `startupProbe` |
| `/health/ready` | Runs every registered dependency check; 503 if any is down | `readinessProbe` |

Liveness deliberately ignores dependencies: if it checked the database, a slow
database would get healthy pods killed and turn a partial outage into a total
one. Readiness *does* check them, so an unhealthy pod leaves the Service instead
of serving errors.

Add a dependency check without touching the health module:

```python
health.register_readiness_check("my_thing", my_async_ping)
```

---

## Swap points

The template is local-first but cloud-swappable. Two seams are interfaces:

| Seam | Today | Production swap |
|---|---|---|
| **Secrets** | `EnvSecrets` (env vars) | `AzureKeyVaultSecrets` — set `SECRETS_PROVIDER=azure_key_vault` and `AZURE_KEY_VAULT_URL`. See [`app/config.py`](app/config.py) |
| **Identity** | `get_current_user()` returns a stub principal | Entra ID + Casbin. See below |

**Object storage** is **MinIO** (S3 API). Call sites depend on the
[`Storage`](app/storage/base.py) interface rather than on boto3, which keeps the
object store faked in unit tests — and because MinIO speaks the S3 API, the same
adapter works unchanged against real S3 by changing the endpoint and credentials.

### The auth seam

**Authentication is deliberately not implemented.**
[`app/security/current_user.py`](app/security/current_user.py) defines a
`Principal` model and a `get_current_user()` dependency that returns a fixed
stub (`id="dev"`, `roles=["dev"]`).

Every route that will ever need a caller already depends on it:

```python
@router.post("/things")
async def create(user: CurrentUser, session: DbSession) -> Thing: ...
```

To add real auth, change **only that one file**: validate the bearer token
against the Entra ID JWKS, map the claims onto `Principal`, and enforce a Casbin
policy. No route signature moves, and audit rows start recording real actors
immediately, because the correlation middleware binds whatever
`get_current_user()` returns into the audit context — see
[`app/audit/context.py`](app/audit/context.py). A row written by a feature that
never mentions an actor is still attributed correctly; if nothing is bound it
records `unresolved` and logs a warning rather than inventing `anonymous`.

---

## Configuration

All configuration is environment-driven via `Settings`
([`app/core/config.py`](app/core/config.py)); see [`.env.example`](.env.example)
for every variable with its default. Nothing sensitive is hard-coded — `make
scan` runs a Trivy secret scan over the tree to keep it that way.

### What changes the moment `ENVIRONMENT` is not `local`

The defaults in this repo exist so `make up` works from a clean clone. Two of
them would be dangerous anywhere else, so the base refuses to let them travel:

- **Dev credentials do not survive.** Set `ENVIRONMENT` to `dev`, `staging` or
  `prod` and startup **fails** — naming every field still on its shipped
  default — until each real secret is supplied. The check is derived from the
  model, so a credential added later is covered the day it is added:

  ```
  ValidationError: ENVIRONMENT='prod' is still using the local development
  defaults for: postgres_app_password, postgres_password, s3_access_key,
  s3_secret_key. Set each one from your secret store (see SECRETS_PROVIDER)
  before deploying.
  ```

- **CORS denies by default.** `CORS_ALLOW_ORIGINS` is empty unless you set it.
  `*` is available, but it has to be typed — it is never what you get by
  forgetting. (Server-to-server callers are unaffected; CORS is a browser
  mechanism.)

### Retries

Every task inherits a retry policy from the base task class: transient failures
(`ConnectionError`, `TimeoutError`, `OSError`, SQLAlchemy `OperationalError`,
botocore errors) are retried with exponential backoff and jitter, capped at
`TASK_MAX_RETRIES` (default 3). A `TypeError` in your task body is a bug, not a
blip, and is **not** retried — it would fail identically three more times.

`demo.flaky` demonstrates it end to end.

---

## Off by default, on purpose

These are deliberate local defaults, not gaps:

| Thing | State locally | Turn it on with |
|---|---|---|
| **Sentry** | disabled, logged as `sentry.disabled` | `SENTRY_DSN=...` |
| **Tracing → Tempo** | spans are generated for correlation but not exported | `OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318` and `--profile tracing` |
| **Schema creation** | never automatic | `make revision m="..."` then `make migrate` |

---

## Testing

```bash
make test              # everything, with the coverage gate
make test-unit         # no stack required
make test-integration  # needs `make up`
make test-e2e          # Playwright, needs `make up`
```

Unit tests need nothing running. Integration and e2e tests **skip with a
reason** when the stack is down, so a missing stack can never look like a
passing suite.

---

## CI/CD

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs
`lint → test → scan → build → deploy`. The image is built, **scanned, and
smoke-tested before it is pushed**, so a failing scan cannot ship an artefact.
The deploy job renders the manifests on every run and stubs the apply until a
cluster is wired up.

Kubernetes manifests are in [`deploy/k8s/`](deploy/k8s/):

```bash
kubectl kustomize deploy/k8s                 # render
kubectl kustomize deploy/k8s | kubeconform - # validate offline
kubectl apply -k deploy/k8s                  # deploy
```
