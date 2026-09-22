---
name: import-poc
description: Port a standalone POC/prototype project (backend AND frontend) living in another folder onto this base, wholesale — backend logic rewritten through core seams, frontend built and served alongside it, plus three interactive architecture diagrams (the POC's own shape, the clean base, and the two merged). Use whenever asked to import, migrate, integrate, wire in, or "plug in" an external POC/prototype/target folder into this codebase.
---

# Importing a POC

The goal: point this skill at a POC folder and get back a working whole —
backend AND frontend, if the POC has one — built on top of this base, in one
pass. A user should not need to hand-assemble the pieces afterward. The port
is only finished once three architecture diagrams exist in
`docs/architecture/` (step 8) — they are part of the deliverable, exactly like
the migration or the tests, not optional documentation.

A POC's **backend** is a source of business logic only. Its own logging,
auth, error handling, DB session code, cache client, storage client, HTTP
client, Dockerfile, docker-compose, and observability all get **thrown
away** — this base already provides all of it. Porting is not copy-paste; it
is inventory → rewrite → verify.

A POC's **frontend**, if it has one, is ported too (see step 6) — copied in,
pointed at the newly-ported backend, built, and served from its own compose
service. It is not thrown away and not left for the user to wire up
separately.

Same law as `add-feature` for the backend: zero edits to `app/core/**`, zero
edits to `app/main.py`, zero edits to the `Makefile`. `deploy/docker-compose.yml`
gets exactly one kind of addition — a new `frontend-<feature>` service (step
6) — and nothing else. If the port seems to require any other edit to those
files, **stop and report it as a decision, do not make it silently.**

**Only stop for genuine infrastructure ambiguity** — a second database
engine, a competing broker/observability stack, or real auth semantics (see
the table below). Everything else — including the frontend — is a normal,
fully-automated part of the port. Do not pause for confirmation on steps that
have no ambiguity; run the whole pipeline (inventory → backend → migrate →
test → frontend → verify) and report what was done, not what you're about to
do.

## 0. Locate the POC and do the boundary scan FIRST

Before writing anything, read the POC's `requirements.txt`/`pyproject.toml`
and its settings/config module and check for infrastructure this base does
not already provide under a different name:

| Found in POC | What it means | Action |
|---|---|---|
| `redis`, `boto3`/`minio`, `httpx` used directly | just POC plumbing this base already has | discard, use core seam — not a decision |
| A **non-Postgres** DB driver (`pymongo`, `mysqlclient`, `asyncpg` pointed at a separate instance, raw `sqlite3`) | a second database engine | **STOP.** Report it. Do not add a second DB to `app/core/db` or `deploy/docker-compose.yml` without the user choosing that explicitly. Default recommendation: remodel the data as Postgres tables via `Base`, unless there's a hard reason (e.g. genuine document store need) — that's the user's call. |
| A **non-Celery** job runner, a **non-Redis** broker/cache, its own Grafana/Prometheus/Loki config | competing observability/infra | **STOP.** This base already ships Grafana/Loki/Prometheus/Tempo in `deploy/docker-compose.yml` regardless of what the POC has or lacks — never add a second copy. Report if the POC's dashboards/alerts contain logic worth preserving; otherwise they're dropped, not merged. |
| Its own auth (JWT issuing, session cookies, API keys) | a real auth mechanism | **STOP and ask** only if the POC has near-zero identity model (nothing to scope). Otherwise the default (no need to stop) is to port it **scoped to the feature package** — its own login/JWT/session code lives in `app/services/<feature>/security.py`, not `app/core/security/current_user.py` — exactly like a DB model or any other piece of business logic. This is not a base-wide auth change, so it does not cross the boundary. Only escalate if the user's request implies this auth should become the base's *real* auth for every feature. |
| A frontend folder (React/Vue/Svelte/static) | **not** out of scope — port it. See step 6. This is a normal, fully-automated step, not a boundary crossing. |
| WebSockets / SSE / any persistent-connection endpoint | this base has no pre-built seam for it (no sticky-session or pub/sub-to-client story) | **STOP.** Report it — building one is a real infra decision (which of several designs, whether it needs Redis pub/sub fan-out across workers), not something to bolt on silently inside a feature package. |
| File uploads / multipart forms | plumbing this base already has | route it through `app.core.storage.get_storage()` like `app/services/files.py` does — not a decision, but do not skip it: check the frontend's upload code sends `multipart/form-data` with the field name your endpoint expects. |

Anything not in this table (plain REST routes, SQLAlchemy/Pydantic models,
background jobs, simple outbound calls) is a normal port — proceed without
asking.

## 1. Inventory

List, per POC module: routes (path + method), DB models, background jobs,
outbound HTTP calls, and anything reading env vars directly. This becomes the
checklist for step 3 — nothing should be ported twice, nothing dropped
silently without saying so in the final report.

Also answer explicitly: **how does this POC get its first user/record?** If
there is no signup/create endpoint (common — many internal tools only have an
admin-managed user list), find its seed script (`seed.py`, a fixture, a SQL
file, a `--init` flag) and port it too (see step 7a). Skipping this produces
a fully-deployed app nobody can log into — a real, easy-to-miss gap, not a
theoretical one.

## 2. Group into feature packages

One POC domain concept → one `app/services/<feature>/` package (not one
package per POC file). Follow the layout from the `add-feature` skill:

```
app/services/<feature>/
  __init__.py
  models.py
  router.py
  tasks.py
  service.py     # optional
```

If the POC is one flat `main.py`, split it along its actual domain nouns —
don't create `app/services/poc/` as a dumping ground.

### 2a. If a frontend is being ported too: the wire contract is part of the POC, not an implementation detail

This is the single most common way a "successfully ported" backend still breaks
the ported UI, and it applies to **every** POC with a frontend, not a
particular one. Two separate traps, both caused by the same mistake — treating
JSON field names/error shapes as free to "clean up" during translation:

1. **Field names and casing.** A POC's frontend is copied in **unmodified**
   (step 6) except for its API base URL. It still sends/expects whatever
   field names and casing the original backend used for each field — and
   real POCs are often *inconsistent* about this (`teamId` on one endpoint,
   `manager_id` on another, in the very same file). Do not rename fields to
   a "cleaner" or more idiomatic convention while porting schemas. If a
   Python-idiomatic internal name is wanted, keep the original wire name via
   `Field(alias="teamId")` + `model_config = ConfigDict(populate_by_name=True)`
   — router code keeps using the snake_case attribute; the JSON on the wire
   stays byte-for-byte what the frontend already sends and expects.
   **Verify by grep, don't guess:** search the frontend source for every API
   call's request body keys and every response field access
   (`grep -rnE "\.[a-z]+[A-Z][a-zA-Z]*\b" src`) and cross-check each one
   against the ported schema before considering the port done.
2. **Error response shape.** This base's error envelope is fixed and
   deliberate (`{"error": "<code>", "message": "<human text>", "detail":
   ..., "request_id", "trace_id"}`) — never bend it per feature, that would
   break the correlation guarantee every other feature relies on. But a
   POC's frontend almost always expects **its own original backend's** error
   shape (e.g. `{"error": "<human message>"}`, or a bespoke nested
   `details` object for one specific error case). These will not match.
   Grep the frontend for every place it reads an error response
   (`grep -rn "\.data\?\.error\|\.data\.error\|catch (err" src`) and fix
   each call site to read this base's actual fields (`data.message` for the
   human text a user should see; `data.detail` for any structured extra
   payload a POC endpoint used to attach, e.g. suggested alternatives on a
   409). This is a legitimate, expected frontend edit — the base's error
   envelope is a fixed seam, not something to match per POC.
3. **Primary key type changes.** `add-feature`'s convention is a UUID
   primary key (`UUIDPrimaryKeyMixin`); many POCs use auto-incrementing
   integers instead. If you change a model's ID type during the port (a
   reasonable default, not something to avoid), the frontend almost
   certainly has code that coerces that ID to a number before sending it —
   `Number(id)`, `parseInt(id)`, `+id` — every one of these turns a UUID
   string into `NaN`, which then serializes to JSON `null` and fails
   validation with a generic, unhelpful `422`. This is easy to miss because
   it only breaks at the specific call sites that do numeric coercion, not
   at every use of the ID — a page that just displays or round-trips an ID
   as a string keeps working, one that runs it through `Number(...)` before
   a write silently breaks. Grep for it explicitly:
   `grep -rn "Number(\|parseInt(" src` — then check each hit: coercion
   applied to a genuinely-numeric field (a quantity, a group number) is
   fine; coercion applied to anything that is or contains an id/`_id`/`Id`
   is not, and the `Number(...)`/`parseInt(...)` wrapper must simply be
   removed so the id stays a string end to end.
4. **Enum / status-string values.** A frontend often hardcodes the exact
   strings a backend returns — `row.status === "active"`,
   `role === "admin"`, a CSS class keyed off `type` — usually lowercase
   because Python enums and Postgres check constraints are usually
   lowercase, but verify rather than assume: some POCs stored
   `"Active"`/`"ADMIN"` instead. If a ported model changes a string enum's
   case or spelling, every frontend comparison against the old literal
   silently stops matching (no crash, no error — a filter/badge/permission
   check just never fires). Grep the frontend for the literal values your
   model actually stores (`grep -rn '"active"\|"admin"\|"fixed"' src`,
   substituting your feature's actual enum values) and confirm each one
   still matches.
5. **Dates and timezones.** This base's `created_at`/`updated_at`-style
   columns are `timezone=True` and server-generated in UTC; a POC's
   business-logic date comparisons (`date.today()`, "is this in the past")
   run in whatever timezone the container's clock is in, while the
   browser's `new Date()` runs in the user's local timezone. If a POC has
   any "can't book a day in the past" / "expires after N days" logic near a
   date boundary, check whether the original used local or UTC dates before
   assuming either is fine — porting the comparison unchanged is correct
   only if the timezone assumption transfers unchanged too.

Skipping this step produces exactly the failure mode that prompted this
section: an endpoint that returns a perfectly correct `422`/`409`, an admin
that clicks a button and sees the literal string `"validation_error"` on
screen instead of a real message, and a feature that looks broken when the
backend logic was actually right all along.

## 3. Mechanical rewrite table

Apply every one of these while porting; do not leave a POC-native version
"for now":

| POC pattern | Replace with |
|---|---|
| `logging.getLogger(__name__)` / `print()` | `from app.core.logging import get_logger` |
| `raise HTTPException(404, ...)` (or any hand-rolled error JSON) | `app.core.errors.NotFoundError` / `ConflictError` / etc. |
| its own `redis.Redis(...)` client | `from app.core import cache` |
| `boto3.client("s3", ...)` or local disk writes | `from app.core.storage import get_storage` |
| `requests`/raw `httpx.Client()` calls | `from app.core.http import request` |
| its own Celery app, or a different task queue (RQ, arq, plain threads) | `app.core.jobs.celery_app` + `enqueue()` |
| no identity, or a bespoke `get_current_user` | `user: CurrentUser` on every route that needs one |
| manual `session.commit()`/`db.close()` | `DbSession` dependency — it commits/rolls back for you |
| its own request-id / correlation-id middleware | delete outright — `CorrelationMiddleware` already covers it |
| its own CORS/security-header middleware | delete — already in `app/core/middleware/security.py` |
| its own rate limiter | delete — already in `app/core/middleware/ratelimit.py`, tune via `RATE_LIMIT_*` env vars if the POC needs different limits |
| `pydantic.BaseSettings` reading its own env vars | fold feature-specific env vars into `app/core/config.py`'s `Settings` only if truly infra-level; otherwise keep them local constants in the feature package — do not create a second settings object |
| its own Dockerfile / requirements.txt | discarded — the feature ships inside the existing `app` image; add any *new* third-party dependency to this repo's `pyproject.toml`, **then run `uv lock`** — editing `pyproject.toml` alone leaves `uv.lock` stale, and the Docker build installs from the lock, not the pyproject file. A stale lock means the container silently ships without the new dependency and fails at import time, not at build time. |

## 4. Models and migration

Point every model at `app.core.db.Base`. Then:

```bash
make revision m="import <feature> from POC"
make migrate
```

Read the generated migration. If it wants to `DROP` something, that almost
always means a model failed to import (check for a typo'd import path), not
that a real drop is warranted — do not set `ALLOW_DESTRUCTIVE=1` reflexively.

## 5. Tests

Port the POC's test intent, not its test code verbatim — its fixtures assume
its own app instance. Use `app_client` from `tests/conftest.py`:

```python
@pytest.mark.asyncio
async def test_<behavior>(app_client):
    response = await app_client.post("/<feature>", json={...})
    assert response.status_code == 201
```

Put unit tests in `tests/unit/test_<feature>.py`. If the POC's test asserted
against its own logging/error format, drop that assertion — the shape is now
whatever `app/core/errors.py` emits.

## 6. Frontend, if the POC has one

Copy it in, point it at the newly-ported backend, and give it its own
container — same-origin via a reverse proxy, so there is no CORS to
configure.

```
frontend/<feature>/          # copied from the POC's frontend folder
  (framework files as-is: package.json, src/, index.html, vite/webpack config, ...)

services/frontend-<feature>/
  Dockerfile                 # multi-stage: build the static bundle, serve with nginx
  nginx.conf                 # serves the bundle; reverse-proxies /api/ -> http://app:8000/api/
```

Steps:

1. Copy the POC's frontend source (not `node_modules`, not its own lockfile's
   resolved binaries) into `frontend/<feature>/`.
2. Point its API client at this feature's namespaced prefix. Every ported
   backend lives under `/api/<feature>/...` (step 2's router prefixes), so if
   the POC's frontend called a bare `/api/...`, change its base URL/axios
   instance to `/api/<feature>` — a one-line change, not a rewrite of every
   call site. **Then do step 2a** — the base URL is the only frontend change
   that's "free"; every field-name and error-shape mismatch in the same
   frontend must be checked and fixed the same way.
3. Add `services/frontend-<feature>/Dockerfile`: a Node build stage (`npm
   install && npm run build`) feeding an `nginx:alpine` runtime stage. Never
   add a Node stage to `services/app/Dockerfile` — the frontend is a separate
   image, always.
4. Add `services/frontend-<feature>/nginx.conf`: serve the built static
   files with SPA fallback (`try_files $uri /index.html`), and
   `location /api/ { proxy_pass http://app:8000/api/; }`. This makes the
   browser's origin the frontend container's own port — no
   `CORS_ALLOW_ORIGINS` change needed, because the browser never talks to the
   `app` origin directly.
5. Add exactly one new service to `deploy/docker-compose.yml`, named
   `frontend-<feature>`, `depends_on: app` (`condition: service_healthy`),
   building from the Dockerfile above. This is the one permitted
   `docker-compose.yml` addition per feature — do not add anything else to
   that file.
6. Build and smoke-test it for real before reporting success:
   `docker compose -f deploy/docker-compose.yml build frontend-<feature>`,
   bring it up, `curl` the static root (expect 200) and a proxied API call
   through it (expect the same response the backend gives directly). A
   frontend that builds but was never actually curled through its proxy is
   not verified. This one call proves the proxy and CORS story, nothing
   more — it does **not** prove field names line up (step 2a) or that error
   messages render as real text instead of a bare code. Those need the
   multi-endpoint pass in step 7b, using request bodies shaped exactly like
   the frontend's own source (copy the JSON keys from the `.tsx`/`.ts` file,
   don't reconstruct them from memory).

## 7. Verify, then report

`make lint` / `make typecheck` / `make test` run against the **local venv**,
not the container. Passing them proves the code is correct; it does **not**
prove the deployed app works, because the running `app`/`worker` containers
are built from whatever image existed *before* this port started. This
distinction caused real, user-visible bugs (`404 Not Found` on every ported
route) in earlier runs of this skill — do not skip 7b believing 7a already
covered it.

**7a. Local checks:**

```bash
make lint
make typecheck
make test
git status --porcelain
```

**7b. Rebuild and smoke-test the real containers — mandatory, not optional:**

```bash
docker compose -f deploy/docker-compose.yml build app worker
docker compose -f deploy/docker-compose.yml up -d --force-recreate app worker
```

Then `curl` the actual running container and read the real response bodies.
"The image built" and "`make test` passed" are both insufficient — a route
only counts as working once you have seen it return the right thing from the
container that will actually run.

**If no frontend was ported**, one read and one write endpoint per router is
enough — there is no UI contract to protect.

**If a frontend was ported (step 6), sampling is not enough — enumerate and
test every single mutating call the frontend makes, exhaustively, not "one
per router."** This is not optional rigor; it is the specific gap that let
two working-in-isolation bugs (shuffle and swap, in an earlier run of this
skill) ship past a check that only sampled "one write per router" and missed
the two mutations that weren't the sampled one. Concretely:

1. `grep -rn "api\.\(get\|post\|put\|patch\|delete\)\(" <frontend dir>` and
   list every distinct call site. Every `post`/`put`/`patch`/`delete` call
   goes on the checklist; every `get` call related to one of them (does the
   write actually show up on the next read?) goes on it too.
2. For each one, open its `.tsx`/`.ts` file and copy the **exact request
   body keys and value types** it sends — including how it derives each
   value (`Number(x)`? `x` as-is? a nested object?). Do not reconstruct the
   payload from your own schema; that only proves the schema is internally
   consistent, not that this specific frontend code can talk to it.
3. `curl` each one against the real running container with that exact
   payload and confirm the real response — not just the status code, read
   the body, and if the frontend reads specific fields back out of it
   (check the `.then`/destructuring at the call site), confirm those fields
   are actually present under the names the frontend expects.
4. Track this as a literal checklist in your own working notes while you do
   it (call site → tested → result) and only report the port complete when
   every row is checked. A port that verified 3 of a frontend's 11 mutating
   endpoints is not "verified, with an acceptable sample" — it is 8
   endpoints of unknown status, and must be reported that way if you run out
   of time to finish the sweep.

**7c. Seed data, if step 1 found the POC has no signup flow:**

Run the ported seed script once against the running stack
(`python -m app.services.<feature>.seed` or equivalent) and confirm a login
actually succeeds afterward. Report the created credentials (they are
dev/demo accounts, safe to state plainly) so the user can log in immediately
without guessing.

**7d. Test-data cleanup note:** this repo's integration tests run against the
same Postgres the dev stack (and now the user, manually, through the
frontend) uses — there is no separate test database. If your step 5 tests
created teams/users/etc. with generated names, say so in the report and give
the user the cleanup command (`TRUNCATE ... RESTART IDENTITY CASCADE` on the
feature's tables, then re-run the seed script) rather than leaving test
debris silently mixed into what looks like real data.

**If you (or the user) truncate/reseed a user table after anyone has already
logged in**, say so explicitly and tell them to log out and back in. A JWT
issued before the reset still decodes and passes signature checks — it just
names a `user_id` that no longer exists — so the failure surfaces later, on
some unrelated write, as a confusing foreign-key/database error instead of a
clean "please log in again." This is not a porting bug and not something to
silently work around; it is a direct, foreseeable consequence of resetting
identity data out from under a live session, and the fix is always the same
(re-authenticate), so just say it up front.

**7e. Rate-limit budget vs. frontend polling.** This base's rate limiter is
global and per-client across *all* routes (`RATE_LIMIT_REQUESTS` per
`RATE_LIMIT_WINDOW_SECONDS`), not per-route like some POCs' own limiters. A
frontend that polls several endpoints on an interval (react-query
`refetchInterval`, a dashboard that refreshes every few seconds) can rack up
requests fast enough to hit `429`s that never happened against the
original POC. Check the frontend for `refetchInterval`/`setInterval`-driven
API calls and do the arithmetic against the configured limit before calling
the port done; if it's close, that's worth a note in the report even if it
doesn't fail today.

The diff should be exactly: the new `app/services/<feature>/` package(s), a
migration file, tests, `frontend/<feature>/`, `services/frontend-<feature>/`,
and the one new service block in `deploy/docker-compose.yml` (plus
`uv.lock` if step 3 added a dependency). Anything else — especially inside
`app/core/**`, `app/main.py`, or `Makefile`, or a second `docker-compose.yml`
service beyond `frontend-<feature>` — means a boundary was crossed; stop and
call it out rather than committing it.

Finish with a short report, always including:

1. **Ported automatically** — routes/models/tasks/frontend that moved over
   cleanly, plus how you actually verified them: the 7b container curl
   results and the 6.6 frontend curl results, not "tests passed" or "it
   built".
2. **Dropped by design** — POC plumbing that this base already provides for
   free (its logging, correlation, CORS, rate limiting, etc.) — say what it
   was, so nothing looks silently missing.
3. **How to log in** — the seeded credentials from 7c, or a note that the
   POC has a real signup flow and none was needed.
4. **Test-data cleanup**, if 7d applies — and a reminder to re-auth if any
   existing session's user data was touched.
5. **Rate-limit headroom**, if 7e found the frontend polls aggressively.
6. **Needs your decision** — only genuine infra ambiguity from step 0 (a
   second DB engine, a competing broker/observability stack, or auth that
   implies a base-wide change). A frontend existing is not one of these
   anymore — it should already be built and running by the time you report.

## 8. Generate the three architecture artifacts

Every import produces three interactive HTML diagrams in `docs/architecture/`,
built from the templates in `.claude/skills/import-poc/assets/`. They are part
of the deliverable, not optional documentation — generate them every time,
not just when asked.

All three share one design system (same CSS, same click-to-open-a-side-panel
JS engine) so a reader learns the interaction once. Copy an existing generated
page as your starting point rather than writing from scratch — `docs/architecture/talentiq-integrated.html`
is the reference example.

**Page 1 — `docs/architecture/common-app-base.html`.** A straight copy of
`assets/architecture-base.template.html`, unmodified. This is the base with
`CORE_SERVICES` deliberately empty — regenerate it only if the base itself
changed; otherwise just make sure the file exists.

**Page 2 — `docs/architecture/<feature>-poc-original.html`.** Diagrams the
POC as it existed *before* porting — its own frontend, its own backend
framework, its own database access, its own (usually missing) auth/logging/
observability. Build this from `assets/architecture-poc.template.html`, filled
in from your **step 0/1 findings** — the boundary scan and inventory you
already did. Do not invent content: every `what`/`how`/`note` string must
trace back to a real file and real code you actually read. Mark any row under
"Operational maturity" as `class="blk missing"` when the POC genuinely has
none of it (most POCs have no auth, no structured logging, no observability at
all — say so plainly, that gap is exactly what porting fixes).

**Page 3 — `docs/architecture/<feature>-integrated.html`.** The real payoff.
Clone `assets/architecture-base.template.html` (or `talentiq-integrated.html`
as a worked example) and, for **every block that is even remotely touched**,
add a `used` property rendered as a highlighted "In this application" panel
section — not just the Core Logic block. Concretely:

- `api` — every real route this feature mounted (path + method), not "see the
  router file."
- `db` — every real table name, and why the PK types were chosen (e.g. kept
  as integers because the frontend does `Number(id)` somewhere — check step
  2a's findings for this).
- `cache` / `storage` / `breaker` — if the feature genuinely doesn't use one
  of these, **say so explicitly** (`unused: true`, a grey "not used" note, not
  silence). A reader should be able to tell what a feature *doesn't* touch as
  easily as what it does.
- `external` — name the actual third-party service and library (e.g. "Groq
  via the OpenAI SDK, not through `app.core.http`" — and say why, if the
  reason is a real one like "a typed SDK with its own retry semantics").
- `queue`/`worker` — the actual registered task name(s).
- `audit` — the actual event-name strings the feature writes
  (`write_audit("thing.created", ...)`), not a generic description.
- Observability blocks (`structlog`/`promtail`/`prometheus`/`tempo`/`grafana`/
  `sentry`) — usually just "inherited automatically, no feature code needed,"
  which is itself worth stating rather than leaving blank.
- `docker` — the exact new service names added, if any.
- `tests` — the real test file name and what it actually covers.

**If the port added a capability the base genuinely didn't have before**
(a frontend, a periodic scheduler, anything from step 0's stop-and-ask table
that got a yes) **add it as a new block**, not a footnote inside an existing
one — give it `kind: '... (new)'` so it renders with the green "NEW" badge,
same as `frontend` and `beat` do in the TalentIQ example. That badge is the
visual answer to "what did this port actually add to the base's capabilities,"
which is usually the single most useful thing in the diagram for a reviewer
who already knows the base.

Verify all three render before reporting done: extract the `<script>` block
and run `node --check` on it (see the worked example's verification — this
catches template-literal/quote escaping mistakes, the most common way these
pages silently break). Then actually open each one and click through at least
the new/changed blocks.

## Anti-patterns to refuse

| Tempting | Do this instead |
|---|---|
| copy the POC's `docker-compose.yml` services in | nothing — this base's `deploy/docker-compose.yml` already has Postgres/Redis/MinIO/Grafana/Loki/Prometheus; a POC's copies are redundant or conflicting |
| add a Mongo/MySQL client because the POC had one | stop and ask — remodeling onto Postgres is the default, adding a second DB is the user's call |
| keep the POC's own `/healthz` or logging middleware "just in case" | delete it — `app/core/api/health.py` and `CorrelationMiddleware` already exist |
| leave the frontend out / hand it back to the user | port it — see step 6. Only a genuinely infra-level ambiguity (step 0's table) is the user's call, not "does a frontend exist" |
| add a Node build stage to `services/app/Dockerfile` | put it in its own `services/frontend-<feature>/Dockerfile` instead — the frontend is always a separate image |
| open the `app` origin to the browser and configure `CORS_ALLOW_ORIGINS` for the frontend | reverse-proxy `/api/` from the frontend's own nginx instead — same-origin, no CORS needed |
| report the port done because the frontend image built | not verified until you've actually curled the static root and a proxied API call and seen real responses |
| report the backend port done because `make test` passed | rebuild and restart the real `app`/`worker` containers (step 7b) and curl them too — `make test` runs against the local venv, which can be ahead of what the containers are actually serving |
| edit `pyproject.toml` and move on | run `uv lock` immediately after — an unregenerated lock file means the Docker build silently keeps the old dependency set |
| leave "how does anyone log in" unanswered | if step 1 found no signup flow, port the seed script, run it, and hand the user real credentials in the report |
| rename a POC's JSON field names/casing to a "cleaner" convention while porting schemas | keep the exact original wire name via `Field(alias=...)` (step 2a) — the frontend you're also porting was written against the original names and is not being touched to match your new ones |
| assume the frontend's error-handling code matches this base's error shape because the POC also returned JSON on error | grep the frontend for every `.data.error`/`.data.details`-style read (step 2a) and point each one at this base's actual `message`/`detail` fields — the two shapes are never the same by default |
| verify a ported write-endpoint with request bodies built from the new Python schema | build them from the frontend's own source instead (step 7b) — a payload you invented yourself can't catch a field-name mismatch with the very UI it's supposed to serve |
| assume a frontend's `Number(id)`/status-string comparisons still work after changing a model's ID type or enum casing | grep for both (step 2a, points 3–4) — a coercion or literal-string comparison that quietly stops matching produces no error, just a feature that silently does nothing |
| build a WebSocket/SSE feature by improvising something inside the feature package | stop and ask (step 0) — this base has no real-time seam, and inventing one silently is an infra decision in disguise |
| reset/truncate a user table without telling the user to re-authenticate | say so immediately (step 7d) — an old JWT will keep decoding successfully and fail later, confusingly, on an unrelated write |
| silently skip a piece that didn't map cleanly | say so explicitly in the final report |
| skip step 8's architecture artifacts because the port already "feels done" | generate all three — they're part of the deliverable, not optional polish |
| only fill in the Core Logic block on the integrated page | every touched block gets an "In this application" section — `db`, `external`, `audit`, `queue` especially |
| leave a block's real usage unstated when the feature doesn't use it | mark it `unused: true` with a grey note saying so explicitly — silence there reads as "forgot to check," not "not applicable" |
| ship an architecture HTML page without checking it renders | `node --check` the extracted `<script>` block before reporting done — a stray unescaped quote in a content string breaks the whole page silently |
