"""Closed-loop load generator.

Each worker holds one request in flight, so `--concurrency` is the number of
simultaneous clients rather than a batch size. That is the shape that exposes
queueing in the modelled tiers: once concurrency passes the tier's slot count,
latency grows and throughput stops climbing.

Streaming requests measure time-to-first-token separately from total latency;
non-streaming requests report TTFT equal to total latency because the first
byte is the whole answer.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Sample:
    status: int
    total_ms: float
    ttft_ms: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    simulated: bool
    tier: str | None
    error: str | None = None


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; avoids interpolating a claim the data cannot make."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return round(ordered[rank - 1], 2)


async def _parse_sse(
    lines, request_started: float
) -> tuple[float | None, int | None, int | None]:
    """Return (ttft_ms, prompt_tokens, completion_tokens) from an SSE stream."""
    ttft = None
    prompt_tokens = None
    completion_tokens = 0
    async for line in lines:
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        usage = event.get("usage")
        if usage:
            prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
            completion_tokens = usage.get("completion_tokens", completion_tokens)
            continue
        for choice in event.get("choices") or []:
            content = (choice.get("delta") or {}).get("content")
            if content:
                if ttft is None:
                    ttft = (time.perf_counter() - request_started) * 1000
                completion_tokens += 1
    return (
        round(ttft, 2) if ttft is not None else None,
        prompt_tokens,
        completion_tokens,
    )


async def _request(
    client,
    url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    stream: bool,
    api_key: str | None,
) -> Sample:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}

    started = time.perf_counter()
    try:
        if stream:
            async with client.stream(
                "POST", f"{url}/v1/chat/completions", json=body, headers=headers
            ) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", "replace")
                    return Sample(
                        status=response.status_code,
                        total_ms=round((time.perf_counter() - started) * 1000, 2),
                        ttft_ms=None,
                        prompt_tokens=None,
                        completion_tokens=None,
                        simulated=response.headers.get("x-simulated") == "true",
                        tier=response.headers.get("x-tier"),
                        error=text[:200],
                    )
                ttft, prompt_tokens, completion_tokens = await _parse_sse(
                    response.aiter_lines(), started
                )
                return Sample(
                    status=response.status_code,
                    total_ms=round((time.perf_counter() - started) * 1000, 2),
                    ttft_ms=ttft,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    simulated=response.headers.get("x-simulated") == "true",
                    tier=response.headers.get("x-tier"),
                )

        response = await client.post(
            f"{url}/v1/chat/completions", json=body, headers=headers
        )
        total_ms = round((time.perf_counter() - started) * 1000, 2)
        if response.status_code >= 400:
            return Sample(
                status=response.status_code,
                total_ms=total_ms,
                ttft_ms=None,
                prompt_tokens=None,
                completion_tokens=None,
                simulated=response.headers.get("x-simulated") == "true",
                tier=response.headers.get("x-tier"),
                error=response.text[:200],
            )
        usage = response.json().get("usage") or {}
        return Sample(
            status=response.status_code,
            total_ms=total_ms,
            ttft_ms=total_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            simulated=response.headers.get("x-simulated") == "true",
            tier=response.headers.get("x-tier"),
        )
    except Exception as exc:  # keep the run going; an error is a measurement too
        return Sample(
            status=0,
            total_ms=round((time.perf_counter() - started) * 1000, 2),
            ttft_ms=None,
            prompt_tokens=None,
            completion_tokens=None,
            simulated=False,
            tier=None,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )


async def _worker(client, queue: asyncio.Queue, samples: list[Sample], **kwargs) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        samples.append(await _request(client, **kwargs))


async def run_target(
    label: str,
    base_url: str,
    model: str,
    concurrency: int,
    requests: int,
    max_tokens: int,
    stream: bool,
    warmup: int,
    prompt: str,
    api_key: str | None = None,
    timeout: float = 180.0,
) -> dict:
    """Run one target at one concurrency level and return a report fragment."""
    import httpx

    limits = httpx.Limits(
        max_connections=concurrency, max_keepalive_connections=concurrency
    )
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        for _ in range(warmup):
            await _request(
                client, base_url, model, prompt, max_tokens, stream, api_key
            )

        queue: asyncio.Queue = asyncio.Queue()
        for _ in range(requests):
            queue.put_nowait(None)
        samples: list[Sample] = []
        started = time.perf_counter()
        await asyncio.gather(
            *(
                _worker(
                    client,
                    queue,
                    samples,
                    url=base_url,
                    model=model,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    stream=stream,
                    api_key=api_key,
                )
                for _ in range(concurrency)
            )
        )
        elapsed = time.perf_counter() - started

    summary = summarize(samples, elapsed)
    return {
        "label": label,
        "model": model,
        "base_url": base_url,
        "concurrency": concurrency,
        "requests": requests,
        "max_tokens": max_tokens,
        "stream": stream,
        "warmup": warmup,
        "started_at": int(time.time()),
        **summary,
    }


def summarize(samples: list[Sample], elapsed_s: float) -> dict:
    ok = [s for s in samples if s.error is None and 200 <= s.status < 300]
    errors = len(samples) - len(ok)
    totals = [s.total_ms for s in ok]
    ttfts = [s.ttft_ms for s in ok if s.ttft_ms is not None]
    output_tokens = sum(s.completion_tokens or 0 for s in ok)
    input_tokens = sum(s.prompt_tokens or 0 for s in ok)
    simulated = bool(ok) and all(s.simulated for s in ok)
    tiers = {s.tier for s in ok if s.tier}
    return {
        "n": len(samples),
        "ok": len(ok),
        "errors": errors,
        "error_rate": round(errors / max(1, len(samples)), 4),
        "elapsed_s": round(elapsed_s, 3),
        "requests_per_s": round(len(ok) / elapsed_s, 3) if elapsed_s else 0.0,
        "output_tokens_per_s": (
            round(output_tokens / elapsed_s, 2) if elapsed_s else 0.0
        ),
        "input_tokens_per_s": (
            round(input_tokens / elapsed_s, 2) if elapsed_s else 0.0
        ),
        "prompt_tokens_avg": (
            round(input_tokens / len(ok), 1) if ok else None
        ),
        "completion_tokens_avg": (
            round(output_tokens / len(ok), 1) if ok else None
        ),
        "ttft_ms": {
            "p50": percentile(ttfts, 50),
            "p95": percentile(ttfts, 95),
            "p99": percentile(ttfts, 99),
        },
        "total_ms": {
            "p50": percentile(totals, 50),
            "p95": percentile(totals, 95),
            "p99": percentile(totals, 99),
        },
        "source": "MODELLED" if simulated else "measured",
        "simulated": simulated,
        "tier": sorted(tiers)[0] if len(tiers) == 1 else (sorted(tiers) if tiers else None),
        "failures": [asdict(s) for s in samples if s.error is not None][:5],
    }
