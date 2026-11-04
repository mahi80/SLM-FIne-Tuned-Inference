"""Simulated compute tier (CPU or cloud, selected by profile).

Speaks the same OpenAI-compatible contract as the GPU tier and borrows the GPU
tier's timing shape: prefill first, then one decode step per token, with a bounded
semaphore that produces real queueing when concurrency exceeds the profile.

The response body is canned. The *timing* is the product — it is what lets the
router, the dashboards and the cost model be exercised before a CPU node exists.
Every response carries `x-simulated: true`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from .profiles import get_profile

PROFILE = get_profile(os.environ.get("TIERSIM_PROFILE", "cpu-xeon-4b"))
MODEL_NAME = os.environ.get("TIERSIM_MODEL_NAME", "slm-small")
SPEEDUP = float(os.environ.get("TIERSIM_SPEEDUP", "1"))
MAX_TOKENS_CAP = int(os.environ.get("TIERSIM_MAX_TOKENS", "512"))
STARTED_AT = int(time.time())

REQUESTS = Counter(
    "tiersim_requests_total", "Simulated requests", ["profile", "stream", "status"]
)
QUEUE_WAIT = Histogram(
    "tiersim_queue_wait_seconds",
    "Time spent waiting for a concurrency slot",
    ["profile"],
    buckets=(0.001, 0.01, 0.05, 0.1, 0.5, 1, 2.5, 5, 10, 30),
)
TOKENS = Counter("tiersim_tokens_total", "Simulated tokens", ["profile", "direction"])
INFLIGHT = Gauge("tiersim_inflight_requests", "Requests holding a slot", ["profile"])
SIM_COST = Counter(
    "tiersim_simulated_cost_usd_total", "Modelled cost of simulated requests", ["profile"]
)

SEMAPHORE = asyncio.Semaphore(PROFILE.max_concurrency)
app = FastAPI(title="SLM tier simulator", version="0.1.0")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def build_answer(prompt: str, max_tokens: int) -> list[str]:
    """Return ~max_tokens word-level pieces, shaped by the prompt."""
    if "json" in prompt.lower():
        words = json.dumps(
            {"customer": "Acme", "product": "X200 pump", "sentiment": "negative"}
        ).split(" ")
    else:
        words = (
            f"This response comes from the simulated {PROFILE.tier} tier. The timing "
            "follows a calibrated prefill and decode profile for a four billion parameter "
            f"model on {PROFILE.hardware}, so latency and queueing behave like the real "
            "tier without requiring the hardware to be present. "
        ).split() * 6
    return words[:max_tokens]


def sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "simulated": True,
        "profile": PROFILE.name,
        "model": MODEL_NAME,
        "max_concurrency": PROFILE.max_concurrency,
    }


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/models")
async def models() -> dict:
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "created": STARTED_AT,
                "owned_by": "tiersim",
                "simulated": True,
                "profile": PROFILE.name,
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    messages = payload.get("messages") or []
    prompt_text = " ".join(
        str(m.get("content", "")) for m in messages if isinstance(m, dict)
    )
    prompt_tokens = estimate_tokens(prompt_text)
    max_tokens = int(
        payload.get("max_completion_tokens") or payload.get("max_tokens") or 128
    )
    max_tokens = max(1, min(max_tokens, MAX_TOKENS_CAP))
    pieces = build_answer(prompt_text, max_tokens)
    stream = bool(payload.get("stream"))
    want_usage = bool((payload.get("stream_options") or {}).get("include_usage"))
    completion_id = f"chatcmpl-sim-{uuid.uuid4().hex[:12]}"

    wait_started = time.perf_counter()
    await SEMAPHORE.acquire()
    queue_wait = time.perf_counter() - wait_started
    QUEUE_WAIT.labels(PROFILE.name).observe(queue_wait)
    INFLIGHT.labels(PROFILE.name).inc()

    prefill_seconds = prompt_tokens / (PROFILE.prefill_tokens_per_s * SPEEDUP)
    step_seconds = 1.0 / (PROFILE.decode_tokens_per_s * SPEEDUP)
    cost_usd = (
        prompt_tokens / 1000 * PROFILE.input_cost_per_1k_usd
        + len(pieces) / 1000 * PROFILE.output_cost_per_1k_usd
    )
    TOKENS.labels(PROFILE.name, "input").inc(prompt_tokens)
    SIM_COST.labels(PROFILE.name).inc(cost_usd)

    headers = {
        "x-simulated": "true",
        "x-tier": PROFILE.tier,
        "x-sim-profile": PROFILE.name,
        "x-sim-queue-wait-ms": f"{queue_wait * 1000:.1f}",
        "x-simulated-cost-usd": f"{cost_usd:.8f}",
    }

    def release() -> None:
        INFLIGHT.labels(PROFILE.name).dec()
        SEMAPHORE.release()

    if not stream:
        try:
            await asyncio.sleep(prefill_seconds + step_seconds * len(pieces))
        finally:
            release()
        TOKENS.labels(PROFILE.name, "output").inc(len(pieces))
        REQUESTS.labels(PROFILE.name, "false", "200").inc()
        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": payload.get("model", MODEL_NAME),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": " ".join(pieces),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": len(pieces),
                    "total_tokens": prompt_tokens + len(pieces),
                },
            },
            headers=headers,
        )

    async def relay():
        try:
            await asyncio.sleep(prefill_seconds)
            for index, piece in enumerate(pieces):
                await asyncio.sleep(step_seconds)
                yield sse(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": payload.get("model", MODEL_NAME),
                        "system_fingerprint": f"sim-{PROFILE.name}",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": (" " if index else "") + piece},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            yield sse(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": payload.get("model", MODEL_NAME),
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
            if want_usage:
                yield sse(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": payload.get("model", MODEL_NAME),
                        "choices": [],
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": len(pieces),
                            "total_tokens": prompt_tokens + len(pieces),
                        },
                    }
                )
            yield b"data: [DONE]\n\n"
            TOKENS.labels(PROFILE.name, "output").inc(len(pieces))
            REQUESTS.labels(PROFILE.name, "true", "200").inc()
        except asyncio.CancelledError:
            REQUESTS.labels(PROFILE.name, "true", "cancelled").inc()
            raise
        finally:
            release()

    return StreamingResponse(
        relay(), media_type="text/event-stream", headers=headers
    )
