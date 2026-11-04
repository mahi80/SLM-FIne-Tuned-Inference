"""Score model aliases on the golden set and enforce release gates."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

from .gate import evaluate_gate, load_gate_config
from .runner import evaluate_model, load_golden


def _baseline_for(report: dict, model: str) -> dict | None:
    for entry in report.get("models", []):
        if entry.get("model") == model:
            return entry
    return None


def _print_summary(report: dict) -> None:
    header = (
        f"{'model':16} {'strict':>7} {'schema':>7} {'exact':>7} "
        f"{'cat':>6} {'prio':>6} {'sent':>6} {'esc':>6} {'prod':>6} "
        f"{'clean':>6} {'hard':>6} {'held':>6} {'gate':>6}"
    )
    print(header)
    print("-" * len(header))
    for m in report["models"]:
        f = m["field_accuracy"]
        s = m["slices"]
        gate = m.get("gate", {}).get("passed")
        state = "PASS" if gate else ("FAIL" if gate is False else "—")
        print(
            f"{m['model']:16} {m['strict_json_rate']:7.1%} {m['schema_rate']:7.1%} "
            f"{m['exact_labels_rate']:7.1%} "
            f"{f['category']:6.1%} {f['priority']:6.1%} {f['sentiment']:6.1%} "
            f"{f['escalate']:6.1%} {f['product']:6.1%} "
            f"{s.get('clean', {}).get('exact', 0):6.1%} "
            f"{s.get('hard', {}).get('exact', 0):6.1%} "
            f"{s.get('held-out', {}).get('exact', 0):6.1%} {state:>6}"
        )

    for m in report["models"]:
        gate = m.get("gate")
        if not gate:
            continue
        print(f"\ngate {m['model']}: {'PASS' if gate['passed'] else 'FAIL'}")
        for check in gate["checks"]:
            mark = "ok  " if check["ok"] else "FAIL"
            detail = f"  {check['detail']}" if check["detail"] else ""
            print(
                f"  {mark} {check['name']:22} actual={check['actual']} "
                f"limit={check['limit']}{detail}"
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default="src/eval/golden.jsonl")
    ap.add_argument("--base-url", default="http://localhost:8020")
    ap.add_argument(
        "--model",
        action="append",
        dest="models",
        help="alias or direct model name; repeatable",
    )
    ap.add_argument(
        "--api-key", default=os.environ.get("SLM_API_KEY", "dev-acme-key")
    )
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=220)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--gates", default="config/eval-gates.yaml")
    ap.add_argument("--baseline", default="", help="previous report to diff against")
    ap.add_argument("--out", default="bench/results/eval-gate.json")
    ap.add_argument("--no-gate", action="store_true")
    args = ap.parse_args()

    rows = load_golden(args.golden, args.limit)
    models = args.models or ["slm-triage-v2"]
    config = load_gate_config(args.gates)
    baseline_report = (
        json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        if args.baseline
        else None
    )

    report = {
        "golden": args.golden,
        "golden_sha256": hashlib.sha256(
            Path(args.golden).read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest(),
        "base_url": args.base_url,
        "n": len(rows),
        "gates": args.gates,
        "started_at": int(time.time()),
        "models": [],
    }

    for model in models:
        print(f"evaluating {model} on {len(rows)} golden tickets", flush=True)
        summary = asyncio.run(
            evaluate_model(
                args.base_url,
                model,
                rows,
                args.concurrency,
                args.api_key,
                args.max_tokens,
            )
        )
        if not args.no_gate:
            baseline = None
            if baseline_report is not None:
                baseline = _baseline_for(baseline_report, model)
                if baseline is None:
                    raise SystemExit(
                        f"baseline {args.baseline} has no entry for model '{model}'"
                    )
            summary["gate"] = evaluate_gate(summary, config, baseline)
        report["models"].append(summary)
        print(
            f"  strict {summary['strict_json_rate']:.1%}  "
            f"schema {summary['schema_rate']:.1%}  "
            f"exact {summary['exact_labels_rate']:.1%}  "
            f"errors {summary['errors']}",
            flush=True,
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8", newline="\n")
    print()
    _print_summary(report)
    print(f"\nreport written to {out}")

    failed = any(
        m.get("gate", {}).get("passed") is False for m in report["models"]
    )
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
