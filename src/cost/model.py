"""What an owned GPU costs per token, and when it beats the alternatives.

The routing policy carries marginal prices for CPU and cloud. The on-prem GPU
has `cost_per_1k_out: 0.0` there because there is no per-token invoice; its cost
is capacity that was bought. This module converts owned hardware into the same
unit — dollars per million output tokens — at a stated utilisation.

Inputs are explicit:
  - capex amortised over its life,
  - wall power at the PUE and the local tariff,
  - an operations overhead,
  - and the aggregate output throughput measured by the benchmark at the
    benchmark's own prompt length.

The last point matters: the number is only as good as the measured throughput,
and the throughput is from a laptop RTX 4080. It calibrates the model, it is not
a datacentre capacity plan.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from tiersim.profiles import PROFILES, TierProfile

HOURS_PER_MONTH = 730


@dataclass(frozen=True)
class OwnedGpu:
    name: str
    capex_usd: float
    amortisation_months: float
    power_w: float
    pue: float
    electricity_usd_per_kwh: float
    ops_overhead_pct: float
    utilisation: float
    fallback_output_tps: float
    bench_path: str = ""
    bench_label_prefix: str = "gpu-direct"
    bench_latency_label: str = "gpu-direct c=1"


def hourly_cost(hw: OwnedGpu) -> float:
    depreciation = hw.capex_usd / (hw.amortisation_months * HOURS_PER_MONTH)
    power = (
        hw.power_w / 1000 * hw.pue * hw.electricity_usd_per_kwh
    )
    return round((depreciation + power) * (1 + hw.ops_overhead_pct / 100), 4)


def cost_per_1m_output(hourly_usd: float, output_tps: float, utilisation: float) -> float:
    if output_tps <= 0:
        raise ValueError("output throughput must be positive")
    if not 0 < utilisation <= 1:
        raise ValueError("utilisation must be in (0, 1]")
    return round(hourly_usd * 1_000_000 / (output_tps * 3600 * utilisation), 4)


def tier_cost_per_request(
    profile: TierProfile, prompt_tokens: int, output_tokens: int
) -> float:
    return (
        prompt_tokens / 1000 * profile.input_cost_per_1k_usd
        + output_tokens / 1000 * profile.output_cost_per_1k_usd
    )


def per_request_to_1m(cost_per_request: float, output_tokens: int) -> float:
    if output_tokens <= 0:
        raise ValueError("output tokens must be positive")
    return round(cost_per_request / output_tokens * 1_000_000, 4)


def _load_bench(path: str) -> dict:
    if not path:
        return {}
    file = Path(path)
    if not file.exists():
        return {}
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _measured(bench: dict, prefix: str) -> dict:
    runs = [
        run
        for run in bench.get("runs", [])
        if run.get("label", "").startswith(prefix) and run.get("source") == "measured"
    ]
    if not runs:
        return {}
    best = max(runs, key=lambda run: run.get("output_tokens_per_s") or 0)
    return {
        "output_tps": best.get("output_tokens_per_s"),
        "concurrency": best.get("concurrency"),
        "requests_per_s": best.get("requests_per_s"),
    }


def _latency(bench: dict, label: str) -> dict:
    for run in bench.get("runs", []):
        if run.get("label") == label:
            return {
                "ttft_ms": (run.get("ttft_ms") or {}).get("p50"),
                "total_ms": (run.get("total_ms") or {}).get("p50"),
                "concurrency": run.get("concurrency"),
            }
    return {}


def build_report(config: dict, utilisation: float | None = None) -> dict:
    workload = config["workload"]
    prompt_tokens = int(workload["prompt_tokens"])
    output_tokens = int(workload["output_tokens"])
    requests_per_day = int(workload["requests_per_day"])

    raw = config["owned_gpu"]
    hw = OwnedGpu(**raw)
    if utilisation is not None:
        hw = OwnedGpu(**{**raw, "utilisation": utilisation})

    bench = _load_bench(hw.bench_path)
    measured = _measured(bench, hw.bench_label_prefix)
    output_tps = float(measured.get("output_tps") or hw.fallback_output_tps)
    latency = _latency(bench, hw.bench_latency_label)

    hourly = hourly_cost(hw)
    monthly = round(hourly * HOURS_PER_MONTH, 2)
    gpu_1m = cost_per_1m_output(hourly, output_tps, hw.utilisation)
    gpu_request = (
        round(monthly / (requests_per_day * 30), 6) if requests_per_day else None
    )
    gpu_capacity = int(output_tps * hw.utilisation * 86400 / output_tokens)
    full_capacity = output_tps * 86400 / output_tokens
    workload_tokens_month = output_tokens * requests_per_day * 30
    workload_utilisation = (
        round(requests_per_day / full_capacity, 4) if full_capacity else 0.0
    )
    # At low volume the fixed monthly bill is spread over few tokens; this is
    # the number that should be compared with the per-token tiers.
    gpu_at_workload = (
        round(monthly * 1_000_000 / workload_tokens_month, 4)
        if workload_tokens_month
        else None
    )

    tiers = [
        {
            "tier": "gpu-owned",
            "source": "measured" if measured else "MODELLED fallback",
            "output_tps": round(output_tps, 2),
            "usd_per_1m_output": gpu_1m,
            "usd_per_1m_at_workload": gpu_at_workload,
            "usd_per_request": gpu_request,
            "usd_per_month_at_workload": monthly,
            "requests_per_day_capacity": gpu_capacity,
            "utilisation": hw.utilisation,
            "latency": latency,
            "notes": "capacity cost, not a per-token invoice",
        }
    ]

    for name in ("cpu-xeon-4b", "gpu-l4-4b"):
        profile = PROFILES[name]
        per_request = tier_cost_per_request(profile, prompt_tokens, output_tokens)
        per_1m = per_request_to_1m(per_request, output_tokens)
        tiers.append(
            {
                "tier": profile.name,
                "source": "MODELLED",
                "output_tps": round(profile.decode_tokens_per_s, 2),
                "usd_per_1m_output": per_1m,
                "usd_per_1m_at_workload": per_1m,
                "usd_per_request": round(per_request, 6),
                "usd_per_month_at_workload": round(
                    per_request * requests_per_day * 30, 2
                ),
                # The profiles price tokens, not capacity; a request/day figure
                # here would invent a batching assumption the profile never made.
                "requests_per_day_capacity": None,
                "utilisation": None,
                "latency": _latency(
                    bench,
                    "cpu-sim c=1" if profile.name == "cpu-xeon-4b" else "cloud-sim c=1",
                ),
                "notes": profile.notes,
            }
        )

    sweep = [
        {
            "utilisation": u,
            "usd_per_1m_output": cost_per_1m_output(hourly, output_tps, u),
            "requests_per_day_capacity": int(output_tps * u * 86400 / output_tokens),
        }
        for u in (0.2, 0.35, 0.5, 0.7, 0.9)
    ]

    break_even = []
    for tier in tiers[1:]:
        cost_per_request = tier["usd_per_request"]
        break_even.append(
            {
                "tier": tier["tier"],
                "requests_per_day": round(monthly / (cost_per_request * 30), 0)
                if cost_per_request
                else None,
                "owned_fits": (
                    monthly / (cost_per_request * 30) <= full_capacity
                    if cost_per_request
                    else False
                ),
            }
        )

    return {
        "version": config.get("version"),
        "workload": {
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "requests_per_day": requests_per_day,
            "output_tokens_per_day": output_tokens * requests_per_day,
        },
        "owned_gpu": {
            **asdict(hw),
            "hourly_usd": hourly,
            "monthly_usd": monthly,
            "output_tps": round(output_tps, 2),
            "full_capacity_requests_per_day": int(full_capacity),
            "workload_utilisation": workload_utilisation,
            "capacity_shortfall": requests_per_day > full_capacity,
            "throughput_source": measured or {"fallback": hw.fallback_output_tps},
        },
        "tiers": tiers,
        "utilisation_sweep": sweep,
        "break_even_vs_owned": break_even,
        "method": (
            "owned $/1M output = hourly cost * 1e6 / (measured output tps * 3600 "
            "* utilisation); modelled tiers use their per-1k input/output prices at "
            f"{prompt_tokens}:{output_tokens} input:output"
        ),
    }
