"""The plugin seam -- **this is where business logic goes**.

Anything importable under this package is auto-discovered:

* Celery tasks are registered with the worker (see ``app.core.discovery``).
* SQLAlchemy models are added to ``Base.metadata`` so Alembic always sees them.

Adding a feature therefore never requires editing infrastructure. Drop a module
or a package in here, include its router in ``app/main.py``, and everything else
-- correlation, logging, tracing, metrics, audit, errors -- applies to it for
free.

Nothing in this package may be imported by ``app.core``: infrastructure must not
depend on business logic. That direction is enforced by an import-linter
contract.
"""

from __future__ import annotations
