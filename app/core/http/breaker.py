"""A per-host circuit breaker for outbound calls.

The problem it solves is not the failing dependency -- it is what this service
does *about* it. Without a breaker, a dead upstream is discovered one request at
a time: every caller waits the full timeout, every worker is occupied waiting,
and the queue behind them grows until this service is down too. The dependency's
incident becomes ours.

A breaker converts that slow failure into a fast one. After
``failure_threshold`` consecutive failures to a host the circuit **opens** and
subsequent calls fail immediately, without a socket. After ``reset_seconds`` one
request is allowed through as a probe (**half-open**): if it succeeds the circuit
**closes**, if it fails the clock restarts.

State is per-process on purpose. Sharing it through Redis would add a network
round trip to the very path that exists to avoid waiting on the network, and
would make the breaker fail when its own store did. Each replica learning
independently costs a few extra probes and is far more robust.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.logging import get_logger

log = get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _HostState:
    failures: int = 0
    opened_at: float = 0.0
    state: CircuitState = CircuitState.CLOSED
    #: Set while a half-open probe is in flight, so a burst of concurrent
    #: requests sends exactly one probe rather than all of them.
    probing: bool = False


@dataclass
class CircuitBreaker:
    """Tracks one circuit per host. Not thread-safe; asyncio-safe by design.

    All mutation happens in synchronous methods, so there is no await between
    reading and writing a host's state -- which is what makes an explicit lock
    unnecessary on a single event loop.
    """

    failure_threshold: int = 5
    reset_seconds: float = 30.0
    enabled: bool = True
    _hosts: dict[str, _HostState] = field(default_factory=dict)
    #: Injected in tests so an elapsed reset window needs no real sleep.
    #: Production always uses the monotonic clock.
    _clock: Callable[[], float] | None = None

    def _now(self) -> float:
        return self._clock() if self._clock is not None else time.monotonic()

    def _state_for(self, host: str) -> _HostState:
        if host not in self._hosts:
            self._hosts[host] = _HostState()
        return self._hosts[host]

    def state(self, host: str) -> CircuitState:
        """Current state, having first honoured an elapsed reset window."""
        if not self.enabled:
            return CircuitState.CLOSED
        st = self._state_for(host)
        if st.state is CircuitState.OPEN and self._now() - st.opened_at >= self.reset_seconds:
            st.state = CircuitState.HALF_OPEN
            st.probing = False
            log.info("circuit.half_open", upstream_host=host)
        return st.state

    def allows(self, host: str) -> bool:
        """Whether a call may be attempted right now."""
        if not self.enabled:
            return True
        current = self.state(host)
        if current is CircuitState.CLOSED:
            return True
        if current is CircuitState.HALF_OPEN and not self._state_for(host).probing:
            self._state_for(host).probing = True
            return True
        return False

    def record_success(self, host: str) -> None:
        if not self.enabled:
            return
        st = self._state_for(host)
        if st.state is not CircuitState.CLOSED:
            log.info("circuit.closed", upstream_host=host, after_state=st.state.value)
        st.failures = 0
        st.state = CircuitState.CLOSED
        st.probing = False

    def record_failure(self, host: str) -> None:
        """Count a failure and open the circuit once the threshold is reached.

        A failed half-open probe re-opens immediately rather than counting
        towards the threshold again: the probe *was* the evidence.
        """
        if not self.enabled:
            return
        st = self._state_for(host)
        st.probing = False
        if st.state is CircuitState.HALF_OPEN:
            st.opened_at = self._now()
            st.state = CircuitState.OPEN
            log.warning("circuit.reopened", upstream_host=host, probe="failed")
            return

        st.failures += 1
        if st.failures >= self.failure_threshold and st.state is CircuitState.CLOSED:
            st.opened_at = self._now()
            st.state = CircuitState.OPEN
            log.warning(
                "circuit.opened",
                upstream_host=host,
                consecutive_failures=st.failures,
                reset_seconds=self.reset_seconds,
            )

    def retry_after(self, host: str) -> int:
        """Seconds until the next probe, for the ``Retry-After`` header."""
        st = self._state_for(host)
        if st.state is not CircuitState.OPEN:
            return 1
        remaining = self.reset_seconds - (self._now() - st.opened_at)
        return max(1, int(remaining) + 1)

    def reset(self) -> None:
        """Forget all state. For tests and for a deliberate operational reset."""
        self._hosts.clear()
