"""Credentials, TLS and health for backends outside the cluster.

The policy names a key; it never contains one. Keys live in a mounted file
(`keys: {external-prod: ...}`) so a policy diff, an audit record or a log line
cannot leak a credential. Nothing in this module prints or returns key values,
except as a request header sent to the named backend.

In-cluster backends are plain HTTP on a NetworkPolicy-restricted path. An
external backend is HTTPS only, validated by the policy parser, and is reachable
only when the deployment opts into the `cloud-egress` component.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import httpx
import yaml

from router.policy import Access, Candidate, Policy

PROBE_TIMEOUT = 5.0


def load_backend_keys(path: str | Path | None) -> dict[str, str]:
    """Read `keys: {name: secret}`. A missing file means no external keys.

    Missing is not an error: an in-cluster-only deployment has no cloud keys,
    and that should leave the gateway running with external candidates vetoed.
    """
    if not path:
        return {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: backend keys must be a mapping")
    unknown = data.keys() - {"keys"}
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    entries = data.get("keys") or {}
    if not isinstance(entries, dict):
        raise ValueError(f"{path}: 'keys' must be a mapping of name to secret")
    keys: dict[str, str] = {}
    for name, value in entries.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path}: backend key names must be non-empty strings")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path}: backend key '{name}' must be a non-empty string")
        if "\r" in value or "\n" in value:
            raise ValueError(f"{path}: backend key '{name}' must be a single line")
        keys[name.strip()] = value
    return keys


def auth_headers(access: Access | None, keys: Mapping[str, str]) -> dict[str, str]:
    if access is None:
        return {}
    value = keys.get(access.key)
    if not value:
        return {}
    return {access.header: f"{access.prefix}{value}"}


@dataclass(frozen=True)
class BackendAccess:
    keys: Mapping[str, str]
    ca_path: str | None = None

    @classmethod
    def load(cls, keys_path: str | Path | None, ca_path: str | Path | None) -> "BackendAccess":
        if ca_path and not Path(ca_path).is_file():
            # Falling back to system trust because a configured bundle is
            # missing would silently accept the wrong certificate.
            raise FileNotFoundError(f"backend CA bundle not found: {ca_path}")
        return cls(keys=load_backend_keys(keys_path), ca_path=str(ca_path) if ca_path else None)

    @property
    def verify(self) -> bool | ssl.SSLContext:
        if not self.ca_path:
            return True
        return ssl.create_default_context(cafile=self.ca_path)

    def ready(self, candidate: Candidate) -> bool:
        if not candidate.external:
            return True
        assert candidate.access is not None
        return candidate.access.key in self.keys

    def headers(self, candidate: Candidate) -> dict[str, str]:
        return auth_headers(candidate.access, self.keys)

    def unconfigured(self, policy: Policy) -> list[str]:
        names = {
            candidate.name
            for alias in policy.aliases.values()
            for candidate in alias.candidates.values()
            if not self.ready(candidate)
        }
        return sorted(names)


def open_upstream(
    client: httpx.AsyncClient,
    candidate: Candidate,
    payload: dict,
    stream: bool,
    headers: Mapping[str, str] | None = None,
) -> "httpx.Response":
    request = client.build_request(
        "POST",
        f"{candidate.backend}/v1/chat/completions",
        json=payload,
        headers={"Content-Type": "application/json", **dict(headers or {})},
    )
    return client.send(request, stream=stream)


async def probe(
    client: httpx.AsyncClient,
    candidate: Candidate,
    headers: Mapping[str, str] | None = None,
    timeout: float = PROBE_TIMEOUT,
) -> bool:
    """One health check. Transport error or a 5xx means the backend is down.

    A 4xx still proves the host answered and TLS verified, so it counts as up;
    an expired key surfaces as a request error, not a silent removal.
    """
    try:
        response = await client.get(
            f"{candidate.backend}/health",
            headers=dict(headers or {}),
            timeout=timeout,
        )
    except httpx.HTTPError:
        return False
    return response.status_code < 500
