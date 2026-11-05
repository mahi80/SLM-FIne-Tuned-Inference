"""Connectivity harness for external backends.

    PYTHONPATH=src python -m tools.check_backend [--policy PATH] [--keys PATH]
                                        [--ca PATH] [--timeout 5]

Prints one line per external candidate, across every alias, and exits non-zero
when one of them cannot be used. The line carries the alias, the candidate, the
backend host, the key *name*, the credential source, whether it is configured,
the probe result and the latency. It never carries a key value, a token or a
header.

This answers the question a policy review cannot: given the Secret and CA this
cluster actually mounts, can the gateway authenticate to each external candidate
and is the endpoint answering? It calls `/health`, which is the same endpoint the
probe loop uses, so a 4xx counts as reachable and a 5xx or transport error does
not.

Environment fallbacks match the gateway: POLICY_FILE, SLM_BACKEND_KEYS,
SLM_BACKEND_CA and SLM_BACKEND_TOKEN_DIR.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from gateway.backends import BackendAccess, CredentialError, probe
from router.policy import Candidate, Policy, load_policy

TIMEOUT = 5.0

UP = "up"
DOWN = "down"
AUTH_ERROR = "auth-error"
UNCONFIGURED = "unconfigured"


@dataclass(frozen=True)
class Check:
    alias: str
    candidate: str
    host: str
    key: str
    source: str
    configured: bool
    outcome: str
    latency_ms: float

    @property
    def failed(self) -> bool:
        return self.outcome != UP


def format_line(check: Check) -> str:
    state = "OK  " if not check.failed else "FAIL"
    configured = "configured" if check.configured else "unconfigured"
    return (
        f"{state} alias={check.alias} candidate={check.candidate} host={check.host} "
        f"key={check.key} source={check.source} {configured} "
        f"probe={check.outcome} latency_ms={check.latency_ms:.1f}"
    )


def exit_code(checks: list[Check]) -> int:
    """1 when any candidate is unconfigured, cannot authenticate or is down."""
    return 1 if any(check.failed for check in checks) else 0


def host_of(candidate: Candidate) -> str:
    parts = urlsplit(candidate.backend)
    return parts.netloc or candidate.backend


async def check_candidate(
    client: httpx.AsyncClient,
    alias: str,
    candidate: Candidate,
    access: BackendAccess,
    timeout: float = TIMEOUT,
) -> Check:
    block = candidate.access
    assert block is not None
    host = host_of(candidate)
    started = time.perf_counter()
    if not access.ready(candidate):
        return Check(
            alias, candidate.name, host, block.key, block.source, False, UNCONFIGURED, 0.0
        )
    try:
        headers = await access.headers(candidate, client)
    except CredentialError:
        # The exception text names the key and, at most, a status. It is not
        # printed: the outcome column says everything an operator needs, and the
        # gateway log holds the detail.
        elapsed = (time.perf_counter() - started) * 1000
        return Check(alias, candidate.name, host, block.key, block.source, True, AUTH_ERROR, elapsed)
    healthy = await probe(client, candidate, headers, timeout)
    elapsed = (time.perf_counter() - started) * 1000
    return Check(
        alias,
        candidate.name,
        host,
        block.key,
        block.source,
        True,
        UP if healthy else DOWN,
        elapsed,
    )


async def check_all(
    client: httpx.AsyncClient,
    policy: Policy,
    access: BackendAccess,
    timeout: float = TIMEOUT,
) -> list[Check]:
    """Every external candidate, deduplicated by alias and candidate."""
    checks: list[Check] = []
    seen: set[tuple[str, str]] = set()
    for alias in policy.aliases.values():
        for candidate in alias.candidates.values():
            if not candidate.external:
                continue
            marker = (alias.name, candidate.name)
            if marker in seen:
                continue
            seen.add(marker)
            checks.append(
                await check_candidate(client, alias.name, candidate, access, timeout)
            )
    return checks


async def run(argv: list[str] | None = None, client: httpx.AsyncClient | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.check_backend",
        description="Authenticate to and probe every external backend candidate.",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path(os.environ.get("POLICY_FILE", "/app/config/policy.yaml")),
        help="routing policy (default: $POLICY_FILE)",
    )
    parser.add_argument(
        "--keys",
        default=os.environ.get("SLM_BACKEND_KEYS", "/app/backend-keys/keys.yaml"),
        help="mounted keys file (default: $SLM_BACKEND_KEYS)",
    )
    parser.add_argument(
        "--ca",
        default=os.environ.get("SLM_BACKEND_CA") or None,
        help="CA bundle for private chains (default: $SLM_BACKEND_CA)",
    )
    parser.add_argument(
        "--timeout", type=float, default=TIMEOUT, help="probe timeout in seconds"
    )
    args = parser.parse_args(argv)

    try:
        policy = load_policy(str(args.policy))
        access = BackendAccess.load(args.keys, args.ca)
    except (OSError, ValueError) as exc:
        print(f"FAIL {exc}")
        return 1

    if client is not None:
        checks = await check_all(client, policy, access, args.timeout)
    else:
        async with httpx.AsyncClient(timeout=args.timeout, verify=access.verify) as owned:
            checks = await check_all(owned, policy, access, args.timeout)

    if not checks:
        print(f"no external candidates in {args.policy}")
        return 0
    for check in checks:
        print(format_line(check))
    return exit_code(checks)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main())
