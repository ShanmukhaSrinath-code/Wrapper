"""Regression tests for Fix 1 -- Celery tasks must be discovered, not listed.

The bug these pin down: ``celery_app`` carried a hardcoded
``include=["app.jobs.tasks"]``. A task that lived anywhere else was never
registered with the worker, yet the API happily returned ``201`` and a
``task_id`` for it. The job silently never ran.

Both halves are tested:

1. a task module dropped into ``app/services/`` is discovered, with no list
   anywhere for a developer to forget;
2. enqueuing a name nobody registered fails *before* a task id is handed out.
"""

from __future__ import annotations

import pathlib
import textwrap
from collections.abc import Iterator

import pytest

PLUGIN_PATH = pathlib.Path(__file__).resolve().parents[2] / "app" / "services" / "_probe_tasks.py"

PLUGIN_SOURCE = '''\
"""Throwaway plugin used by the task-discovery regression test."""

from app.jobs.celery_app import celery_app


@celery_app.task(name="probe.discovered")
def probe_task() -> str:
    return "ok"
'''


@pytest.fixture
def plugin_module() -> Iterator[str]:
    """Drop a task module into the plugin seam, then remove it."""
    PLUGIN_PATH.write_text(textwrap.dedent(PLUGIN_SOURCE), encoding="utf-8")
    try:
        yield "app.services._probe_tasks"
    finally:
        PLUGIN_PATH.unlink(missing_ok=True)


def test_task_in_services_package_is_discovered(plugin_module: str) -> None:
    """A task owned by a feature is registered simply by existing."""
    from app.core import discovery

    found = discovery.discover_task_modules()
    assert plugin_module in found, f"{plugin_module} was not auto-discovered; found={sorted(found)}"


def test_discovered_task_is_registered_with_celery(plugin_module: str) -> None:
    """Discovery actually imports the module, so Celery knows the task."""
    from app.core import discovery
    from app.jobs.celery_app import celery_app

    discovery.import_discovered_tasks()
    assert "probe.discovered" in celery_app.tasks


def test_enqueue_rejects_an_unregistered_task_name() -> None:
    """No task id is handed out for work that cannot run."""
    from app.jobs import UnknownTaskError, enqueue

    with pytest.raises(UnknownTaskError) as excinfo:
        enqueue("nobody.registered.this")

    assert "nobody.registered.this" in str(excinfo.value)


def test_enqueue_accepts_a_registered_task_name(plugin_module: str) -> None:
    """The guard rejects unknown names only -- it does not block real work."""
    from app.core import discovery
    from app.jobs import enqueue

    discovery.import_discovered_tasks()
    # `apply` runs the task locally and synchronously: this asserts the guard
    # lets a registered name through without needing a live broker.
    result = enqueue("probe.discovered", _local=True)
    assert result.get() == "ok"


def test_a_broken_plugin_fails_loudly_rather_than_silently() -> None:
    """A plugin that cannot import must abort startup, not vanish.

    The whole point of discovery is that nothing is silently missing. A module
    that raises on import has to be louder than a module that was never listed.
    """
    from app.core.discovery import PluginImportError, import_discovered_tasks

    broken = PLUGIN_PATH.with_name("_broken_probe.py")
    broken.write_text("raise RuntimeError('this plugin is broken')\n", encoding="utf-8")
    try:
        with pytest.raises(PluginImportError) as excinfo:
            import_discovered_tasks()
        assert "_broken_probe" in str(excinfo.value)
    finally:
        broken.unlink(missing_ok=True)
