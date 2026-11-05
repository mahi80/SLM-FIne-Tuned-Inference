"""Routing decisions: admission first, scoring second.

Admission answers "is this candidate allowed to serve this request at all".
Scoring answers "which allowed standby is best". A cheap tier never wins a
request it is not entitled to serve, which is the difference between a router
and a load balancer.

Canary assignment is deterministic: blake2b(alias, tenant-or-request) mod 100.
A retry, a second replica or a process restart lands in the same bucket, so a
tenant does not flip between model versions mid-session.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from . import breakers
from .policy import Candidate, Policy, load_policy

SOVEREIGN_CLASSES = frozenset({"confidential", "restricted"})


class TenantView(Protocol):
    id: str
    region: str
    data_class: str
    allowed_aliases: tuple[str, ...]
    allowed_tiers: tuple[str, ...]
    allowed_compute: tuple[str, ...]
    allowed_deployment: tuple[str, ...]
    allow_cloud: bool
    latency_budget_ms: int


@dataclass(frozen=True)
class Verdict:
    candidate: str
    admitted: bool
    reason: str


@dataclass(frozen=True)
class Decision:
    alias: str
    policy_version: str
    kind: str
    chosen: Candidate | None
    attempts: tuple[Candidate, ...]
    considered: tuple[Verdict, ...]
    bucket: int
    canary_percent: int
    canary_hit: bool
    shadow: Candidate | None
    refusal: str | None

    def explain(self) -> dict:
        return {
            "alias": self.alias,
            "policy_version": self.policy_version,
            "decision": self.kind,
            "chosen": self.chosen.name if self.chosen else None,
            "tier": self.chosen.tier if self.chosen else None,
            "pool": self.chosen.pool if self.chosen else None,
            "attempts": [candidate.name for candidate in self.attempts],
            "canary": {
                "percent": self.canary_percent,
                "bucket": self.bucket,
                "hit": self.canary_hit,
            },
            "shadow": self.shadow.name if self.shadow else None,
            "refusal": self.refusal,
            "considered": [
                {"candidate": item.candidate, "admitted": item.admitted, "reason": item.reason}
                for item in self.considered
            ],
        }


def _bucket(prefix: str, subject: str) -> int:
    digest = hashlib.blake2b(f"{prefix}:{subject}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % 100


def breaker_keys(policy: Policy) -> set[str]:
    return {
        breakers.key(alias.name, candidate.name)
        for alias in policy.aliases.values()
        for candidate in alias.candidates.values()
    }


class PolicyEngine:
    def __init__(
        self,
        policy: Policy,
        circuit_breakers: breakers.CircuitBreakers,
        backend_ready: Callable[[Candidate], bool] | None = None,
    ) -> None:
        self.policy = policy
        self.breakers = circuit_breakers
        self._backend_ready = backend_ready

    def _admit(
        self,
        alias: str,
        candidate: Candidate,
        tenant: TenantView,
        required_tokens: int,
        latency_budget_ms: int = 0,
    ) -> tuple[bool, str]:
        if tenant.allowed_tiers and candidate.tier not in tenant.allowed_tiers:
            return False, "tier-denied"
        if tenant.allowed_compute and candidate.compute not in tenant.allowed_compute:
            return False, "compute-denied"
        if tenant.allowed_deployment and candidate.deployment not in tenant.allowed_deployment:
            return False, "deployment-denied"
        if candidate.deployment == "cloud" and not tenant.allow_cloud:
            return False, "cloud-denied"
        if candidate.external and self._backend_ready is not None:
            if not self._backend_ready(candidate):
                return False, "backend-unconfigured"
        if "*" not in candidate.serves_regions and tenant.region not in candidate.serves_regions:
            return False, "region-veto"
        if tenant.data_class in SOVEREIGN_CLASSES and not candidate.sovereign:
            return False, "sovereignty-veto"
        if required_tokens > candidate.max_context:
            return False, "context-exceeded"
        # A budget of 0 is no budget, and a candidate that declares no latency
        # (0) cannot be shown to miss one: both stay admissible.
        if (
            latency_budget_ms > 0
            and candidate.latency_ms > 0
            and candidate.latency_ms > latency_budget_ms
        ):
            return False, "latency-budget-exceeded"
        state = self.breakers.state(breakers.key(alias, candidate.name))
        if state == breakers.OPEN:
            return False, "breaker-open"
        if state == breakers.HALF_OPEN:
            return True, "half-open-probe"
        return True, "admitted"

    def _score(self, alias: str, candidate: Candidate) -> float:
        failure_rate = self.breakers.failure_rate(breakers.key(alias, candidate.name))
        return (
            candidate.priority * 100
            + candidate.cost_per_1k_out * 1000
            + failure_rate * 50
        )

    def decide(
        self,
        alias_name: str,
        tenant: TenantView,
        request_id: str,
        required_tokens: int,
        latency_budget_ms: int = 0,
    ) -> Decision:
        alias = self.policy.aliases[alias_name]
        canary = alias.canary
        subject = request_id
        if canary is not None and canary.sticky == "tenant":
            subject = tenant.id
        bucket = _bucket(f"canary:{alias_name}", subject)
        canary_percent = canary.percent if canary is not None else 0

        denied = bool(tenant.allowed_aliases) and alias_name not in tenant.allowed_aliases
        considered: list[Verdict] = []
        admitted: dict[str, Candidate] = {}
        for candidate in alias.candidates.values():
            if denied:
                ok, reason = False, "alias-denied"
            else:
                ok, reason = self._admit(
                    alias_name, candidate, tenant, required_tokens, latency_budget_ms
                )
            considered.append(Verdict(candidate.name, ok, reason))
            if ok:
                admitted[candidate.name] = candidate

        primary = alias.candidates[alias.primary]
        canary_name = canary.candidate if canary is not None else None
        canary_hit = bool(
            canary is not None
            and bucket < canary.percent
            and canary_name in admitted
        )

        kind = "refused"
        chosen: Candidate | None = None
        if canary_hit:
            chosen = admitted[canary_name]
            kind = "canary"
        elif primary.name in admitted:
            chosen = admitted[primary.name]
            kind = "primary"
        else:
            standbys = [
                candidate
                for name, candidate in admitted.items()
                if name not in (primary.name, canary_name)
            ]
            if standbys:
                chosen = min(standbys, key=lambda c: (self._score(alias_name, c), c.name))
                kind = "standby"

        standby_pool = [
            candidate
            for name, candidate in admitted.items()
            if name not in (primary.name, canary_name)
        ]
        standby_pool.sort(key=lambda c: (self._score(alias_name, c), c.name))
        attempts: list[Candidate] = []
        if chosen is not None:
            attempts.append(chosen)
            if kind == "canary" and primary.name in admitted:
                attempts.append(primary)
            attempts.extend(c for c in standby_pool if c not in attempts)

        shadow: Candidate | None = None
        if alias.shadow is not None and alias.shadow.percent > 0 and chosen is not None:
            target = admitted.get(alias.shadow.candidate)
            shadow_bucket = _bucket(f"shadow:{alias_name}", request_id)
            if (
                target is not None
                and shadow_bucket < alias.shadow.percent
                and target.name != chosen.name
            ):
                shadow = target

        refusal = None
        if chosen is None:
            refusal = "alias-denied" if denied else "no-capacity"

        return Decision(
            alias=alias_name,
            policy_version=self.policy.version,
            kind=kind,
            chosen=chosen,
            attempts=tuple(attempts),
            considered=tuple(considered),
            bucket=bucket,
            canary_percent=canary_percent,
            canary_hit=canary_hit,
            shadow=shadow,
            refusal=refusal,
        )


class PolicyStore:
    """The active policy, its breakers, and a validated reload path.

    A reload either fully replaces the policy or leaves the old one untouched:
    a typo in policy.yaml cannot take routing down.
    """

    def __init__(
        self,
        path: str | Path,
        clock: Callable[[], float] = time.monotonic,
        backend_ready: Callable[[Candidate], bool] | None = None,
    ) -> None:
        self.path = Path(path)
        self._backend_ready = backend_ready
        policy = load_policy(str(self.path))
        self._policy = policy
        self._breakers = breakers.CircuitBreakers(policy.breaker, clock)
        self._engine = PolicyEngine(policy, self._breakers, backend_ready)

    @property
    def policy(self) -> Policy:
        return self._policy

    @property
    def engine(self) -> PolicyEngine:
        return self._engine

    @property
    def breakers(self) -> breakers.CircuitBreakers:
        return self._breakers

    def reload(self) -> Policy:
        policy = load_policy(str(self.path))
        self._breakers.reconfigure(policy.breaker)
        self._breakers.prune(breaker_keys(policy))
        self._engine = PolicyEngine(policy, self._breakers, self._backend_ready)
        self._policy = policy
        return policy
