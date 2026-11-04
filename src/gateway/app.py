"""OpenAI-compatible gateway in front of the compute tiers.

Responsibilities in Phase 1 (deliberately narrow):

* authenticate tenants and enforce per-tenant rate limits;
* resolve a model alias to a backend, with one fallback attempt on connect failure;
* proxy streaming and non-streaming completions without buffering;
* account tokens, latency, time-to-first-token and cost;
* emit one structured audit record per request.

Routing is still a static table. The policy engine that chooses a tier per request
arrives in Phase 3 and will populate the same `x-route-reason` contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import metrics
from .auth import TenantRegistry
from .config import Route, Tenant, load_routes, load_tenants

ROUTES_FILE = os.environ.get("ROUTES_FILE", "/app/config/routes.yaml")
TENANTS_FILE = os.environ.get("TENANTS_FILE", "/app/config/tenants.yaml")

route_table = load_routes(ROUTES_FILE)
tenants = TenantRegistry(load_tenants(TENANTS_FILE))

logger = logging.getLogger("slm.audit")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
    logger.propagate = False

TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)
STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(
        timeout=TIMEOUT,
        limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
    )
    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(title="SLM serving gateway", version="0.1.0", lifespan=lifespan)


def current_tenant(request: Request) -> Tenant:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    tenant = tenants.resolve(token.strip())
    if tenant is None:
        raise HTTPException(
            status_code=401, detail="invalid api key", headers={"WWW-Authenticate": "Bearer"}
        )
    return tenant


def estimate_prompt_tokens(messages: list) -> int:
    chars = 0
    for message in messages:
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, list):
            chars += sum(
                len(str(part.get("text", "")))
                for part in content
                if isinstance(part, dict)
            )
        else:
            chars += len(str(content))
    return max(1, chars // 4)


def build_chain(route: Route) -> list[Route]:
    chain = [route]
    if route.fallback:
        fallback = route_table.get(route.fallback)
        if fallback is not None:
            chain.append(fallback)
    return chain


def simulated_headers(upstream: httpx.Response) -> dict[str, str]:
    names = (
        "x-simulated",
        "x-simulated-cost-usd",
        "x-sim-queue-wait-ms",
        "x-sim-profile",
    )
    return {name: upstream.headers[name] for name in names if name in upstream.headers}


def record(
    *,
    state: dict,
    route: Route,
    tenant: Tenant,
    request_id: str,
    alias: str,
    stream: bool,
    upstream_headers: dict[str, str],
) -> None:
    elapsed = time.perf_counter() - state["started"]
    tier = route.tier
    metrics.REQUESTS.labels(alias, tier, state["status"]).inc()
    metrics.REQUEST_DURATION.labels(alias, tier).observe(elapsed)
    if state["ttft"] is not None:
        metrics.TTFT.labels(alias, tier).observe(state["ttft"])
    metrics.TOKENS.labels(alias, tier, "input").inc(state["prompt_tokens"])
    metrics.TOKENS.labels(alias, tier, "output").inc(state["completion_tokens"])
    metrics.INFLIGHT.labels(alias, tier).dec()
    if state["fallback"]:
        metrics.FALLBACKS.labels(alias, tier).inc()
    logger.info(
        json.dumps(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "event": "inference",
                "request_id": request_id,
                "tenant": tenant.id,
                "alias": alias,
                "tier": tier,
                "upstream_model": route.upstream_model,
                "fallback": state["fallback"],
                "stream": stream,
                "status": state["status"],
                "prompt_tokens": state["prompt_tokens"],
                "completion_tokens": state["completion_tokens"],
                "latency_ms": round(elapsed * 1000, 1),
                "ttft_ms": round(state["ttft"] * 1000, 1) if state["ttft"] else None,
                "simulated": upstream_headers.get("x-simulated") == "true",
                "cost_usd": upstream_headers.get("x-simulated-cost-usd"),
            }
        )
    )


async def open_upstream(
    client: httpx.AsyncClient, route: Route, payload: dict, stream: bool
) -> httpx.Response:
    request = client.build_request(
        "POST",
        f"{route.backend}/v1/chat/completions",
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    return await client.send(request, stream=stream)


def error_response(upstream: httpx.Response, raw: bytes) -> JSONResponse:
    try:
        content = json.loads(raw)
    except json.JSONDecodeError:
        content = {"detail": raw.decode("utf-8", errors="replace")[:500]}
    return JSONResponse(content=content, status_code=upstream.status_code)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "routes": len(route_table.routes),
        "policy_version": f"static-v{route_table.version}",
    }


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    return metrics.render()


@app.get("/v1/models")
async def list_models() -> dict:
    return {
        "object": "list",
        "data": [
            {
                "id": route.alias,
                "object": "model",
                "owned_by": "slm-fabric",
                "tier": route.tier,
                "description": route.description,
            }
            for route in route_table.routes.values()
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request, tenant: Tenant = Depends(current_tenant)
):
    if not tenants.allow(tenant):
        raise HTTPException(
            status_code=429,
            detail="tenant rate limit exceeded",
            headers={"Retry-After": "1"},
        )

    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None

    alias = body.get("model")
    if not alias:
        raise HTTPException(status_code=400, detail="'model' is required")
    route = route_table.get(alias)
    if route is None:
        raise HTTPException(status_code=404, detail=f"unknown model '{alias}'")

    stream = bool(body.get("stream"))
    client_requested_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    if stream:
        options = dict(body.get("stream_options") or {})
        options["include_usage"] = True
        body["stream_options"] = options

    state = {
        "started": time.perf_counter(),
        "ttft": None,
        "prompt_tokens": estimate_prompt_tokens(body.get("messages") or []),
        "completion_tokens": 0,
        "status": "200",
        "fallback": False,
        "usage_seen": False,
    }

    client: httpx.AsyncClient = request.app.state.client
    used_route: Route | None = None
    upstream: httpx.Response | None = None
    chain = build_chain(route)
    last_error: Exception | None = None

    for index, candidate in enumerate(chain):
        payload = dict(body, model=candidate.upstream_model)
        try:
            upstream = await open_upstream(client, candidate, payload, stream)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            last_error = exc
            continue
        used_route = candidate
        state["fallback"] = index > 0
        break

    if upstream is None or used_route is None:
        metrics.REQUESTS.labels(alias, route.tier, "503").inc()
        raise HTTPException(
            status_code=503, detail=f"no backend available for '{alias}': {last_error}"
        )

    metrics.INFLIGHT.labels(alias, used_route.tier).inc()
    sim_headers = simulated_headers(upstream)
    headers = {
        "x-request-id": request_id,
        "x-tier": used_route.tier,
        "x-route-alias": used_route.alias,
        "x-route-reason": f"static-alias:{alias}->{used_route.alias}",
        "x-policy-version": f"static-v{route_table.version}",
        "x-fallback": "true" if state["fallback"] else "false",
        **sim_headers,
    }

    if upstream.status_code >= 400:
        raw = await upstream.aread()
        await upstream.aclose()
        state["status"] = str(upstream.status_code)
        record(
            state=state,
            route=used_route,
            tenant=tenant,
            request_id=request_id,
            alias=alias,
            stream=stream,
            upstream_headers=sim_headers,
        )
        return error_response(upstream, raw)

    if not stream:
        raw = await upstream.aread()
        await upstream.aclose()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            state["status"] = "502"
            record(
                state=state,
                route=used_route,
                tenant=tenant,
                request_id=request_id,
                alias=alias,
                stream=stream,
                upstream_headers=sim_headers,
            )
            raise HTTPException(status_code=502, detail="upstream returned non-JSON") from None
        usage = data.get("usage") or {}
        state["prompt_tokens"] = usage.get("prompt_tokens", state["prompt_tokens"])
        state["completion_tokens"] = usage.get("completion_tokens", 0)
        record(
            state=state,
            route=used_route,
            tenant=tenant,
            request_id=request_id,
            alias=alias,
            stream=stream,
            upstream_headers=sim_headers,
        )
        return JSONResponse(content=data, headers=headers)

    async def relay():
        streamed_tokens = 0
        try:
            async for line in upstream.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    yield b"data: [DONE]\n\n"
                    return
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                usage = chunk.get("usage")
                if usage:
                    state["usage_seen"] = True
                    state["prompt_tokens"] = usage.get(
                        "prompt_tokens", state["prompt_tokens"]
                    )
                    state["completion_tokens"] = usage.get(
                        "completion_tokens", state["completion_tokens"]
                    )
                choices = chunk.get("choices") or []
                if not choices and usage and not client_requested_usage:
                    continue
                if choices:
                    delta = choices[0].get("delta") or {}
                    if delta.get("content"):
                        if state["ttft"] is None:
                            state["ttft"] = time.perf_counter() - state["started"]
                        streamed_tokens += 1
                yield f"data: {json.dumps(chunk)}\n\n".encode()
        except (httpx.HTTPError, asyncio.CancelledError):
            state["status"] = "499"
            raise
        finally:
            if not state["usage_seen"]:
                state["completion_tokens"] = streamed_tokens
            await upstream.aclose()
            record(
                state=state,
                route=used_route,
                tenant=tenant,
                request_id=request_id,
                alias=alias,
                stream=True,
                upstream_headers=sim_headers,
            )

    return StreamingResponse(
        relay(), media_type="text/event-stream", headers={**headers, **STREAM_HEADERS}
    )
