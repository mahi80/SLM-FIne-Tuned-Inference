"""Prometheus instrumentation for the gateway."""

from __future__ import annotations

from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

REQUESTS = Counter(
    "gateway_requests_total",
    "Requests handled, by alias, tier and status class",
    ["alias", "tier", "status"],
)
REQUEST_DURATION = Histogram(
    "gateway_request_duration_seconds",
    "End-to-end request duration",
    ["alias", "tier"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
TTFT = Histogram(
    "gateway_ttft_seconds",
    "Time to first streamed token",
    ["alias", "tier"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 15, 60),
)
TOKENS = Counter(
    "gateway_tokens_total",
    "Tokens proxied, by direction",
    ["alias", "tier", "direction"],
)
INFLIGHT = Gauge(
    "gateway_inflight_requests",
    "Requests currently being proxied",
    ["alias", "tier"],
)
FALLBACKS = Counter(
    "gateway_fallback_total",
    "Requests served by a fallback route",
    ["alias", "tier"],
)
ROUTE_DECISIONS = Counter(
    "gateway_route_decisions_total",
    "Routing decisions, by alias and kind",
    ["alias", "kind"],
)
POLICY_REFUSALS = Counter(
    "gateway_policy_refusals_total",
    "Requests with no admissible candidate",
    ["alias", "reason"],
)
CANARY = Counter(
    "gateway_canary_requests_total",
    "Requests served by a canary candidate",
    ["alias", "candidate"],
)
SHADOW = Counter(
    "gateway_shadow_requests_total",
    "Shadow requests, by outcome",
    ["alias", "candidate", "status"],
)
BREAKER_STATE = Gauge(
    "gateway_breaker_state",
    "Circuit breaker state: 0 closed, 1 half-open, 2 open",
    ["alias", "candidate"],
)
POLICY_RELOADS = Counter(
    "gateway_policy_reloads_total",
    "Policy reload attempts",
    ["result"],
)


def render() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
