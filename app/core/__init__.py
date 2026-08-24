"""Infrastructure core -- **do not edit this package to add a feature**.

Everything under ``app.core`` is the reusable base: configuration, logging,
correlation, observability, error handling, database, cache, storage, audit and
job plumbing. It knows nothing about any particular business domain, and it must
never import from ``app.services``.

Business logic belongs in ``app.services`` (the plugin seam), which is
auto-discovered. See ``ARCHITECTURE.md``.
"""

from __future__ import annotations
