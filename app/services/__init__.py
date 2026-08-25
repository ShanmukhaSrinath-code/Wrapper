"""The plugin seam -- **this is where business logic goes**.

Anything importable under this package is auto-discovered:

* a module-level ``router`` is mounted on the app -- **no edit to**
  ``app/main.py`` (see ``app.core.discovery.discover_routers``);
* Celery tasks are registered with the worker;
* SQLAlchemy models are added to ``Base.metadata`` so Alembic always sees them.

Adding a feature therefore never requires editing infrastructure. Drop a module
or a package in here and everything else -- correlation, logging, tracing,
metrics, audit, errors -- applies to it for free.

The one step discovery cannot do for you is create the table: a new model still
needs ``make revision m="..."`` and ``make migrate``. Discovery makes the model
*visible* to Alembic; it does not touch your database.

Nothing in this package may be imported by ``app.core``: infrastructure must not
depend on business logic. That direction is enforced by an import-linter
contract.
"""

from __future__ import annotations
