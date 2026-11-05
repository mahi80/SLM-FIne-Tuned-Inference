"""Credentials, TLS and health for backends outside the cluster.

The policy names a key; it never contains one. Keys live in a mounted file
(`keys: {external-prod: ...}`) so a policy diff, an audit record or a log line
cannot leak a credential. Nothing in this module prints or returns key values,
except as a request header sent to the named backend.

A credential can arrive three ways, chosen by `access.source` in the policy:

* `static` — the value in the keys file is the credential, as in Stage 2;
* `file` — `access.key` names a file in the token directory (a projected
  service-account token, for example); it is read on every call, so rotation
  needs no restart;
* `oidc` — the keys file holds a client secret, exchanged at `token_url` for a
  short-lived access token with the client-credentials grant. The token is
  cached in this process until shortly before it expires.

Failures raise `CredentialError`, whose message names the key and, at most, an
HTTP status. A response body, a token and a secret never appear in an exception,
a log line or a probe result.

In-cluster backends are plain HTTP on a NetworkPolicy-restricted path. An
external backend is HTTPS only, validated by the policy parser, and is reachable
only when the deployment opts into the `cloud-egress` component.
"""

from __future__ import annotations

import json
import math
import os
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

import httpx
import yaml

from router.policy import Access, Candidate, Policy

PROBE_TIMEOUT = 5.0
TOKEN_TIMEOUT = 10.0
# An OIDC token with no `expires_in` is assumed to live five minutes, the usual
# default for client-credentials grants.
TOKEN_TTL_S = 300.0
# Renew early: a token that expires in flight turns a retry into a 401.
TOKEN_EXPIRY_MARGIN_S = 60.0
# A provider that issues very short tokens must still get a cache, or every
# request would pay for an exchange.
TOKEN_MIN_TTL_S = 5.0
DEFAULT_TOKEN_DIR = "/app/backend-tokens"
REFRESHABLE_SOURCES = frozenset({"file", "oidc"})


class CredentialError(RuntimeError):
    """A credential could not be read, loaded or exchanged.

    The message names the key and, at most, an HTTP status. It deliberately
    carries no secret, no token, no header value and no response body: it reaches
    a log line and, worse, a 503 detail a tenant can read.
    """


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
    """Static credential headers. Rotating and OIDC sources need a client, so
    they go through `BackendAccess.headers` instead."""
    if access is None or access.source != "static":
        return {}
    value = keys.get(access.key)
    if not value:
        return {}
    return {access.header: f"{access.prefix}{value}"}


@dataclass
class BackendAccess:
    """Every credential source for one gateway process.

    The OIDC token cache is per process and per key: one gateway replica does
    not share a token with another, and a rotation is picked up by whichever
    process notices first. That is acceptable for short-lived tokens and stated
    in ADR-0010 rather than hidden.
    """

    keys: Mapping[str, str] = field(repr=False)
    ca_path: str | None = None
    token_dir: str = DEFAULT_TOKEN_DIR
    clock: Callable[[], float] = time.monotonic
    _tokens: dict[str, tuple[str, float]] = field(default_factory=dict, repr=False)

    @classmethod
    def load(
        cls,
        keys_path: str | Path | None,
        ca_path: str | Path | None,
        token_dir: str | Path | None = None,
    ) -> "BackendAccess":
        if ca_path and not Path(ca_path).is_file():
            # Falling back to system trust because a configured bundle is
            # missing would silently accept the wrong certificate.
            raise FileNotFoundError(f"backend CA bundle not found: {ca_path}")
        directory = token_dir or os.environ.get("SLM_BACKEND_TOKEN_DIR") or DEFAULT_TOKEN_DIR
        return cls(
            keys=load_backend_keys(keys_path),
            ca_path=str(ca_path) if ca_path else None,
            token_dir=str(directory),
        )

    @property
    def verify(self) -> bool | ssl.SSLContext:
        if not self.ca_path:
            return True
        return ssl.create_default_context(cafile=self.ca_path)

    def _token_path(self, name: str) -> Path | None:
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            return None
        return Path(self.token_dir) / name

    def _read_token(self, name: str) -> str | None:
        """Read a rotating token file. A projected token carries a newline, so
        the value is stripped; a file that vanishes or empties between requests
        is treated as unconfigured rather than as an empty credential."""
        path = self._token_path(name)
        if path is None:
            return None
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def ready(self, candidate: Candidate) -> bool:
        if not candidate.external:
            return True
        access = candidate.access
        assert access is not None
        if access.source == "file":
            return self._read_token(access.key) is not None
        return access.key in self.keys

    async def headers(
        self, candidate: Candidate, client: httpx.AsyncClient
    ) -> dict[str, str]:
        """The credential for this candidate, or `{}` when it is not ready.

        A file credential is read fresh on every call so a rotation needs no
        restart. An OIDC credential is exchanged once and cached until it is
        close to expiry; a failed exchange raises `CredentialError`.
        """
        access = candidate.access
        if access is None:
            return {}
        if access.source == "file":
            value = self._read_token(access.key)
            if value is None:
                return {}
            return {access.header: f"{access.prefix}{value}"}
        value = self.keys.get(access.key)
        if not value:
            return {}
        if access.source == "oidc":
            token = await self._oidc_token(access, client)
            return {access.header: f"{access.prefix}{token}"}
        return {access.header: f"{access.prefix}{value}"}

    async def refresh(self, candidate: Candidate) -> None:
        """Drop a cached OIDC token. A file token needs nothing: the next read
        sees the rotated content."""
        access = candidate.access
        if access is not None and access.source == "oidc":
            self._tokens.pop(access.key, None)

    async def _oidc_token(self, access: Access, client: httpx.AsyncClient) -> str:
        now = self.clock()
        cached = self._tokens.get(access.key)
        if cached is not None and cached[1] > now:
            return cached[0]

        secret = self.keys[access.key]
        form = {
            "grant_type": "client_credentials",
            "client_id": access.client_id or "",
            "client_secret": secret,
        }
        if access.scope:
            form["scope"] = access.scope
        try:
            response = await client.post(
                access.token_url or "", data=form, timeout=TOKEN_TIMEOUT
            )
        except httpx.HTTPError as exc:
            # The request carried the client secret; the exception text is not
            # repeated here, and `from None` keeps the chained request out of
            # the traceback.
            raise CredentialError(
                f"token request for key '{access.key}' failed: {type(exc).__name__}"
            ) from None
        if response.status_code >= 400:
            raise CredentialError(
                f"token request for key '{access.key}' failed: HTTP {response.status_code}"
            )
        try:
            body = response.json()
        except ValueError:
            raise CredentialError(
                f"token response for key '{access.key}' was not JSON"
            ) from None
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise CredentialError(
                f"token response for key '{access.key}' carried no access_token"
            )

        ttl = TOKEN_TTL_S
        raw_ttl = body.get("expires_in") if isinstance(body, dict) else None
        if raw_ttl is not None:
            try:
                ttl = float(raw_ttl)
            except (TypeError, ValueError):
                ttl = TOKEN_TTL_S
            if not math.isfinite(ttl):
                ttl = TOKEN_TTL_S
        lifetime = max(TOKEN_MIN_TTL_S, ttl - TOKEN_EXPIRY_MARGIN_S)
        token = token.strip()
        self._tokens[access.key] = (token, now + lifetime)
        return token

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


async def open_authenticated(
    client: httpx.AsyncClient,
    candidate: Candidate,
    access: BackendAccess,
    payload: dict,
    stream: bool,
) -> "httpx.Response":
    """Open the upstream with current credentials, refreshing once on a 401.

    A rotating file token and an OIDC token can both expire between the probe and
    the request. A 401 from either is worth exactly one retry with a fresh
    credential; a second 401 is an authentication failure like any other. A
    static key is never retried: the value cannot change without a restart, so a
    second request would fail the same way.
    """
    headers = await access.headers(candidate, client)
    response = await open_upstream(client, candidate, payload, stream, headers=headers)
    source = candidate.access.source if candidate.access is not None else "static"
    if response.status_code != 401 or source not in REFRESHABLE_SOURCES:
        return response
    await response.aread()
    await response.aclose()
    await access.refresh(candidate)
    headers = await access.headers(candidate, client)
    return await open_upstream(client, candidate, payload, stream, headers=headers)


async def probe(
    client: httpx.AsyncClient,
    candidate: Candidate,
    headers: Mapping[str, str] | None = None,
    timeout: float = PROBE_TIMEOUT,
    access: BackendAccess | None = None,
) -> bool:
    """One health check. Transport error or a 5xx means the backend is down.

    A 4xx still proves the host answered and TLS verified, so it counts as up;
    an expired key surfaces as a request error, not a silent removal.

    Pass `headers` when the caller already holds a credential (the gateway does,
    so one exchange serves the probe and the next request). Pass `access` when
    it does not: the headers then come from the same async path as a request,
    and a credential that cannot be obtained counts as down.
    """
    if headers is None and access is not None and candidate.external:
        try:
            headers = await access.headers(candidate, client)
        except CredentialError:
            return False
    try:
        response = await client.get(
            f"{candidate.backend}/health",
            headers=dict(headers or {}),
            timeout=timeout,
        )
    except httpx.HTTPError:
        return False
    return response.status_code < 500
