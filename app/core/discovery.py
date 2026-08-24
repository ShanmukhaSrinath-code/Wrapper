"""Dynamic discovery of feature plugins.

Two things used to be hand-maintained lists, and both failed silently when a
developer forgot to update them:

* ``celery_app(include=[...])`` -- a task outside the list was never registered,
  yet the API still handed back a task id for it;
* ``app/db/models/__init__.py`` -- a model missing from it was invisible to
  Alembic, so the next autogenerate proposed dropping its table.

This module replaces both with discovery. Anything importable under the
configured plugin packages is found by walking the package tree, so a feature
owns its tasks and models simply by existing.

**Discovery fails loudly.** A plugin module that raises on import aborts startup
here, rather than surfacing later as a missing task or a phantom migration.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)


class PluginImportError(RuntimeError):
    """A discovered module could not be imported.

    Raised at startup, deliberately fatal: a half-loaded plugin set is worse
    than a stopped process, because the damage shows up later and elsewhere.
    """


def _walk_package(package_name: str) -> list[str]:
    """Return every importable module under ``package_name``, recursively.

    A missing package is not an error -- ``app.services`` may legitimately be
    empty in a fresh clone -- but a package that exists and fails to import is.
    """
    try:
        package: ModuleType = importlib.import_module(package_name)
    except ModuleNotFoundError:
        log.debug("discovery.package_absent", package=package_name)
        return []
    except Exception as exc:  # pragma: no cover - defensive
        raise PluginImportError(f"Plugin package {package_name!r} failed to import: {exc}") from exc

    # A plain module rather than a package: it is itself the only thing to load.
    if not hasattr(package, "__path__"):
        return [package_name]

    def _onerror(failed: str) -> None:
        # pkgutil swallows ImportError by default. That default is exactly the
        # silent-failure behaviour this module exists to remove.
        raise PluginImportError(f"Plugin module {failed!r} failed to import during discovery.")

    found: list[str] = []
    for info in pkgutil.walk_packages(package.__path__, prefix=f"{package_name}.", onerror=_onerror):
        leaf = info.name.rpartition(".")[2]
        if leaf.startswith("__"):
            continue
        found.append(info.name)
    return sorted(found)


def _discover(package_names: list[str]) -> list[str]:
    found: list[str] = []
    for name in package_names:
        found.extend(_walk_package(name))
    return sorted(set(found))


def _import_all(module_names: list[str], *, what: str) -> list[str]:
    for name in module_names:
        try:
            importlib.import_module(name)
        except PluginImportError:
            raise
        except Exception as exc:
            raise PluginImportError(f"{what} module {name!r} failed to import: {exc}") from exc
    log.info(f"discovery.{what}_loaded", count=len(module_names), modules=module_names)
    return module_names


# --- tasks -------------------------------------------------------------------


def discover_task_modules() -> list[str]:
    """Every module that may define Celery tasks."""
    return _discover(settings.plugin_packages_list + settings.task_packages_list)


def import_discovered_tasks() -> list[str]:
    """Import task modules so their ``@celery_app.task`` decorators run.

    Called by both the API process and the worker, from the same configuration,
    so the two cannot disagree about which tasks exist.
    """
    return _import_all(discover_task_modules(), what="tasks")


# --- models ------------------------------------------------------------------


def discover_model_modules() -> list[str]:
    """Every module that may define SQLAlchemy models."""
    return _discover(settings.plugin_packages_list + settings.model_packages_list)


def import_discovered_models() -> list[str]:
    """Import model modules so they attach themselves to ``Base.metadata``.

    Alembic calls this before comparing metadata to the database, which is what
    makes a forgotten registration impossible rather than merely discouraged.
    """
    return _import_all(discover_model_modules(), what="models")
