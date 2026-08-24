"""The base's own tables.

**There is deliberately no registry here.** Models used to have to be imported
into this file by hand so Alembic could see them; a model that was missed still
worked at runtime, and the next autogenerate proposed dropping its table.

Model modules are now discovered instead -- see ``app.core.discovery``, which
walks ``MODEL_PACKAGES`` and the plugin packages before Alembic reads
``Base.metadata``. Adding a module to this directory, or to a feature package
under ``app/services/``, is all that is required.
"""

from __future__ import annotations
