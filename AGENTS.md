# Agent instructions

The rules for working in this repo live in **[CLAUDE.md](CLAUDE.md)**. Read that
file first, whichever assistant you are.

One-paragraph version: this is a reusable FastAPI base. Business logic goes in
`app/services/` and is auto-discovered (routers, Celery tasks, SQLAlchemy
models). `app/core/**` is infrastructure and is **not edited to add a feature** —
an import-linter contract fails the build if it depends on business logic. A new
model still needs `make revision m="..."` and `make migrate`. Verify with
`make lint && make test`.

Claude Code users also get task-specific skills in `.claude/skills/`:
`add-feature`, `verify-base`, `stack-doctor`.
