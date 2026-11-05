"""Configuration loading for tenants."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from router.policy import COMPUTE, DATA_CLASSES, DEPLOYMENTS, TIERS

TENANT_KEYS = {
    "id",
    "api_key",
    "rpm",
    "region",
    "data_class",
    "allowed_aliases",
    "allowed_tiers",
    "allowed_compute",
    "allowed_deployment",
    "allow_cloud",
    "latency_budget_ms",
    "admin",
}


@dataclass(frozen=True)
class Tenant:
    id: str
    api_key: str
    rpm: int
    region: str = "us"
    data_class: str = "internal"
    allowed_aliases: tuple[str, ...] = ()
    allowed_tiers: tuple[str, ...] = ()
    allowed_compute: tuple[str, ...] = ()
    allowed_deployment: tuple[str, ...] = ()
    allow_cloud: bool = False
    admin: bool = False
    latency_budget_ms: int = 0


def _string_list(value, field: str, tenant_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"tenant '{tenant_id}': {field} must be a list of non-empty strings")
    return tuple(value)


def load_tenants(path: str) -> list[Tenant]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    tenants: list[Tenant] = []
    for item in data.get("tenants") or []:
        if not isinstance(item, dict):
            raise ValueError(f"{path}: each tenant must be a mapping")
        unknown = item.keys() - TENANT_KEYS
        if unknown:
            raise ValueError(f"{path}: tenant uses unknown keys {sorted(unknown)}")
        tenant_id = str(item.get("id", "")).strip()
        if not tenant_id:
            raise ValueError(f"{path}: tenant id must not be empty")
        api_key = str(item.get("api_key", ""))
        if not api_key:
            raise ValueError(f"tenant '{tenant_id}': api_key must not be empty")
        rpm = int(item.get("rpm", 120))
        if rpm <= 0:
            raise ValueError(f"tenant '{tenant_id}': rpm must be positive")
        # 0 means no budget: the tenant accepts whatever tier admission allows.
        latency_budget_ms = int(item.get("latency_budget_ms") or 0)
        if latency_budget_ms < 0:
            raise ValueError(f"tenant '{tenant_id}': latency_budget_ms must be >= 0")
        region = str(item.get("region", "us"))
        data_class = str(item.get("data_class", "internal"))
        if data_class not in DATA_CLASSES:
            raise ValueError(
                f"tenant '{tenant_id}': data_class must be one of {sorted(DATA_CLASSES)}"
            )
        allowed_tiers = _string_list(item.get("allowed_tiers"), "allowed_tiers", tenant_id)
        unknown_tiers = set(allowed_tiers) - TIERS
        if unknown_tiers:
            raise ValueError(f"tenant '{tenant_id}': unknown tiers {sorted(unknown_tiers)}")
        allowed_compute = _string_list(
            item.get("allowed_compute"), "allowed_compute", tenant_id
        )
        unknown_compute = set(allowed_compute) - COMPUTE
        if unknown_compute:
            raise ValueError(
                f"tenant '{tenant_id}': unknown compute types {sorted(unknown_compute)}"
            )
        allowed_deployment = _string_list(
            item.get("allowed_deployment"), "allowed_deployment", tenant_id
        )
        unknown_deployment = set(allowed_deployment) - DEPLOYMENTS
        if unknown_deployment:
            raise ValueError(
                f"tenant '{tenant_id}': unknown deployments {sorted(unknown_deployment)}"
            )
        tenants.append(
            Tenant(
                id=tenant_id,
                api_key=api_key,
                rpm=rpm,
                region=region,
                data_class=data_class,
                allowed_aliases=_string_list(
                    item.get("allowed_aliases"), "allowed_aliases", tenant_id
                ),
                allowed_tiers=allowed_tiers,
                allowed_compute=allowed_compute,
                allowed_deployment=allowed_deployment,
                allow_cloud=bool(item.get("allow_cloud", False)),
                admin=bool(item.get("admin", False)),
                latency_budget_ms=latency_budget_ms,
            )
        )
    if not tenants:
        raise ValueError(f"no tenants defined in {path}")
    ids = [tenant.id for tenant in tenants]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: duplicate tenant id")
    return tenants
