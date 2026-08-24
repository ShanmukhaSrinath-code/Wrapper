"""Background jobs (Celery).

Prefer :func:`enqueue` over ``send_task``. It refuses to publish a task nobody
has registered, so a typo or a missing plugin surfaces as a 500 at the call site
instead of a ``201 Created`` for work that will never run.
"""

from __future__ import annotations

from typing import Any

from celery.result import EagerResult

from app.jobs.celery_app import celery_app
from app.logging import get_logger

log = get_logger(__name__)

__all__ = ["UnknownTaskError", "celery_app", "enqueue"]


class UnknownTaskError(RuntimeError):
    """Asked to enqueue a task name that is not registered.

    Registration is shared: the API process and the worker both build their task
    registry from the same discovery pass, so if this process does not know the
    name, the worker will not either.
    """


def enqueue(name: str, *args: Any, _local: bool = False, **kwargs: Any) -> Any:
    """Publish ``name`` to the queue, having first checked it exists.

    ``_local=True`` runs the task inline and synchronously; it exists so tests
    can exercise the guard without a live broker.
    """
    task = celery_app.tasks.get(name)
    if task is None:
        known = sorted(n for n in celery_app.tasks if not n.startswith("celery."))
        log.error("task.unregistered", task_name=name, registered=known)
        raise UnknownTaskError(
            f"Task {name!r} is not registered, so it would never run. "
            f"Registered tasks: {known}. If this is a new feature, make sure its "
            f"module is importable under one of PLUGIN_PACKAGES."
        )

    if _local:
        result: EagerResult = task.apply(args=args, kwargs=kwargs)
        return result
    return task.delay(*args, **kwargs)
