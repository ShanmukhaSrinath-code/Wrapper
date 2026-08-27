"""Celery application and correlation propagation.

The hard part of a job system is not running work off-request -- it is not
losing the thread when you do. A task enqueued by request X must log under
request X, otherwise the moment work goes async your correlation story stops.

So this module:

1. attaches the current `request_id`/`trace_id` to every message as a **header**
   when a task is published (`before_task_publish`), and
2. binds those ids into structlog inside the worker before the task body runs
   (`task_prerun`), clearing them afterwards (`task_postrun`).

Nothing in a task body has to know about any of this.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import BotoCoreError
from celery import Celery, Task
from celery.signals import (
    before_task_publish,
    setup_logging,
    task_failure,
    task_postrun,
    task_prerun,
    worker_process_init,
)
from celery.signals import (
    import_modules as celery_on_after_configure,
)
from sqlalchemy.exc import OperationalError

from app.core.audit.context import bind_actor, clear_actor, current_actor
from app.core.config import settings
from app.core.logging import (
    bind_request_context,
    clear_request_context,
    configure_logging,
    current_request_id,
    current_trace_id,
    get_logger,
)
from app.core.security.current_user import Principal

log = get_logger(__name__)


def _split_roles(raw: str | None) -> list[str]:
    return [r for r in (raw or "").split(",") if r]


REQUEST_ID_HEADER = "x_request_id"
TRACE_ID_HEADER = "x_trace_id"
ACTOR_ID_HEADER = "x_actor_id"
ACTOR_ROLES_HEADER = "x_actor_roles"

#: Failures worth trying again: something outside the process was briefly
#: unavailable. A bug in the task body is *not* here -- retrying a TypeError
#: burns the queue to arrive at the identical failure three more times.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,  # includes redis.ConnectionError and kombu's, which subclass it
    TimeoutError,
    OSError,  # socket-level failures, DNS, broken pipes
    OperationalError,  # SQLAlchemy: connection dropped, pool exhausted, deadlock
    BotoCoreError,  # object store unreachable, throttled or timing out
)


class BaseTask(Task):
    """The default base for **every** task in this codebase.

    Set as ``celery_app.Task``, so a feature gets the retry policy by existing
    rather than by remembering to ask for it. A task that genuinely wants
    different behaviour overrides these attributes in its own decorator.

    ``retry_backoff`` spaces attempts exponentially and ``retry_jitter``
    scatters them: without jitter, every task that failed during an outage
    retries in lockstep the moment it ends, and knocks the dependency over
    again.
    """

    autoretry_for = TRANSIENT_ERRORS
    retry_backoff = True
    retry_backoff_max = settings.task_retry_backoff_max_seconds
    retry_jitter = True
    max_retries = settings.task_max_retries
    #: Report the retry in the result backend, so a caller polling the task id
    #: sees RETRY rather than a task that appears to be hanging.
    track_started = True


celery_app = Celery(
    "common-app-base",
    task_cls=BaseTask,
    broker=settings.broker_url,
    backend=settings.result_backend,
    # No `include=[...]`: task modules are discovered, not listed. See
    # `load_tasks()` at the bottom of this module.
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Ack after the task finishes, so a worker crash re-queues rather than
    # silently dropping the job.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    task_track_started=True,
    task_time_limit=300,
    task_soft_time_limit=270,
    broker_connection_retry_on_startup=True,
)


@setup_logging.connect
def _configure_worker_logging(**_: Any) -> None:
    """Use our structlog pipeline in the worker, not Celery's own format.

    Without this the worker would emit plain text that Promtail cannot parse
    into correlated fields.
    """
    configure_logging()


@before_task_publish.connect
def _propagate_context(headers: dict[str, Any] | None = None, **_: Any) -> None:
    """Copy the caller's correlation ids onto the outgoing message."""
    if headers is None:
        return
    request_id = current_request_id()
    trace_id = current_trace_id()
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    if trace_id:
        headers[TRACE_ID_HEADER] = trace_id

    # The acting principal rides along too, so an audit row written inside the
    # task is attributed to the person who triggered it rather than to the
    # worker process.
    actor = current_actor()
    if actor is not None:
        headers[ACTOR_ID_HEADER] = actor.id
        headers[ACTOR_ROLES_HEADER] = ",".join(actor.roles)


@task_prerun.connect
def _bind_context(task_id: str | None = None, task: Any = None, **_: Any) -> None:
    """Rebind the originating ids inside the worker before the task runs."""
    clear_request_context()
    clear_actor()
    request = getattr(task, "request", None)
    request_id = getattr(request, REQUEST_ID_HEADER, None) if request else None
    trace_id = getattr(request, TRACE_ID_HEADER, None) if request else None
    actor_id = getattr(request, ACTOR_ID_HEADER, None) if request else None
    actor_roles = getattr(request, ACTOR_ROLES_HEADER, None) if request else None
    if actor_id:
        bind_actor(Principal(id=actor_id, roles=_split_roles(actor_roles)))

    bind_request_context(
        # Fall back to the Celery task id so a task published outside a request
        # (a beat schedule, say) is still traceable to something.
        request_id=request_id or f"task:{task_id}",
        trace_id=trace_id,
        task_id=task_id,
        task_name=getattr(task, "name", None),
    )
    log.info("task.started", task_id=task_id, task_name=getattr(task, "name", None))


@task_postrun.connect
def _unbind_context(task_id: str | None = None, state: str | None = None, **_: Any) -> None:
    log.info("task.finished", task_id=task_id, task_state=state)
    clear_request_context()
    clear_actor()


@task_failure.connect
def _report_failure(
    task_id: str | None = None, exception: BaseException | None = None, **_: Any
) -> None:
    """Log the failure with its ids and send it to Sentry, like an HTTP 500."""
    log.error(
        "task.failed",
        task_id=task_id,
        error=f"{type(exception).__name__}: {exception}" if exception else None,
    )
    try:
        import sentry_sdk

        if sentry_sdk.get_client().is_active() and exception is not None:
            sentry_sdk.capture_exception(exception)
    except Exception as exc:
        log.debug("sentry.task_capture.failed", error=str(exc))


def load_tasks() -> list[str]:
    """Import every discovered task module, registering its tasks.

    Called by the API process (at app startup) and by the worker (via the
    `on_after_configure` signal below). Both use the same discovery pass, so the
    two processes cannot disagree about which tasks exist -- which is what made
    the old hardcoded `include` dangerous.

    Import errors are fatal here, on purpose: a worker that starts with half its
    tasks missing looks healthy and drops jobs.
    """
    # Imported lazily: app.core.discovery imports app.core.config, and importing it
    # at module scope would make this module's import order matter.
    from app.core.discovery import import_discovered_tasks

    modules = import_discovered_tasks()
    # Mark the lazy loader satisfied so a later enqueue does not repeat the walk.
    import app.core.jobs as jobs_pkg

    jobs_pkg._tasks_loaded = True
    log.info("celery.tasks_loaded", module_count=len(modules), modules=modules)
    return modules


@celery_on_after_configure.connect
def _load_tasks_in_worker(**_: Any) -> None:
    """Register discovered tasks as soon as the worker has its config.

    `configure_logging()` first: this signal fires *before* Celery's
    `setup_logging`, so without it discovery's own log lines would be rendered by
    the default console formatter and be the only non-JSON output the worker
    produces -- which Promtail cannot parse. It is idempotent.
    """
    configure_logging()
    load_tasks()


@worker_process_init.connect
def _configure_worker_tracing(**_: Any) -> None:
    """Set up tracing inside each worker *child* process.

    Deliberately not done at import time in the parent. Celery's default pool
    forks, and a `BatchSpanProcessor` runs a background export thread that does
    **not** survive `fork` -- a provider built in the parent would look healthy
    in every child while silently exporting nothing. `worker_process_init` fires
    once per child, after the fork, which is the only correct place for it.

    With this in place `CeleryInstrumentor` makes task execution a child span of
    the request that published the message, so background work shows up in the
    same trace as the HTTP call that caused it.
    """
    # Imported here rather than at module scope: the API process configures
    # tracing through its own lifespan, and the worker should not pay for
    # importing the observability stack until it is actually starting up.
    from app.core.observability import configure_tracing, instrument_celery

    configure_tracing(settings)
    instrument_celery()
