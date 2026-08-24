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

The template is local-first but cloud-swappable. Three seams are deliberately
left as interfaces:

| Seam | Local implementation | Production swap |
|---|---|---|
| Secrets | `EnvSecrets` (env vars) | `AzureKeyVaultSecrets` — set `SECRETS_PROVIDER=azure_key_vault` |
| Object storage | `MinioStorage` (S3 API) | `AzureBlobStorage` — set `STORAGE_PROVIDER=azure_blob` |
| Identity | `get_current_user()` stub principal | Entra ID + Casbin |

See [app/config.py](app/config.py) for the secrets interface.
