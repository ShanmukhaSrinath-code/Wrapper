# app — the FastAPI service

The only service that contains business logic, and the one you extend.

| | |
|---|---|
| Image | built from `Dockerfile` in this folder |
| Port | 8000 |
| Depends on | postgres, redis, minio |
| Config | environment only (`app/core/config.py`, `.env`) |

The worker uses this same image with a different command — see `../worker/`.

**Where code goes:** `app/core/**` is infrastructure and should not be edited to
add a feature. `app/services/**` is the plugin seam: routers, tasks and models
dropped there are discovered automatically. See `../../ARCHITECTURE.md`.
