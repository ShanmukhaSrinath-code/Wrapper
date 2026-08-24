# redis — cache and Celery broker

| | |
|---|---|
| Image | `redis:7-alpine` |
| Port | 6379 |
| Used by | app (cache), app + worker (Celery broker and result backend) |
| Config | none — command-line flags in `deploy/docker-compose.yml` |

No config file: the defaults are correct for a local stack, and inventing one
would be a file to keep in step with nothing.

**Losing Redis degrades, it does not break.** Cache reads and writes catch
connection and timeout errors, log `cache.unavailable`, and treat the outage as
a miss, so routes fall through to their origin and keep returning 200.
Readiness still reports Redis down, so traffic drains.
