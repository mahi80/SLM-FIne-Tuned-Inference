#!/usr/bin/env python3
"""End-to-end smoke test for the GPU serving tier.

Measures streaming time-to-first-token and decode throughput, then checks that
the model can produce strict JSON. Exit code is non-zero on any failure so this
can be used as a CI gate.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("VLLM_URL", "http://localhost:8010").rstrip("/")
MODEL = os.environ.get("SERVED_MODEL_NAME", "slm-small")

JSON_PROMPT = (
    "Extract fields as strict JSON with keys customer, product, sentiment. "
    "Text: 'Acme says the X200 pump is leaking after two weeks and they are furious.' "
    "Return JSON only, no prose."
)


def _fail(message: str) -> None:
    print(f"FAIL  {message}")
    sys.exit(1)


def get_json(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def chat(payload: dict, stream: bool) -> tuple[dict, float | None]:
    """Return (merged response, ttft seconds)."""
    body = dict(payload, stream=stream)
    request = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    started = time.perf_counter()
    if not stream:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8")), None

    ttft = None
    text: list[str] = []
    usage: dict = {}
    with urllib.request.urlopen(request, timeout=300) as response:
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
            delta = chunk["choices"][0].get("delta", {}) if chunk.get("choices") else {}
            if delta.get("content"):
                if ttft is None:
                    ttft = time.perf_counter() - started
                text.append(delta["content"])

    elapsed = time.perf_counter() - started
    return (
        {
            "content": "".join(text),
            "usage": usage,
            "elapsed": elapsed,
        },
        ttft,
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print(f"endpoint  {BASE}")
    try:
        models = get_json("/v1/models")
    except urllib.error.URLError as exc:
        _fail(f"cannot reach {BASE}: {exc}")
    served = [m["id"] for m in models.get("data", [])]
    print(f"models    {served}")
    if MODEL not in served:
        _fail(f"expected served model '{MODEL}', got {served}")

    reply, ttft = chat(
        {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "You are a terse assistant."},
                {"role": "user", "content": "Explain KV cache reuse in two sentences."},
            ],
            "max_tokens": 128,
            "temperature": 0.2,
            "stream": True,
            "stream_options": {"include_usage": True},
        },
        stream=True,
    )
    usage = reply.get("usage") or {}
    completion_tokens = usage.get("completion_tokens") or 0
    decode_window = reply["elapsed"] - (ttft or 0)
    tok_per_s = completion_tokens / decode_window if decode_window > 0 else 0.0
    print(f"ttft      {ttft:.3f} s" if ttft else "ttft      n/a")
    print(f"decode    {tok_per_s:.1f} tok/s over {completion_tokens} tokens")
    print(f"latency   {reply['elapsed']:.3f} s total")

    structured, _ = chat(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": JSON_PROMPT}],
            "max_tokens": 120,
            "temperature": 0.0,
            "stream": False,
        },
        stream=False,
    )
    content = structured["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        _fail(f"model did not return valid JSON: {content[:200]!r}")
    print(f"json      {json.dumps(parsed, ensure_ascii=False)[:160]}")
    print("PASS")


if __name__ == "__main__":
    main()
