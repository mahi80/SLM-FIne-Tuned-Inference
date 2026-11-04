"""Calibrated profiles for compute tiers that are not physically present.

Every number here is MODELLED, not measured. The profiles exist so that routing,
queueing and cost behaviour can be exercised on a single-GPU workstation without
pretending a 32-vCPU node is available.

Derivation, for the CPU profile:

    32 vCPU Xeon at ~$0.95/h, 50% utilisation, ~60 output tok/s aggregate
    (8 concurrent streams at 4B Q4_K_M, memory-bandwidth bound)
    -> 108k output tokens/h -> ~$8.8 per 1M output tokens
    -> $0.0088 per 1k output tokens

    Prefill is far cheaper per token than decode -> $0.0009 per 1k input tokens.

The GPU profile uses the same arithmetic for an L4 at ~$0.60/h and ~500 output
tok/s aggregate. The ~25x cost gap per output token is the reason the router must
treat CPU as a capacity tier for latency-tolerant work, not as a cheap GPU.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TierProfile:
    name: str
    tier: str
    hardware: str
    prefill_tokens_per_s: float
    decode_tokens_per_s: float
    max_concurrency: int
    input_cost_per_1k_usd: float
    output_cost_per_1k_usd: float
    notes: str


PROFILES: dict[str, TierProfile] = {
    "cpu-xeon-4b": TierProfile(
        name="cpu-xeon-4b",
        tier="cpu",
        hardware="32 vCPU Intel Xeon (AMX/AVX-512)",
        prefill_tokens_per_s=850.0,
        decode_tokens_per_s=17.0,
        max_concurrency=8,
        input_cost_per_1k_usd=0.0009,
        output_cost_per_1k_usd=0.0088,
        notes="MODELLED: 4B Q4_K_M, decode is memory-bandwidth bound",
    ),
    "gpu-l4-4b": TierProfile(
        name="gpu-l4-4b",
        tier="cloud",
        hardware="1x NVIDIA L4 24GB",
        prefill_tokens_per_s=5200.0,
        decode_tokens_per_s=95.0,
        max_concurrency=32,
        input_cost_per_1k_usd=0.00006,
        output_cost_per_1k_usd=0.00033,
        notes="MODELLED: 4B AWQ, continuous batching at moderate utilisation",
    ),
}


def get_profile(name: str) -> TierProfile:
    try:
        return PROFILES[name]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown tier profile '{name}'; known: {known}") from None
