#!/usr/bin/env python3
"""End-to-end check of the fine-tuned adapters through the gateway.

Three fixed tickets at temperature 0. Verifies that every alias returns strict
JSON with the exact contract keys, that v2 labels the adversarial cases
correctly, and that traffic reaches the GPU tier. Exit code is non-zero on
failure.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8020").rstrip("/")
API_KEY = os.environ.get("TENANT_KEY", "dev-acme-key")

EXPECTED_KEYS = {"category", "priority", "sentiment", "escalate", "product", "summary"}

TICKETS = {
    "sarcasm": (
        "Fantastic work on the update. The X200 pump at the north line has been "
        "down all morning and the line is stopped. Truly world-class reliability.",
        {"category": "technical", "priority": "P2", "sentiment": "negative", "escalate": True},
    ),
    "feature-request": (
        "Would be great if the GateLink controller export supported Parquet. "
        "Not urgent at all, just a nice-to-have.",
        {"category": "technical", "priority": "P4", "sentiment": "positive", "escalate": False},
    ),
    "calm-outage": (
        "No drama, but the ThermoQ sensor at plant 2 stopped at 05:00 and the "
        "line is blocked. Could someone take a look when you get a chance?",
        {"category": "technical", "priority": "P2"},
    ),
}


def _fail(message: str) -> None:
    print(f"FAIL  {message}")
    sys.exit(1)


def chat(model: str, ticket: str) -> tuple[dict, dict]:
    from finetune.prompt import prompt_messages

    body = {
        "model": model,
        "messages": prompt_messages(ticket),
        "temperature": 0.0,
        "max_tokens": 220,
    }
    request = urllib.request.Request(
        GATEWAY + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            headers = {k.lower(): v for k, v in response.headers.items()}
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        _fail(f"{model} returned HTTP {exc.code}: {exc.read()[:200]!r}")
    return headers, payload


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, ".")
    print(f"gateway   {GATEWAY}")

    with urllib.request.urlopen(
        urllib.request.Request(
            GATEWAY + "/v1/models", headers={"Authorization": f"Bearer {API_KEY}"}
        ),
        timeout=30,
    ) as response:
        aliases = [m["id"] for m in json.loads(response.read())["data"]]
    for required in ("slm-small", "slm-triage", "slm-triage-v2"):
        if required not in aliases:
            _fail(f"alias '{required}' missing from /v1/models: {aliases}")
    print(f"models    {aliases}")

    labels: dict[str, dict[str, dict]] = {}
    for name, (ticket, expected) in TICKETS.items():
        labels[name] = {}
        for alias in ("slm-small", "slm-triage", "slm-triage-v2"):
            headers, payload = chat(alias, ticket)
            if headers.get("x-tier") != "gpu":
                _fail(f"{alias} reported tier {headers.get('x-tier')}")
            try:
                answer = json.loads(payload["choices"][0]["message"]["content"])
            except (KeyError, json.JSONDecodeError):
                _fail(f"{alias} did not return parseable JSON for '{name}'")
            if set(answer) != EXPECTED_KEYS:
                _fail(f"{alias} returned wrong keys for '{name}': {sorted(answer)}")
            labels[name][alias] = answer

    for name, (_, expected) in TICKETS.items():
        answer = labels[name]["slm-triage-v2"]
        for field, want in expected.items():
            if answer[field] != want:
                _fail(f"v2 {name}: {field}={answer[field]!r}, expected {want!r}")

    for name in TICKETS:
        print(f"{name:15} base={labels[name]['slm-small']['priority']}/{labels[name]['slm-small']['sentiment']} "
              f"v1={labels[name]['slm-triage']['priority']}/{labels[name]['slm-triage']['sentiment']} "
              f"v2={labels[name]['slm-triage-v2']['priority']}/{labels[name]['slm-triage-v2']['sentiment']}")
    print("PASS")


if __name__ == "__main__":
    main()
