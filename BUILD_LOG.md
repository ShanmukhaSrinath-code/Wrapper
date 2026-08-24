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

