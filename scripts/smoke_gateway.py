#!/usr/bin/env python3
"""End-to-end check through the gateway, both tiers.

Verifies auth, the alias table, streaming on the GPU tier, the simulated CPU
tier, and that the gateway is exporting metrics. Exit code is non-zero on any
failure so it can serve as a CI gate.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8020").rstrip("/")
API_KEY = os.environ.get("TENANT_KEY", "dev-acme-key")
GPU_ALIAS = os.environ.get("GPU_ALIAS", "slm-small")
CPU_ALIAS = os.environ.get("CPU_ALIAS", "slm-small-cpu")


def _fail(message: str) -> None:
    print(f"FAIL  {message}")
    sys.exit(1)


def http(path: str, body: dict | None = None, key: str | None = API_KEY,
         timeout: float = 300.0) -> tuple[int, dict, dict]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(GATEWAY + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, dict(response.headers), json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"detail": raw.decode("utf-8", errors="replace")[:200]}
        return exc.code, dict(exc.headers), payload


def stream_chat(alias: str, max_tokens: int = 48) -> dict:
    body = {
        "model": alias,
        "messages": [{"role": "user", "content": "Describe tiered inference briefly."}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": True,
    }
    request = urllib.request.Request(
        GATEWAY + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )
    started = time.perf_counter()
    ttft = None
    pieces: list[str] = []
    usage: dict = {}
    with urllib.request.urlopen(request, timeout=300) as response:
        headers = dict(response.headers)
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                if delta.get("content"):
                    if ttft is None:
                        ttft = time.perf_counter() - started
                    pieces.append(delta["content"])
    return {
        "headers": headers,
        "text": "".join(pieces),
        "ttft": ttft,
        "elapsed": time.perf_counter() - started,
        "usage": usage,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print(f"gateway   {GATEWAY}")

    status, _, health = http("/health")
    if status != 200:
        _fail(f"/health returned {status}: {health}")
    print(f"health    ok ({health.get('aliases')} aliases, {health.get('policy_version')})")

    status, _, models = http("/v1/models")
    aliases = [item["id"] for item in models.get("data", [])]
    for required in (GPU_ALIAS, CPU_ALIAS):
        if required not in aliases:
            _fail(f"alias '{required}' missing from /v1/models: {aliases}")
    print(f"models    {aliases}")

    status, _, payload = http("/v1/chat/completions", {"model": GPU_ALIAS,
                                                       "messages": []}, key=None)
    if status != 401:
        _fail(f"unauthenticated request returned {status}, expected 401: {payload}")
    print("auth      401 without a bearer token")

    gpu = stream_chat(GPU_ALIAS)
    if not gpu["text"].strip():
        _fail("GPU tier returned no content")
    if gpu["headers"].get("x-tier") != "gpu":
        _fail(f"GPU route reported tier {gpu['headers'].get('x-tier')}")
    if not gpu["headers"].get("x-policy-version"):
        _fail("GPU route did not report a policy version")
    print(
        f"gpu       {gpu['headers'].get('x-tier')} tier, "
        f"ttft {gpu['ttft']:.3f}s, {len(gpu['text'].split())} words, "
        f"reason {gpu['headers'].get('x-route-reason')}"
    )

    cpu = stream_chat(CPU_ALIAS)
    if not cpu["text"].strip():
        _fail("CPU tier returned no content")
    if cpu["headers"].get("x-tier") != "cpu":
        _fail(f"CPU route reported tier {cpu['headers'].get('x-tier')}")
    if cpu["headers"].get("x-simulated") != "true":
        _fail("CPU tier did not label itself as simulated")
    print(
        f"cpu       {cpu['headers'].get('x-tier')} tier, "
        f"ttft {cpu['ttft']:.3f}s, simulated={cpu['headers'].get('x-simulated')}, "
        f"cost=${cpu['headers'].get('x-simulated-cost-usd')}"
    )

    request = urllib.request.Request(GATEWAY + "/metrics")
    with urllib.request.urlopen(request, timeout=30) as response:
        metrics = response.read().decode("utf-8")
    if "gateway_requests_total" not in metrics:
        _fail("/metrics did not expose gateway_requests_total")
    counted = sum(
        float(line.rsplit(" ", 1)[1])
        for line in metrics.splitlines()
        if line.startswith("gateway_requests_total{")
    )
    print(f"metrics   gateway_requests_total={counted:.0f}")
    print("PASS")


if __name__ == "__main__":
    main()
