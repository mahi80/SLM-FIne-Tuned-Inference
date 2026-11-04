#!/usr/bin/env python3
"""Policy smoke test: admission, residency, canary determinism, audit chain.

Read-only. The outage drill (stop the GPU tier and watch the router move
traffic) is documented in the README and is deliberately not part of this
script: it stops a container.

Exit code is non-zero on failure.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8020").rstrip("/")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "dev-acme-key")
TENANT_KEY = os.environ.get("TENANT_KEY", "dev-globex-key")


def _fail(message: str) -> None:
    print(f"FAIL  {message}")
    sys.exit(1)


def http(path: str, body: dict | None = None, key: str | None = ADMIN_KEY) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(GATEWAY + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"detail": raw.decode("utf-8", errors="replace")[:200]}
        return exc.code, payload


def explain(model: str, key: str, prompt_tokens: int = 100) -> dict:
    status, payload = http(
        "/v1/policy/explain", {"model": model, "prompt_tokens": prompt_tokens}, key=key
    )
    if status != 200:
        _fail(f"explain {model} returned {status}: {payload}")
    return payload


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"gateway   {GATEWAY}")

    status, health = http("/health", key=None)
    if status != 200 or not health.get("policy_version"):
        _fail(f"/health returned {status}: {health}")
    print(f"health    {health.get('aliases')} aliases, policy {health.get('policy_version')}")

    status, policy = http("/v1/policy")
    if status != 200 or not policy.get("aliases"):
        _fail(f"/v1/policy returned {status}: {policy}")
    print(f"policy    {len(policy['aliases'])} aliases, breaker {policy['breaker']}")

    acme = explain("slm-small", ADMIN_KEY)
    if acme["chosen"] != "gpu":
        _fail(f"acme slm-small chose {acme['chosen']}, expected gpu")
    vetoes = {
        item["candidate"]: item["reason"]
        for item in acme["considered"]
        if not item["admitted"]
    }
    if vetoes.get("cloud-us") != "region-veto":
        _fail(f"acme did not see the cloud region veto: {vetoes}")
    print(f"admission acme/eu   chosen={acme['chosen']} vetoed={vetoes}")

    denied = explain("slm-small-cpu", TENANT_KEY)
    if denied["refusal"] != "alias-denied":
        _fail(f"globex should be denied slm-small-cpu, got {denied['refusal']}")
    print(f"admission globex    refused={denied['refusal']}")

    first = explain("slm-triage", TENANT_KEY)
    second = explain("slm-triage", TENANT_KEY)
    if (first["canary"], first["decision"]) != (second["canary"], second["decision"]):
        _fail("canary assignment is not stable for the same tenant")
    print(
        f"canary    globex bucket={first['canary']['bucket']} "
        f"percent={first['canary']['percent']} "
        f"decision={first['decision']} chosen={first['chosen']}"
    )

    status, verify = http("/v1/audit/verify")
    if status != 200 or not verify.get("ok"):
        _fail(f"audit verify failed: {verify}")
    print(f"audit     ok, {verify['records']} records, head {verify['last_hash'][:16]}...")

    status, _ = http("/v1/policy", key=TENANT_KEY)
    if status != 403:
        _fail(f"non-admin /v1/policy returned {status}, expected 403")
    print("auth      non-admin policy read is 403")
    print("PASS")


if __name__ == "__main__":
    main()
