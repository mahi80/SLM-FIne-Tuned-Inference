"""Run the benchmark matrix from config/bench.yaml."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import yaml

from .runner import run_target


def load_config(path: str) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("bench config must be a mapping")
    for key in ("prompt", "targets", "concurrency", "requests", "max_tokens"):
        if key not in data:
            raise ValueError(f"bench config is missing '{key}'")
    labels = [target["label"] for target in data["targets"]]
    if len(labels) != len(set(labels)):
        raise ValueError("bench target labels must be unique")
    for target in data["targets"]:
        for key in ("label", "base_url", "model"):
            if not isinstance(target.get(key), str) or not target[key]:
                raise ValueError(f"bench target is missing '{key}': {target}")
    probe = data.get("prefix_probe")
    if probe is not None:
        if not isinstance(probe, dict):
            raise ValueError("prefix_probe must be a mapping")
        if not isinstance(probe.get("system_prompt"), str) or not probe["system_prompt"].strip():
            raise ValueError("prefix_probe needs a non-empty 'system_prompt'")
        unknown = set(probe.get("targets") or []) - set(labels)
        if unknown:
            raise ValueError(f"prefix_probe names unknown targets: {sorted(unknown)}")
    return data


def render_markdown(report: dict) -> str:
    lines = [
        "| Target | Source | c | Requests | Req/s | Output tok/s | TTFT p50 | TTFT p95 "
        "| TPOT p50 | ITL p95 | ITL p99 | Total p50 | Total p95 | Errors |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in report["runs"]:
        ttft = run["ttft_ms"]
        total = run["total_ms"]
        tpot = run.get("tpot_ms") or {}
        itl = run.get("itl_ms") or {}
        lines.append(
            f"| {run['label']} | {run['source']} | {run['concurrency']} | "
            f"{run['ok']}/{run['n']} | {run['requests_per_s']:.2f} | "
            f"{run['output_tokens_per_s']:.1f} | "
            f"{_ms(ttft['p50'])} | {_ms(ttft['p95'])} | "
            f"{_ms(tpot.get('p50'), 1)} | {_ms(itl.get('p95'), 1)} | "
            f"{_ms(itl.get('p99'), 1)} | "
            f"{_ms(total['p50'])} | {_ms(total['p95'])} | {run['errors']} |"
        )
    probes = report.get("prefix_cache") or []
    if probes:
        lines += [
            "",
            "Prefix-cache probe (same system prompt vs a nonce that defeats reuse):",
            "",
            "| Target | Source | Prompt tokens | TTFT p50 shared | TTFT p50 unique | TTFT saved |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for probe in probes:
            saved = probe["ttft_saved_pct"]
            lines.append(
                f"| {probe['target']} | {probe['source']} | "
                f"{probe['prompt_tokens_avg'] or '—'} | "
                f"{_ms(probe['ttft_p50_shared_ms'])} | "
                f"{_ms(probe['ttft_p50_unique_ms'])} | "
                f"{'—' if saved is None else f'{saved:.0f}%'} |"
            )
    return "\n".join(lines) + "\n"


def _ms(value: float | None, digits: int = 0) -> str:
    return "—" if value is None else f"{value:.{digits}f} ms"


def summarise_prefix_probe(target: str, shared: dict, unique: dict) -> dict:
    """Compare TTFT with a reusable prefix against one that cannot be reused."""
    a = shared["ttft_ms"]["p50"]
    b = unique["ttft_ms"]["p50"]
    saved = round((b - a) / b * 100, 1) if a is not None and b else None
    return {
        "target": target,
        "source": shared["source"],
        "prompt_tokens_avg": shared.get("prompt_tokens_avg"),
        "ttft_p50_shared_ms": a,
        "ttft_p50_unique_ms": b,
        "ttft_saved_pct": saved,
    }


async def run_prefix_probe(config: dict, only: set[str] | None) -> tuple[list[dict], list[dict]]:
    """Run each probe target twice at low concurrency: shared prefix, then unique."""
    probe = config.get("prefix_probe")
    if not probe:
        return [], []
    targets = {t["label"]: t for t in config["targets"]}
    runs: list[dict] = []
    summaries: list[dict] = []
    for name in probe.get("targets") or []:
        if only and name not in only:
            continue
        target = targets[name]
        pair = {}
        for mode in ("shared", "unique"):
            label = f"{name} prefix={mode}"
            print(f"running {label}", flush=True)
            pair[mode] = await run_target(
                label=label,
                base_url=target["base_url"],
                model=target["model"],
                concurrency=int(probe.get("concurrency", 1)),
                requests=int(probe.get("requests", 10)),
                max_tokens=int(probe.get("max_tokens", 16)),
                stream=True,
                # Warm-up on the shared run is what puts the prefix in cache.
                warmup=int(probe.get("warmup", 1)),
                prompt=config["prompt"],
                api_key=target.get("api_key"),
                timeout=float(config.get("timeout_s", 180)),
                system_prompt=probe["system_prompt"],
                unique_prefix=(mode == "unique"),
            )
            runs.append(pair[mode])
        summaries.append(summarise_prefix_probe(name, pair["shared"], pair["unique"]))
    return runs, summaries


async def run_matrix(config: dict, only: set[str] | None) -> list[dict]:
    runs = []
    for target in config["targets"]:
        if only and target["label"] not in only:
            continue
        requests = int(target.get("requests", config["requests"]))
        for concurrency in config["concurrency"]:
            label = f"{target['label']} c={concurrency}"
            print(f"running {label} x{requests}", flush=True)
            run = await run_target(
                label=label,
                base_url=target["base_url"],
                model=target["model"],
                concurrency=int(concurrency),
                requests=requests,
                max_tokens=int(config["max_tokens"]),
                stream=bool(config.get("stream", True)),
                warmup=int(config.get("warmup", 0)),
                prompt=config["prompt"],
                api_key=target.get("api_key"),
                timeout=float(config.get("timeout_s", 180)),
                system_prompt=config.get("system_prompt"),
            )
            print(
                f"  {run['source']}  {run['output_tokens_per_s']:.1f} tok/s  "
                f"p50 {run['total_ms']['p50']} ms  errors {run['errors']}",
                flush=True,
            )
            runs.append(run)
    return runs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/bench.yaml")
    ap.add_argument("--out", default="bench/results/tier-comparison.json")
    ap.add_argument("--markdown", default="bench/results/tier-comparison.md")
    ap.add_argument("--only", default="", help="comma-separated target labels")
    ap.add_argument("--concurrency", default="", help="override, e.g. 1,4,12")
    ap.add_argument("--requests", type=int, default=0)
    ap.add_argument("--max-tokens", type=int, default=0)
    ap.add_argument("--no-stream", action="store_true")
    ap.add_argument(
        "--prefix-probe",
        action="store_true",
        help="also run the prefix-cache probe from config.prefix_probe",
    )
    args = ap.parse_args()

    config = load_config(args.config)
    if args.concurrency:
        config["concurrency"] = [int(v) for v in args.concurrency.split(",")]
    if args.requests:
        config["requests"] = args.requests
    if args.max_tokens:
        config["max_tokens"] = args.max_tokens
    if args.no_stream:
        config["stream"] = False

    only = {v.strip() for v in args.only.split(",") if v.strip()} or None
    runs = asyncio.run(run_matrix(config, only))
    prefix_summaries: list[dict] = []
    if args.prefix_probe:
        probe_runs, prefix_summaries = asyncio.run(run_prefix_probe(config, only))
        runs += probe_runs
    if not runs:
        raise SystemExit("no targets selected")

    report = {
        "version": config.get("version"),
        "config": args.config,
        "prompt": config["prompt"],
        "max_tokens": config["max_tokens"],
        "stream": config.get("stream", True),
        "runs": runs,
        "prefix_cache": prefix_summaries,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8", newline="\n")

    markdown = render_markdown(report)
    md = Path(args.markdown)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(markdown, encoding="utf-8", newline="\n")
    print()
    print(markdown)
    print(f"report written to {out}")
    raise SystemExit(1 if any(r["error_rate"] >= 0.5 for r in runs) else 0)


if __name__ == "__main__":
    main()
