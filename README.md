# Common Application Base

A reusable FastAPI service template. Clone it, delete the demo routes, and write
only business logic — configuration, database, cache, object storage, background
jobs, logging, metrics, tracing, auditing, error reporting, security headers,
tests, CI and Kubernetes manifests are already wired together and *correlated*.

Build status and per-phase gate output: [BUILD_LOG.md](BUILD_LOG.md).

## Prerequisites

- Python 3.12 (installed automatically by `uv`)
- [`uv`](https://docs.astral.sh/uv/) on `PATH`
- GNU Make
- Docker + Docker Compose

## Quick start

```bash
cp .env.example .env
make install     # create the venv, install deps
make up          # start the full stack
make smoke       # prove every component is correlated
```

## Common targets

Run `make help` for the full list.

| Target | What it does |
|---|---|
| `make install` | Create the venv and install all dependency groups |
| `make lint` | `ruff check` + `ruff format --check` |
| `make typecheck` | `mypy app` |
| `make run` | Run the API locally with reload |
| `make up` / `make down` | Start / tear down the compose stack |
| `make migrate` | Apply Alembic migrations |
| `make test` | Full test suite with the coverage gate |
| `make smoke` | Full-stack correlation smoke test |

## Swap points

The template is local-first but cloud-swappable. Two seams are deliberately
left as interfaces:

| Seam | Local implementation | Production swap |
|---|---|---|
| Secrets | `EnvSecrets` (env vars) | `AzureKeyVaultSecrets` — set `SECRETS_PROVIDER=azure_key_vault` |
| Identity | `get_current_user()` stub principal | Entra ID + Casbin |

Object storage is **MinIO** (S3 API). Call sites depend on the
[`Storage`](app/storage/base.py) interface rather than on boto3, which keeps
the object store faked in unit tests — and because MinIO speaks the S3 API,
the same adapter works unchanged against real S3 by changing the endpoint
and credentials.

See [app/config.py](app/config.py) for the secrets interface.
