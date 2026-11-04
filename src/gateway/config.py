"""Configuration loading for routes and tenants."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Route:
    alias: str
    tier: str
    backend: str
    upstream_model: str
    fallback: str | None = None
    description: str = ""


@dataclass(frozen=True)
class RouteTable:
    version: str
    routes: dict[str, Route]

    def get(self, alias: str) -> Route | None:
        return self.routes.get(alias)


def load_routes(path: str) -> RouteTable:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    routes: dict[str, Route] = {}
    for alias, spec in (data.get("routes") or {}).items():
        routes[alias] = Route(
            alias=alias,
            tier=spec["tier"],
            backend=str(spec["backend"]).rstrip("/"),
            upstream_model=spec.get("upstream_model", alias),
            fallback=spec.get("fallback"),
            description=spec.get("description", ""),
        )
    if not routes:
        raise ValueError(f"no routes defined in {path}")
    for name, route in routes.items():
        if route.fallback and route.fallback not in routes:
            raise ValueError(f"route '{name}' falls back to unknown alias '{route.fallback}'")
    return RouteTable(version=str(data.get("version", "1")), routes=routes)


@dataclass(frozen=True)
class Tenant:
    id: str
    api_key: str
    rpm: int


def load_tenants(path: str) -> list[Tenant]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    tenants = [
        Tenant(id=item["id"], api_key=item["api_key"], rpm=int(item.get("rpm", 120)))
        for item in data.get("tenants") or []
    ]
    if not tenants:
        raise ValueError(f"no tenants defined in {path}")
    return tenants
