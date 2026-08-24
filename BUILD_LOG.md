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

