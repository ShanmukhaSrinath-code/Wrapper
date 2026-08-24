# postgres — the database

| | |
|---|---|
| Image | `postgres:16-alpine` |
| Port | 5432 |
| Used by | app, worker, Alembic |
| Config | environment (in `deploy/docker-compose.yml`) + `init/` |

## Two roles, on purpose

| Role | Used by | Rights |
|---|---|---|
| `appuser` | Alembic migrations | owns the schema |
| `appruntime` | the application and worker | CRUD on business tables; **INSERT + SELECT only** on `audit_log`; no ownership; no rights on `alembic_version` |

The split is what makes the append-only audit log real. When the app connected
as the owner it could simply `DROP TRIGGER` and then rewrite or erase the trail.
Now two independent layers stop it: the grant refuses before the trigger is
reached, and the trigger refuses even for the owner.

`init/01-create-runtime-role.sql` runs once on a brand-new data directory and
only creates the role. **Grants live in the Alembic migration**
(`migrations/versions/*_harden_audit_immutability.py`), because they are
per-table and must stay in step with the schema — and because a managed
production Postgres never runs the init script at all. The migration creates the
role idempotently too, so every environment converges on the same state.
