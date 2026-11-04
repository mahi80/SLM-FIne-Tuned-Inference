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
    return data


def render_markdown(report: dict) -> str:
    lines = [
        "| Target | Source | c | Requests | Req/s | Output tok/s | TTFT p50 | TTFT p95 | Total p50 | Total p95 | Errors |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in report["runs"]:
        ttft = run["ttft_ms"]
        total = run["total_ms"]
        lines.append(
            f"| {run['label']} | {run['source']} | {run['concurrency']} | "
            f"{run['ok']}/{run['n']} | {run['requests_per_s']:.2f} | "
            f"{run['output_tokens_per_s']:.1f} | "
            f"{_ms(ttft['p50'])} | {_ms(ttft['p95'])} | "
            f"{_ms(total['p50'])} | {_ms(total['p95'])} | {run['errors']} |"
        )
    return "\n".join(lines) + "\n"


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} ms"


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
    if not runs:
        raise SystemExit("no targets selected")

    report = {
        "version": config.get("version"),
        "config": args.config,
        "prompt": config["prompt"],
        "max_tokens": config["max_tokens"],
        "stream": config.get("stream", True),
        "runs": runs,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    markdown = render_markdown(report)
    md = Path(args.markdown)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(markdown, encoding="utf-8")
    print()
    print(markdown)
    print(f"report written to {out}")
    raise SystemExit(1 if any(r["error_rate"] >= 0.5 for r in runs) else 0)


if __name__ == "__main__":
    main()
