"""Support tickets -- a POC feature built entirely through the plugin seam.

This package exists to prove a claim the base makes: a real feature needs
**zero** edits to `app/core/**` and **zero** edits to `app/main.py`. Everything
it uses -- the DB session, the cache, the object store, the queue, audit,
errors, correlation -- is imported from `app.core` and nothing is reached into.

What each module is for:

* `models.py`   -- the `ticket` table. Discovery imports it, so Alembic sees it.
* `schemas.py`  -- request/response shapes, kept apart from the ORM model.
* `service.py`  -- the logic worth testing without an HTTP layer.
* `router.py`   -- the endpoints. Auto-mounted by `discover_routers()`.
* `tasks.py`    -- background work. Auto-registered with the worker.
"""
