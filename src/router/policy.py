"""Versioned routing policy.

A policy is data, not code: it can be reloaded without a restart, diffed in a
review, and rolled back by restoring the previous file. The gateway never
routes to a backend that is not described here.

An alias is what a client asks for. A candidate is a place the request can run:
a pool, a compute type, an on-prem or cloud deployment, a backend, a region,
and the model version that backend serves. Every alias has exactly one primary;
the remaining candidates are standbys, except the one named by the optional
canary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

TIERS = frozenset({"gpu", "cpu", "cloud"})
COMPUTE = frozenset({"gpu", "cpu"})
DEPLOYMENTS = frozenset({"onprem", "cloud"})
STICKY_KEYS = frozenset({"tenant", "request"})
DATA_CLASSES = frozenset({"public", "internal", "confidential", "restricted"})
ACCESS_SOURCES = frozenset({"static", "file", "oidc"})
HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
TOKEN_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OIDC_FIELDS = ("token_url", "client_id", "scope")


def _require_keys(raw: dict, required: set[str], optional: set[str], where: str) -> None:
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"{where}: missing {sorted(missing)}")
    unknown = raw.keys() - required - optional
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")


@dataclass(frozen=True)
class Access:
    """How to authenticate to a backend that is not inside the cluster.

    The policy names the key; it does not contain it. `key` is a lookup into a
    mounted secret file, so a policy diff never carries a credential.

    `source` says how the credential becomes a header value:

    * `static` — the value in the keys file is the credential, sent as-is until
      the Secret changes and the process restarts;
    * `file` — `key` names a file in the token directory (a projected
      service-account token, for example), read on every request so rotation
      needs no restart;
    * `oidc` — the keys file holds a client secret, exchanged at `token_url`
      for a short-lived access token. The exchange is server-side and the token
      is cached in the process, never in the policy.
    """

    key: str
    header: str = "authorization"
    prefix: str = "Bearer "
    source: str = "static"
    token_url: str | None = None
    client_id: str | None = None
    scope: str | None = None


@dataclass(frozen=True)
class Candidate:
    name: str
    compute: str
    deployment: str
    pool: str
    backend: str
    upstream_model: str
    region: str
    serves_regions: tuple[str, ...]
    sovereign: bool
    priority: int
    max_context: int
    cost_per_1k_out: float
    model_version: str
    access: Access | None = None

    @property
    def tier(self) -> str:
        """The label clients and tenant allowlists see. A cloud deployment is
        `cloud` whatever hardware it runs; an on-prem candidate is named by its
        compute. Hardware and location are separate fields because a tenant may
        allow GPUs but forbid leaving the perimeter."""
        return "cloud" if self.deployment == "cloud" else self.compute

    @property
    def external(self) -> bool:
        return self.access is not None


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


def _access(raw: dict, where: str) -> Access:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}.access must be a mapping")
    _require_keys(
        raw,
        {"key"},
        {"header", "prefix", "source", "token_url", "client_id", "scope"},
        f"{where}.access",
    )
    key = raw["key"]
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"{where}.access: key must be a non-empty name")
    key = key.strip()

    source = str(raw.get("source", "static")).strip()
    if source not in ACCESS_SOURCES:
        raise ValueError(
            f"{where}.access: source must be one of {sorted(ACCESS_SOURCES)}, got '{source}'"
        )

    if source == "file":
        # The key becomes a path in the token directory, so it must be a plain
        # file name: no separators, no traversal, no hidden dotfiles.
        if not TOKEN_FILE_NAME.match(key) or key in {".", ".."}:
            raise ValueError(
                f"{where}.access: source 'file' needs key to name a token file "
                f"(letters, digits, '.', '_' or '-'), got '{key}'"
            )

    if source != "oidc":
        for field in OIDC_FIELDS:
            if raw.get(field) is not None:
                raise ValueError(
                    f"{where}.access: '{field}' is only valid for source 'oidc', "
                    f"not '{source}'"
                )
        token_url = client_id = scope = None
    else:
        token_url = raw.get("token_url")
        if not isinstance(token_url, str) or not token_url.strip():
            raise ValueError(f"{where}.access: source 'oidc' requires 'token_url'")
        token_url = token_url.strip()
        client_id = raw.get("client_id")
        if not isinstance(client_id, str) or not client_id.strip():
            raise ValueError(f"{where}.access: source 'oidc' requires 'client_id'")
        client_id = client_id.strip()
        scope = raw.get("scope")
        if scope is not None:
            if not isinstance(scope, str) or not scope.strip():
                raise ValueError(
                    f"{where}.access: 'scope' must be a non-empty string when set"
                )
            scope = scope.strip()
        _https_url(token_url, f"{where}.access", "token_url")

    header = str(raw.get("header", "authorization")).strip()
    if not HEADER_NAME.match(header):
        raise ValueError(f"{where}.access: header must be a token like 'Authorization'")
    prefix = str(raw.get("prefix", "Bearer "))
    if any(char in prefix for char in "\r\n"):
        raise ValueError(f"{where}.access: prefix must not contain line breaks")
    return Access(
        key=key,
        header=header,
        prefix=prefix,
        source=source,
        token_url=token_url,
        client_id=client_id,
        scope=scope,
    )


def _https_url(url: str, where: str, what: str) -> None:
    """The one URL rule for anything the gateway calls outside the cluster.

    Shared by the backend endpoint and the OIDC token endpoint so a new field
    cannot arrive with a weaker check than the one next to it.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"{where}: {what} must use https, got '{url}'")
    if not parts.hostname:
        raise ValueError(f"{where}: {what} must name a host")
    if parts.username or parts.password:
        raise ValueError(f"{where}: {what} must not embed credentials")
    if parts.query or parts.fragment:
        raise ValueError(f"{where}: {what} must not carry a query or fragment")


def _external_backend(backend: str, where: str) -> None:
    _https_url(backend, where, "external backend")


def _candidate(raw: dict, where: str) -> Candidate:
    _require_keys(
        raw,
        {"name", "compute", "deployment", "pool", "backend", "upstream_model"},
        {
            "region",
            "serves_regions",
            "sovereign",
            "priority",
            "max_context",
            "cost_per_1k_out",
            "model_version",
            "access",
        },
        where,
    )
    compute = str(raw["compute"])
    if compute not in COMPUTE:
        raise ValueError(f"{where}: compute must be one of {sorted(COMPUTE)}, got '{compute}'")
    deployment = str(raw["deployment"])
    if deployment not in DEPLOYMENTS:
        raise ValueError(
            f"{where}: deployment must be one of {sorted(DEPLOYMENTS)}, got '{deployment}'"
        )
    pool = str(raw["pool"]).strip()
    if not pool:
        raise ValueError(f"{where}: pool must not be empty")
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
    backend = str(raw["backend"]).rstrip("/")
    access = _access(raw["access"], where) if raw.get("access") is not None else None
    if access is not None:
        _external_backend(backend, where)
    return Candidate(
        name=str(raw["name"]),
        compute=compute,
        deployment=deployment,
        pool=pool,
        backend=backend,
        upstream_model=upstream_model,
        region=str(raw.get("region", "onprem")),
        serves_regions=tuple(serves),
        sovereign=bool(raw.get("sovereign", True)),
        priority=int(raw.get("priority", 0)),
        max_context=max_context,
        cost_per_1k_out=cost,
        model_version=str(raw.get("model_version", upstream_model)),
        access=access,
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
