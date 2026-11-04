"""Per-candidate circuit breakers.

State machine: closed -> open after `failures` failures inside `window_s`;
open -> half-open after `open_s`; half-open admits one probe at a time and
returns to closed on success or open on failure.

Breakers live beside the policy, not inside it: a reload changes what is
allowed, not how healthy a backend currently is.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .policy import BreakerPolicy

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half-open"

STATE_VALUES = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}


def key(alias: str, candidate: str) -> str:
    return f"{alias}:{candidate}"


@dataclass
class _State:
    state: str = CLOSED
    failures: deque[float] = field(default_factory=deque)
    opened_at: float | None = None
    probe_at: float | None = None


class CircuitBreakers:
    def __init__(
        self, policy: BreakerPolicy, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._states: dict[str, _State] = {}

    def _get(self, key_: str) -> _State:
        return self._states.setdefault(key_, _State())

    def _refresh(self, entry: _State, now: float) -> None:
        if entry.state == OPEN and entry.opened_at is not None:
            if now - entry.opened_at >= self._policy.open_s:
                entry.state = HALF_OPEN
                entry.probe_at = None
        if entry.state == HALF_OPEN and entry.probe_at is not None:
            # A probe whose request vanished (client disconnect, cancelled task)
            # must not wedge the breaker in half-open forever.
            if now - entry.probe_at >= self._policy.open_s:
                entry.probe_at = None

    def _trim(self, entry: _State, now: float) -> None:
        while entry.failures and now - entry.failures[0] > self._policy.window_s:
            entry.failures.popleft()

    def state(self, key_: str) -> str:
        entry = self._get(key_)
        self._refresh(entry, self._clock())
        return entry.state

    def allow(self, key_: str) -> bool:
        """Admit a request. In half-open exactly one probe is admitted."""
        entry = self._get(key_)
        now = self._clock()
        self._refresh(entry, now)
        if entry.state == CLOSED:
            return True
        if entry.state == OPEN:
            return False
        if entry.probe_at is None:
            entry.probe_at = now
            return True
        return False

    def record_success(self, key_: str) -> None:
        entry = self._get(key_)
        entry.state = CLOSED
        entry.failures.clear()
        entry.opened_at = None
        entry.probe_at = None

    def record_failure(self, key_: str) -> str:
        entry = self._get(key_)
        now = self._clock()
        self._refresh(entry, now)
        self._trim(entry, now)
        entry.failures.append(now)
        if entry.state in (HALF_OPEN, OPEN):
            entry.state = OPEN
            entry.opened_at = now
            entry.probe_at = None
        elif len(entry.failures) >= self._policy.failures:
            entry.state = OPEN
            entry.opened_at = now
        return entry.state

    def failure_rate(self, key_: str) -> float:
        """Fraction of the failure budget consumed inside the window."""
        entry = self._get(key_)
        now = self._clock()
        self._trim(entry, now)
        if self._policy.failures <= 0:
            return 0.0
        return min(1.0, len(entry.failures) / self._policy.failures)

    def snapshot(self) -> dict[str, dict]:
        now = self._clock()
        result: dict[str, dict] = {}
        for key_, entry in self._states.items():
            self._refresh(entry, now)
            self._trim(entry, now)
            item = {"state": entry.state, "failures": len(entry.failures)}
            if entry.state == OPEN and entry.opened_at is not None:
                item["reopen_in_s"] = round(
                    max(0.0, self._policy.open_s - (now - entry.opened_at)), 1
                )
            result[key_] = item
        return result

    def prune(self, valid_keys: set[str]) -> None:
        for stale in set(self._states) - valid_keys:
            del self._states[stale]

    def reconfigure(self, policy: BreakerPolicy) -> None:
        """Adopt new thresholds without discarding observed backend health."""
        self._policy = policy
