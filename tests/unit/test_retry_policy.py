"""Every task inherits a retry policy; transient failures are survivable.

`task_acks_late` only covers a *worker* dying. A task whose broker, database or
object store blinks used to fail permanently on the first attempt -- the most
common failure in a distributed system had no answer at all.
"""

from __future__ import annotations

import pytest

from app.core.jobs.celery_app import BaseTask, celery_app


@pytest.fixture(autouse=True)
def _eager() -> None:
    """Run tasks inline so retries are observable without a broker."""
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False
    yield
    celery_app.conf.task_always_eager = False


def test_the_base_task_carries_a_real_policy() -> None:
    assert BaseTask.autoretry_for, "no transient error class is retried"
    assert BaseTask.retry_backoff is not False, "retries must back off, not hammer"
    assert BaseTask.retry_jitter is True, (
        "without jitter, retries synchronise into a thundering herd"
    )
    assert isinstance(BaseTask.max_retries, int) and BaseTask.max_retries > 0, (
        "retries must be capped, or a poisoned message never dies"
    )


def test_every_discovered_task_inherits_the_policy() -> None:
    """The policy applies by existing -- there is no per-task opt-in to forget."""
    from app.core.jobs import ensure_tasks_loaded

    ensure_tasks_loaded()
    ours = [t for name, t in celery_app.tasks.items() if not name.startswith("celery.")]
    assert ours
    assert all(isinstance(task, BaseTask) for task in ours), [
        task.name for task in ours if not isinstance(task, BaseTask)
    ]


def test_a_transient_failure_is_retried_and_then_succeeds() -> None:
    attempts = {"n": 0}

    @celery_app.task(name="tests.flaky_then_ok")
    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("the database is briefly unreachable")
        return "ok"

    result = flaky.apply()
    assert result.successful(), f"state={result.state} value={result.result!r}"
    assert result.get() == "ok"
    assert attempts["n"] == 3, "expected two retries before the success"


def test_a_permanent_transient_failure_ends_failed_after_the_cap() -> None:
    attempts = {"n": 0}

    @celery_app.task(name="tests.always_unreachable", max_retries=2)
    def never_ok() -> str:
        attempts["n"] += 1
        raise ConnectionError("gone for good")

    result = never_ok.apply()
    assert result.failed(), f"state={result.state}"
    assert attempts["n"] == 3, f"expected the first attempt plus 2 retries, got {attempts['n']}"


def test_a_bug_is_not_retried() -> None:
    """Retrying a TypeError just burns the queue -- it will fail identically."""
    attempts = {"n": 0}

    @celery_app.task(name="tests.programming_error")
    def buggy() -> str:
        attempts["n"] += 1
        raise TypeError("this is a bug, not a blip")

    result = buggy.apply()
    assert result.failed()
    assert attempts["n"] == 1
