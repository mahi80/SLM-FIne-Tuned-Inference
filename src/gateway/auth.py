"""Tenant authentication and per-tenant rate limiting."""

from __future__ import annotations

import secrets
import time
from collections import deque

from .config import Tenant


class TenantRegistry:
    def __init__(self, tenants: list[Tenant]) -> None:
        self._tenants = tenants
        self._windows: dict[str, deque[float]] = {t.id: deque() for t in tenants}

    def resolve(self, token: str) -> Tenant | None:
        # Compare against every key in constant time so a rejected request does
        # not reveal, through timing, how close a guess was.
        found: Tenant | None = None
        for tenant in self._tenants:
            if secrets.compare_digest(tenant.api_key, token):
                found = tenant
        return found

    def allow(self, tenant: Tenant) -> bool:
        """Sliding one-minute window. In-memory on purpose: Phase 3 moves this
        to the policy store with a shared counter for multi-replica gateways."""
        now = time.monotonic()
        window = self._windows[tenant.id]
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= tenant.rpm:
            return False
        window.append(now)
        return True
