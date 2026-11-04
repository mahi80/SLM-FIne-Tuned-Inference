"""Print and record the ownership cost model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from .model import build_report


def load_config(path: str) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("cost config must be a mapping")
    for key in ("workload", "owned_gpu"):
        if not isinstance(data.get(key), dict):
            raise ValueError(f"cost config is missing '{key}'")
    return data


def _latency(tier: dict) -> str:
    latency = tier.get("latency") or {}
    total = latency.get("total_ms")
    ttft = latency.get("ttft_ms")
    if total is None:
        return "—"
    if ttft is None:
        return f"{total:.0f} ms"
    return f"{total:.0f} ms / {ttft:.0f} ms"


def render(report: dict) -> str:
    w = report["workload"]
    lines = [
        f"Workload: {w['prompt_tokens']} input + {w['output_tokens']} output tokens, "
        f"{w['requests_per_day']:,} requests/day",
        "",
        "| Tier | Source | $/1M output (capacity) | $/1M at workload | $/request | $/month | p50 total/TTFT |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for tier in report["tiers"]:
        lines.append(
            f"| {tier['tier']} | {tier['source']} | "
            f"${tier['usd_per_1m_output']:.4f} | "
            f"${tier['usd_per_1m_at_workload']:.4f} | "
            f"${tier['usd_per_request']:.6f} | "
            f"${tier['usd_per_month_at_workload']:,.2f} | {_latency(tier)} |"
        )

    lines += [
        "",
        "Owned GPU utilisation sweep:",
        "",
        "| Utilisation | $/1M output | Requests/day capacity |",
        "|---:|---:|---:|",
    ]
    for point in report["utilisation_sweep"]:
        lines.append(
            f"| {point['utilisation']:.0%} | ${point['usd_per_1m_output']:.4f} | "
            f"{point['requests_per_day_capacity']:,} |"
        )

    lines += ["", "Break-even against owning the GPU:"]
    for entry in report["break_even_vs_owned"]:
        fits = "inside GPU capacity" if entry["owned_fits"] else "beyond GPU capacity"
        lines.append(
            f"  {entry['tier']}: {entry['requests_per_day']:,.0f} requests/day "
            f"({fits})"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/cost.yaml")
    ap.add_argument("--utilisation", type=float, default=None)
    ap.add_argument("--out", default="bench/results/cost-model.json")
    args = ap.parse_args()

    report = build_report(load_config(args.config), args.utilisation)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8", newline="\n")
    print(render(report))
    print(f"report written to {out}")


if __name__ == "__main__":
    main()
