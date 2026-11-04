"""Versioned routing policy.

A policy is data, not code: it can be reloaded without a restart, diffed in a
review, and rolled back by restoring the previous file. The gateway never
routes to a backend that is not described here.

An alias is what a client asks for. A candidate is a place the request can run:
a tier, a backend, a region, and the model version that backend serves. Every
alias has exactly one primary; the remaining candidates are standbys, except
the one named by the optional canary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

TIERS = frozenset({"gpu", "cpu", "cloud"})
STICKY_KEYS = frozenset({"tenant", "request"})
DATA_CLASSES = frozenset({"public", "internal", "confidential", "restricted"})


def _require_keys(raw: dict, required: set[str], optional: set[str], where: str) -> None:
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"{where}: missing {sorted(missing)}")
    unknown = raw.keys() - required - optional
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")


@dataclass(frozen=True)
class Candidate:
    name: str
    tier: str
    backend: str
    upstream_model: str
    region: str
    serves_regions: tuple[str, ...]
    sovereign: bool
    priority: int
    max_context: int
    cost_per_1k_out: float
    model_version: str


@dataclass(frozen=True)
class Canary:
    candidate: str
    percent: int
    sticky: str


@dataclass(frozen=True)
class Shadow:
    candidate: str
    percent: int
    max_tokens: int


@dataclass(frozen=True)
class Alias:
    name: str
    description: str
    primary: str
    candidates: dict[str, Candidate]
    canary: Canary | None = None
    shadow: Shadow | None = None


@dataclass(frozen=True)
class BreakerPolicy:
    failures: int
    window_s: float
    open_s: float


@dataclass(frozen=True)
class Policy:
    version: str
    aliases: dict[str, Alias]
    breaker: BreakerPolicy

    def alias(self, name: str) -> Alias | None:
        return self.aliases.get(name)


def _candidate(raw: dict, where: str) -> Candidate:
    _require_keys(
        raw,
        {"name", "tier", "backend", "upstream_model"},
        {
            "region",
            "serves_regions",
            "sovereign",
            "priority",
            "max_context",
            "cost_per_1k_out",
            "model_version",
        },
        where,
    )
    tier = str(raw["tier"])
    if tier not in TIERS:
        raise ValueError(f"{where}: tier must be one of {sorted(TIERS)}, got '{tier}'")
    serves = raw.get("serves_regions") or ["*"]
    if not isinstance(serves, list) or not all(isinstance(item, str) and item for item in serves):
        raise ValueError(f"{where}: serves_regions must be a list of non-empty strings")
    max_context = int(raw.get("max_context", 8192))
    if max_context <= 0:
        raise ValueError(f"{where}: max_context must be positive")
    cost = float(raw.get("cost_per_1k_out", 0.0))
    if cost < 0:
        raise ValueError(f"{where}: cost_per_1k_out must be >= 0")
    upstream_model = str(raw["upstream_model"])
    return Candidate(
        name=str(raw["name"]),
        tier=tier,
        backend=str(raw["backend"]).rstrip("/"),
        upstream_model=upstream_model,
        region=str(raw.get("region", "onprem")),
        serves_regions=tuple(serves),
        sovereign=bool(raw.get("sovereign", True)),
        priority=int(raw.get("priority", 0)),
        max_context=max_context,
        cost_per_1k_out=cost,
        model_version=str(raw.get("model_version", upstream_model)),
    )


def _alias(name: str, raw: dict, where: str) -> Alias:
    _require_keys(raw, {"primary", "candidates"}, {"description", "canary", "shadow"}, where)
    entries = raw["candidates"]
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{where}: candidates must be a non-empty list")
    candidates: dict[str, Candidate] = {}
    for index, item in enumerate(entries):
        candidate = _candidate(item, f"{where}.candidates[{index}]")
        if candidate.name in candidates:
            raise ValueError(f"{where}: duplicate candidate '{candidate.name}'")
        candidates[candidate.name] = candidate

    primary = str(raw["primary"])
    if primary not in candidates:
        raise ValueError(f"{where}: primary '{primary}' is not a candidate")

    canary = None
    if raw.get("canary") is not None:
        spec = raw["canary"]
        _require_keys(spec, {"candidate", "percent"}, {"sticky"}, f"{where}.canary")
        percent = int(spec["percent"])
        if not 0 <= percent <= 100:
            raise ValueError(f"{where}.canary: percent must be 0..100")
        sticky = str(spec.get("sticky", "tenant"))
        if sticky not in STICKY_KEYS:
            raise ValueError(f"{where}.canary: sticky must be one of {sorted(STICKY_KEYS)}")
        if spec["candidate"] not in candidates:
            raise ValueError(f"{where}.canary: unknown candidate '{spec['candidate']}'")
        if spec["candidate"] == primary:
            raise ValueError(f"{where}.canary: candidate must differ from primary")
        canary = Canary(candidate=str(spec["candidate"]), percent=percent, sticky=sticky)

    shadow = None
    if raw.get("shadow") is not None:
        spec = raw["shadow"]
        _require_keys(spec, {"candidate", "percent"}, {"max_tokens"}, f"{where}.shadow")
        percent = int(spec["percent"])
        if not 0 <= percent <= 100:
            raise ValueError(f"{where}.shadow: percent must be 0..100")
        max_tokens = int(spec.get("max_tokens", 64))
        if max_tokens <= 0:
            raise ValueError(f"{where}.shadow: max_tokens must be positive")
        if spec["candidate"] not in candidates:
            raise ValueError(f"{where}.shadow: unknown candidate '{spec['candidate']}'")
        if spec["candidate"] == primary:
            raise ValueError(f"{where}.shadow: candidate must differ from primary")
        shadow = Shadow(
            candidate=str(spec["candidate"]), percent=percent, max_tokens=max_tokens
        )

    return Alias(
        name=name,
        description=str(raw.get("description", "")),
        primary=primary,
        candidates=candidates,
        canary=canary,
        shadow=shadow,
    )


def load_policy(path: str) -> Policy:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    _require_keys(data, {"version", "aliases"}, {"breaker"}, path)
    version = str(data["version"]).strip()
    if not version:
        raise ValueError(f"{path}: version must not be empty")

    breaker_raw = data.get("breaker") or {}
    _require_keys(breaker_raw, set(), {"failures", "window_s", "open_s"}, f"{path}.breaker")
    breaker = BreakerPolicy(
        failures=int(breaker_raw.get("failures", 3)),
        window_s=float(breaker_raw.get("window_s", 60)),
        open_s=float(breaker_raw.get("open_s", 20)),
    )
    if breaker.failures <= 0 or breaker.window_s <= 0 or breaker.open_s <= 0:
        raise ValueError(f"{path}.breaker: failures, window_s and open_s must be positive")

    aliases: dict[str, Alias] = {}
    for name, spec in (data.get("aliases") or {}).items():
        if not isinstance(spec, dict):
            raise ValueError(f"{path}: alias '{name}' must be a mapping")
        aliases[name] = _alias(str(name), spec, f"{path}:alias '{name}'")
    if not aliases:
        raise ValueError(f"{path}: no aliases defined")

    return Policy(version=version, aliases=aliases, breaker=breaker)
