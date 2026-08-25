---
name: verify-base
description: Run every gate this repo ships (lint, boundary contract, types, tests with coverage, stack health, correlation smoke, security scan) and report honestly what passed and what failed. Use when asked to verify, validate, check, or prove the base or a change is sound, or before declaring work done.
---

# Verifying the base

Run these in order. **Report the real output.** A gate you skipped is not a gate
that passed — say which ones you ran and which you could not.

## 1. Static gates (no stack needed)

```bash
make lint        # ruff check + ruff format --check + lint-imports
make typecheck   # mypy app
```

`lint-imports` is the important one: it enforces that `app.core` never imports
`app.services`. Expect `Contracts: 2 kept, 0 broken.`

## 2. Tests

```bash
make up          # integration/e2e tests skip without it
make migrate
make test        # 80% coverage gate
```

Two things to check in the output, not just the exit code:

- **the pass count** — if it dropped, something was deleted, not fixed;
- **skips** — integration and e2e tests skip with a reason when the stack is
  down. A suite that is "green" because two thirds of it skipped is not green.
  Report the skip count.

## 3. Stack health

```bash
docker compose -f deploy/docker-compose.yml ps
curl -s localhost:8000/health/ready
```

All ten services healthy; readiness `200` with every check `ok`.

## 4. Correlation smoke

```bash
make smoke
```

This is the one command that proves the blocks are actually wired to each other:
one `request_id` is followed into Loki, Tempo, the audit table and a Prometheus
counter. Expect `SMOKE PASSED`.

It reports honestly when a leg is unavailable — `SENTRY_DSN is not set`, and
`trace not found in Tempo` when trace export is off (both are normal locally).
Do not present those as failures, and do not present them as passes either.

## 5. Security scan

```bash
make scan        # trivy fs + trivy image
```

Requires `trivy` on PATH and `make build` to have run for the image half.

## Optional: prove the seam still holds

The claim this base makes is that a feature needs zero infrastructure edits. To
re-verify it, add a throwaway feature under `app/services/` with a router, a
model and a task, exercise it, then:

```bash
git status --porcelain    # only app/services/<probe>/, its migration, its tests
```

Then delete it, `alembic downgrade -1`, and confirm `git status` is empty and the
suite is still green.

## Optional: prove the deploy guard

```bash
docker run --rm -e ENVIRONMENT=prod --entrypoint python common-app-base:local -c "import app.main"
# must exit non-zero, naming every credential still on its shipped default
```

## Reporting rules

- Paste actual output for anything you claim.
- If a gate fails, say so plainly with the output — do not summarise a failure
  as "mostly working".
- Distinguish **"passed"** from **"not asserted"**. Sentry and Tempo legs are
  routinely not asserted locally; that is a limit of the environment, not a pass.
- Never report a coverage number or pass count you did not just observe.
