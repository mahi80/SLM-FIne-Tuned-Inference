"""OpenAI-compatible gateway in front of the compute tiers.

Phase 3 replaced the static alias table with a policy engine:

* admission (tenant, residency, data class, context budget) before scoring;
* one primary per alias, deterministic canaries, standbys only when the
  primary is not admissible;
* per-candidate circuit breakers, half-open probing, transport fallback;
* hash-chained audit records, one per request plus one per shadow call.

Routing decisions are explained on demand so a canary or veto can be
reproduced without reading the audit file by hand.
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
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from router import AuditLog, PolicyStore, breaker_keys, breakers
from router.policy import Candidate, Policy

from . import metrics
from .auth import TenantRegistry
from .config import Tenant, load_tenants

POLICY_FILE = os.environ.get("POLICY_FILE", "/app/config/policy.yaml")
TENANTS_FILE = os.environ.get("TENANTS_FILE", "/app/config/tenants.yaml")
AUDIT_FILE = os.environ.get("AUDIT_FILE", "/var/lib/slm/audit.jsonl")

store = PolicyStore(POLICY_FILE)
tenant_list = load_tenants(TENANTS_FILE)
for tenant in tenant_list:
    unknown = set(tenant.allowed_aliases) - set(store.policy.aliases)
    if unknown:
        raise ValueError(f"tenant '{tenant.id}' allows unknown aliases {sorted(unknown)}")
tenants = TenantRegistry(tenant_list)
audit = AuditLog(AUDIT_FILE)

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
    app.state.shadow_tasks = set()
    try:
        yield
    finally:
        for task in list(app.state.shadow_tasks):
            task.cancel()
        if app.state.shadow_tasks:
            await asyncio.gather(*app.state.shadow_tasks, return_exceptions=True)
        await app.state.client.aclose()
        audit.close()


app = FastAPI(title="SLM serving gateway", version="0.2.0", lifespan=lifespan)


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


def current_admin(tenant: Tenant = Depends(current_tenant)) -> Tenant:
    if not tenant.admin:
        raise HTTPException(status_code=403, detail="admin role required")
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


def update_breaker(alias: str, candidate: str) -> None:
    state = store.breakers.state(breakers.key(alias, candidate))
    metrics.BREAKER_STATE.labels(alias, candidate).set(breakers.STATE_VALUES[state])


for _alias in store.policy.aliases.values():
    for _candidate in _alias.candidates.values():
        update_breaker(_alias.name, _candidate.name)


def write_audit(payload: dict) -> None:
    try:
        audit.append(payload)
    except (OSError, ValueError):
        # Losing an audit record is worse than losing a request, but a full
        # disk must not take inference down with it. Loud, not silent.
        logger.exception("audit append failed")


def simulated_headers(upstream: httpx.Response) -> dict[str, str]:
    names = (
        "x-simulated",
        "x-simulated-cost-usd",
        "x-sim-profile",
        "x-sim-queue-wait-ms",
    )
    return {name: upstream.headers[name] for name in names if name in upstream.headers}


def record(
    *,
    state: dict,
    decision,
    candidate: Candidate | None,
    tenant: Tenant,
    request_id: str,
    alias: str,
    stream: bool,
    upstream_headers: dict[str, str],
) -> None:
    elapsed = time.perf_counter() - state["started"]
    tier = candidate.tier if candidate else "none"
    metrics.REQUESTS.labels(alias, tier, state["status"]).inc()
    metrics.REQUEST_DURATION.labels(alias, tier).observe(elapsed)
    if state["ttft"] is not None:
        metrics.TTFT.labels(alias, tier).observe(state["ttft"])
    metrics.TOKENS.labels(alias, tier, "input").inc(state["prompt_tokens"])
    metrics.TOKENS.labels(alias, tier, "output").inc(state["completion_tokens"])
    if state["inflight"]:
        metrics.INFLIGHT.labels(alias, tier).dec()
    if state["fallback"]:
        metrics.FALLBACKS.labels(alias, tier).inc()
    if candidate is None and decision.refusal:
        metrics.POLICY_REFUSALS.labels(alias, decision.refusal).inc()
    estimated_cost = None
    if candidate is not None:
        estimated_cost = round(
            state["completion_tokens"] / 1000 * candidate.cost_per_1k_out, 8
        )
    write_audit(
        {
            "event": "inference",
            "request_id": request_id,
            "tenant": tenant.id,
            "alias": alias,
            "tier": tier,
            "candidate": candidate.name if candidate else None,
            "decision": decision.kind,
            "policy_version": decision.policy_version,
            "upstream_model": candidate.upstream_model if candidate else None,
            "model_version": candidate.model_version if candidate else None,
            "fallback": state["fallback"],
            "stream": stream,
            "status": state["status"],
            "prompt_tokens": state["prompt_tokens"],
            "completion_tokens": state["completion_tokens"],
            "latency_ms": round(elapsed * 1000, 1),
            "ttft_ms": round(state["ttft"] * 1000, 1) if state["ttft"] else None,
            "simulated": upstream_headers.get("x-simulated") == "true",
            "cost_usd": upstream_headers.get("x-simulated-cost-usd"),
            "est_cost_usd": estimated_cost,
            "attempts": [item.name for item in decision.attempts],
            "vetoed": [
                f"{item.candidate}:{item.reason}"
                for item in decision.considered
                if not item.admitted
            ],
            "canary_bucket": decision.bucket,
            "canary_hit": decision.canary_hit,
            "shadow": decision.shadow.name if decision.shadow else None,
            "refusal": decision.refusal,
        }
    )


async def open_upstream(
    client: httpx.AsyncClient, candidate: Candidate, payload: dict, stream: bool
) -> httpx.Response:
    request = client.build_request(
        "POST",
        f"{candidate.backend}/v1/chat/completions",
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    return await client.send(request, stream=stream)


async def run_shadow(
    *,
    client: httpx.AsyncClient,
    alias: str,
    candidate: Candidate,
    cap: int,
    body: dict,
    tenant: Tenant,
    request_id: str,
    policy_version: str,
    primary: str,
) -> None:
    requested = body.get("max_tokens") or body.get("max_completion_tokens") or cap
    payload = {
        key: value
        for key, value in body.items()
        if key not in {"stream", "stream_options", "max_completion_tokens"}
    }
    payload["model"] = candidate.upstream_model
    payload["stream"] = False
    payload["max_tokens"] = max(1, min(cap, int(requested)))
    status = "error"
    completion_tokens = 0
    started = time.perf_counter()
    try:
        response = await open_upstream(client, candidate, payload, stream=False)
        raw = await response.aread()
        await response.aclose()
        status = str(response.status_code)
        if response.status_code == 200:
            try:
                data = json.loads(raw)
                completion_tokens = int((data.get("usage") or {}).get("completion_tokens") or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                status = "invalid-json"
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except httpx.HTTPError:
        status = "error"
    finally:
        metrics.SHADOW.labels(alias, candidate.name, status).inc()
        write_audit(
            {
                "event": "shadow",
                "request_id": request_id,
                "tenant": tenant.id,
                "alias": alias,
                "candidate": candidate.name,
                "primary_candidate": primary,
                "policy_version": policy_version,
                "upstream_model": candidate.upstream_model,
                "model_version": candidate.model_version,
                "status": status,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "completion_tokens": completion_tokens,
            }
        )


def schedule_shadow(
    app: FastAPI,
    *,
    body: dict,
    decision,
    tenant: Tenant,
    request_id: str,
) -> None:
    spec = store.policy.aliases[decision.alias].shadow
    if decision.shadow is None or spec is None or decision.chosen is None:
        return
    task = asyncio.create_task(
        run_shadow(
            client=app.state.client,
            alias=decision.alias,
            candidate=decision.shadow,
            cap=spec.max_tokens,
            body=body,
            tenant=tenant,
            request_id=request_id,
            policy_version=decision.policy_version,
            primary=decision.chosen.name,
        )
    )
    app.state.shadow_tasks.add(task)
    task.add_done_callback(app.state.shadow_tasks.discard)


def policy_view(policy: Policy) -> dict:
    def candidate_view(candidate: Candidate) -> dict:
        return {
            "name": candidate.name,
            "tier": candidate.tier,
            "compute": candidate.compute,
            "deployment": candidate.deployment,
            "pool": candidate.pool,
            "backend": candidate.backend,
            "upstream_model": candidate.upstream_model,
            "model_version": candidate.model_version,
            "region": candidate.region,
            "serves_regions": list(candidate.serves_regions),
            "sovereign": candidate.sovereign,
            "priority": candidate.priority,
            "max_context": candidate.max_context,
            "cost_per_1k_out": candidate.cost_per_1k_out,
        }

    aliases = {}
    for alias in policy.aliases.values():
        entry = {
            "description": alias.description,
            "primary": alias.primary,
            "candidates": [candidate_view(c) for c in alias.candidates.values()],
            "canary": None,
            "shadow": None,
        }
        if alias.canary is not None:
            entry["canary"] = {
                "candidate": alias.canary.candidate,
                "percent": alias.canary.percent,
                "sticky": alias.canary.sticky,
            }
        if alias.shadow is not None:
            entry["shadow"] = {
                "candidate": alias.shadow.candidate,
                "percent": alias.shadow.percent,
                "max_tokens": alias.shadow.max_tokens,
            }
        aliases[alias.name] = entry
    return {
        "version": policy.version,
        "path": str(store.path),
        "breaker": {
            "failures": policy.breaker.failures,
            "window_s": policy.breaker.window_s,
            "open_s": policy.breaker.open_s,
        },
        "aliases": aliases,
        "breakers": store.breakers.snapshot(),
    }


@app.get("/health")
async def health() -> dict:
    snapshot = store.breakers.snapshot()
    open_count = sum(1 for item in snapshot.values() if item["state"] == breakers.OPEN)
    half_open = sum(1 for item in snapshot.values() if item["state"] == breakers.HALF_OPEN)
    return {
        "status": "ok",
        "aliases": len(store.policy.aliases),
        "policy_version": store.policy.version,
        "breakers": {"open": open_count, "half_open": half_open},
    }


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    return metrics.render()


@app.get("/v1/models")
async def list_models() -> dict:
    data = []
    for alias in store.policy.aliases.values():
        primary = alias.candidates[alias.primary]
        item = {
            "id": alias.name,
            "object": "model",
            "owned_by": "slm-fabric",
            "tier": primary.tier,
            "description": alias.description,
            "primary_version": primary.model_version,
        }
        if alias.canary is not None:
            item["canary"] = {
                "version": alias.candidates[alias.canary.candidate].model_version,
                "percent": alias.canary.percent,
            }
        data.append(item)
    return {"object": "list", "data": data}


@app.get("/v1/policy")
async def get_policy(_: Tenant = Depends(current_admin)) -> dict:
    return policy_view(store.policy)


@app.post("/v1/policy/explain")
async def explain_policy(
    request: Request, tenant: Tenant = Depends(current_tenant)
) -> dict:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    alias = body.get("model")
    if not isinstance(alias, str) or not alias:
        raise HTTPException(status_code=400, detail="'model' is required")
    if store.policy.alias(alias) is None:
        raise HTTPException(status_code=404, detail=f"unknown model '{alias}'")
    try:
        prompt_tokens = body.get("prompt_tokens")
        if prompt_tokens is None:
            prompt_tokens = estimate_prompt_tokens(body.get("messages") or [])
        prompt_tokens = max(1, int(prompt_tokens))
        max_tokens = int(body.get("max_tokens") or 0)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="'prompt_tokens' and 'max_tokens' must be integers"
        ) from None
    required_tokens = prompt_tokens + max(0, max_tokens)
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    decision = store.engine.decide(alias, tenant, request_id, required_tokens)
    return {
        "tenant": tenant.id,
        "required_tokens": required_tokens,
        **decision.explain(),
    }


@app.post("/v1/policy/reload")
async def reload_policy(_: Tenant = Depends(current_admin)) -> dict:
    before = breaker_keys(store.policy)
    try:
        policy = store.reload()
    except (ValueError, TypeError, OSError, yaml.YAMLError) as exc:
        metrics.POLICY_RELOADS.labels("rejected").inc()
        raise HTTPException(status_code=422, detail=f"policy reload rejected: {exc}") from None
    metrics.POLICY_RELOADS.labels("ok").inc()
    for stale in before - breaker_keys(policy):
        alias, _, candidate = stale.partition(":")
        metrics.BREAKER_STATE.remove(alias, candidate)
    for alias in policy.aliases.values():
        for candidate in alias.candidates.values():
            update_breaker(alias.name, candidate.name)
    return {
        "version": policy.version,
        "aliases": len(policy.aliases),
        "breakers": store.breakers.snapshot(),
    }


@app.get("/v1/audit/verify")
async def verify_audit(_: Tenant = Depends(current_admin)) -> JSONResponse:
    result = audit.verify()
    return JSONResponse(content=result, status_code=200 if result["ok"] else 500)


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
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    alias = body.get("model")
    if not isinstance(alias, str) or not alias:
        raise HTTPException(status_code=400, detail="'model' is required")
    if store.policy.alias(alias) is None:
        raise HTTPException(status_code=404, detail=f"unknown model '{alias}'")

    messages = body.get("messages") or []
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="'messages' must be an array")
    options = body.get("stream_options")
    if options is not None and not isinstance(options, dict):
        raise HTTPException(status_code=400, detail="'stream_options' must be an object")
    options = options or {}

    stream = bool(body.get("stream"))
    client_requested_usage = bool(options.get("include_usage"))
    if stream:
        body["stream_options"] = {**options, "include_usage": True}

    prompt_tokens = estimate_prompt_tokens(messages)
    try:
        max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="'max_tokens' must be an integer") from None
    required_tokens = prompt_tokens + max(0, max_tokens)

    decision = store.engine.decide(alias, tenant, request_id, required_tokens)
    metrics.ROUTE_DECISIONS.labels(alias, decision.kind).inc()
    for candidate in decision.attempts:
        update_breaker(alias, candidate.name)

    state = {
        "started": time.perf_counter(),
        "ttft": None,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": 0,
        "status": "200",
        "fallback": False,
        "usage_seen": False,
        "inflight": False,
    }

    if decision.chosen is None:
        state["status"] = "503"
        record(
            state=state,
            decision=decision,
            candidate=None,
            tenant=tenant,
            request_id=request_id,
            alias=alias,
            stream=stream,
            upstream_headers={},
        )
        reasons = "; ".join(
            f"{item.candidate}:{item.reason}"
            for item in decision.considered
            if not item.admitted
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"no route satisfies policy for '{alias}' ({decision.refusal}): "
                f"{reasons or 'no candidates'}"
            ),
        )

    schedule_shadow(
        request.app, body=body, decision=decision, tenant=tenant, request_id=request_id
    )

    client: httpx.AsyncClient = request.app.state.client
    used: Candidate | None = None
    upstream: httpx.Response | None = None
    last_error = "no candidate attempted"

    for index, candidate in enumerate(decision.attempts):
        key = breakers.key(alias, candidate.name)
        if not store.breakers.allow(key):
            update_breaker(alias, candidate.name)
            last_error = f"{candidate.name}: circuit open"
            continue
        try:
            upstream = await open_upstream(
                client, candidate, dict(body, model=candidate.upstream_model), stream
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            store.breakers.record_failure(key)
            update_breaker(alias, candidate.name)
            last_error = f"{candidate.name}: {type(exc).__name__}"
            upstream = None
            continue
        if upstream.status_code >= 500:
            store.breakers.record_failure(key)
            update_breaker(alias, candidate.name)
            last_error = f"{candidate.name}: HTTP {upstream.status_code}"
            await upstream.aread()
            await upstream.aclose()
            upstream = None
            continue
        store.breakers.record_success(key)
        update_breaker(alias, candidate.name)
        used = candidate
        state["fallback"] = index > 0
        break

    if upstream is None or used is None:
        state["status"] = "503"
        record(
            state=state,
            decision=decision,
            candidate=None,
            tenant=tenant,
            request_id=request_id,
            alias=alias,
            stream=stream,
            upstream_headers={},
        )
        raise HTTPException(
            status_code=503, detail=f"no backend available for '{alias}': {last_error}"
        )

    metrics.INFLIGHT.labels(alias, used.tier).inc()
    state["inflight"] = True
    if decision.kind == "canary" and not state["fallback"]:
        metrics.CANARY.labels(alias, used.name).inc()

    sim_headers = simulated_headers(upstream)
    reason = f"policy:{decision.kind}:{used.name}"
    if state["fallback"]:
        reason += ":fallback"
    headers = {
        "x-request-id": request_id,
        "x-tier": used.tier,
        "x-route-alias": alias,
        "x-route-reason": reason,
        "x-policy-version": decision.policy_version,
        "x-fallback": "true" if state["fallback"] else "false",
        "x-canary": "true" if decision.kind == "canary" and not state["fallback"] else "false",
        "x-model-version": used.model_version,
        **sim_headers,
    }
    if decision.shadow is not None:
        headers["x-shadow"] = decision.shadow.name

    def finish(status: str, upstream_headers: dict[str, str]) -> None:
        state["status"] = status
        record(
            state=state,
            decision=decision,
            candidate=used,
            tenant=tenant,
            request_id=request_id,
            alias=alias,
            stream=stream,
            upstream_headers=upstream_headers,
        )

    if upstream.status_code >= 400:
        raw = await upstream.aread()
        await upstream.aclose()
        finish(str(upstream.status_code), sim_headers)
        try:
            content = json.loads(raw)
        except json.JSONDecodeError:
            content = {"detail": raw.decode("utf-8", errors="replace")[:500]}
        return JSONResponse(content=content, status_code=upstream.status_code)

    if not stream:
        raw = await upstream.aread()
        await upstream.aclose()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            finish("502", sim_headers)
            raise HTTPException(status_code=502, detail="upstream returned non-JSON") from None
        usage = data.get("usage") or {}
        state["prompt_tokens"] = usage.get("prompt_tokens", state["prompt_tokens"])
        state["completion_tokens"] = usage.get("completion_tokens", 0)
        finish("200", sim_headers)
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
        except asyncio.CancelledError:
            state["status"] = "499"
            raise
        except httpx.HTTPError:
            store.breakers.record_failure(breakers.key(alias, used.name))
            update_breaker(alias, used.name)
            state["status"] = "502"
            raise
        finally:
            if not state["usage_seen"]:
                state["completion_tokens"] = streamed_tokens
            await upstream.aclose()
            finish(state["status"], sim_headers)

    return StreamingResponse(
        relay(), media_type="text/event-stream", headers={**headers, **STREAM_HEADERS}
    )
