"""Evaluate the base model and the served adapters on the held-out triage set.

Scores the contract, not the prose: strict JSON, exact schema, and exact label
agreement per field. Results are sliced by ticket family (clean / hard /
held-out) so that a fine-tune that only memorised the easy templates does not
look like an improvement.

    python -m finetune.evaluate --models slm-small triage-v1 triage-v2
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path

import httpx

from finetune.prompt import CATEGORIES, PRIORITIES, SENTIMENTS, prompt_messages

KEYS = {"category", "priority", "sentiment", "escalate", "product", "summary"}
LABEL_FIELDS = ("category", "priority", "sentiment", "escalate", "product")


def slice_of(sample_id: str) -> str:
    if sample_id.startswith("hard-eval-"):
        return "held-out"
    if sample_id.startswith("hard-"):
        return "hard"
    return "clean"


def parse_strict(raw: str):
    text = raw.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_lenient(raw: str):
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0].strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def schema_ok(obj) -> bool:
    if not isinstance(obj, dict) or set(obj) != KEYS:
        return False
    if obj["category"] not in CATEGORIES or obj["priority"] not in PRIORITIES:
        return False
    if obj["sentiment"] not in SENTIMENTS or not isinstance(obj["escalate"], bool):
        return False
    if obj["product"] is not None and not isinstance(obj["product"], str):
        return False
    return isinstance(obj["summary"], str) and bool(obj["summary"].strip())


def norm_product(value) -> str | None:
    return None if value is None else str(value).strip().lower()


def score(target: dict, raw: str) -> dict:
    obj = parse_strict(raw)
    out = {
        "strict_json": obj is not None,
        "lenient_json": obj is not None or parse_lenient(raw) is not None,
        "schema": schema_ok(obj),
        "fields": {},
    }
    if not out["schema"]:
        return out
    out["fields"]["category"] = obj["category"] == target["category"]
    out["fields"]["priority"] = obj["priority"] == target["priority"]
    out["fields"]["sentiment"] = obj["sentiment"] == target["sentiment"]
    out["fields"]["escalate"] = obj["escalate"] == target["escalate"]
    out["fields"]["product"] = norm_product(obj["product"]) == norm_product(target["product"])
    words = obj["summary"].split()
    out["summary_ok"] = 0 < len(words) <= 15
    out["exact_labels"] = all(out["fields"][f] for f in LABEL_FIELDS)
    return out


async def call(client: httpx.AsyncClient, url: str, model: str, ticket: str, sem: asyncio.Semaphore):
    async with sem:
        r = await client.post(
            f"{url}/v1/chat/completions",
            json={
                "model": model,
                "messages": prompt_messages(ticket),
                "temperature": 0.0,
                "max_tokens": 220,
            },
            timeout=180.0,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


async def evaluate_model(url: str, model: str, rows: list[dict], concurrency: int) -> dict:
    sem = asyncio.Semaphore(concurrency)
    results = []
    failures = []
    done = 0

    async with httpx.AsyncClient() as client:
        async def one(row):
            nonlocal done
            try:
                raw = await call(client, url, model, row["ticket"], sem)
            except Exception as exc:  # network/HTTP failure, keep the run going
                s = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            else:
                s = score(row["target"], raw)
                s["slice"] = slice_of(row["id"])
                if not s["schema"] and len(failures) < 5:
                    failures.append({"id": row["id"], "raw": raw[:300]})
            s["id"] = row["id"]
            results.append(s)
            done += 1
            if done % 50 == 0:
                print(f"  {model}: {done}/{len(rows)}", flush=True)

        await asyncio.gather(*(one(row) for row in rows))

    return {"model": model, "results": results, "failures": failures}


def summarise(model: str, results: list[dict]) -> dict:
    n = len(results)
    errors = sum("error" in r for r in results)
    valid = [r for r in results if "error" not in r]
    strict = sum(r["strict_json"] for r in valid)
    lenient = sum(r["lenient_json"] for r in valid)
    schema = sum(r["schema"] for r in valid)
    exact = sum(r.get("exact_labels", False) for r in valid)
    field_acc = {
        f: round(sum(r["fields"].get(f, False) for r in valid) / max(1, len(valid)), 3)
        for f in LABEL_FIELDS
    }
    summary_ok = sum(r.get("summary_ok", False) for r in valid)
    by_slice = defaultdict(lambda: {"n": 0, "exact": 0, "schema": 0})
    for r in valid:
        s = by_slice[r["slice"]]
        s["n"] += 1
        s["exact"] += bool(r.get("exact_labels"))
        s["schema"] += bool(r["schema"])
    return {
        "model": model,
        "n": n,
        "errors": errors,
        "strict_json_rate": round(strict / max(1, len(valid)), 3),
        "lenient_json_rate": round(lenient / max(1, len(valid)), 3),
        "schema_rate": round(schema / max(1, len(valid)), 3),
        "exact_labels_rate": round(exact / max(1, len(valid)), 3),
        "field_accuracy": field_acc,
        "summary_ok_rate": round(summary_ok / max(1, len(valid)), 3),
        "slices": {
            k: {"n": v["n"], "schema": round(v["schema"] / v["n"], 3), "exact": round(v["exact"] / v["n"], 3)}
            for k, v in sorted(by_slice.items())
        },
        "invalid_counts": dict(Counter(
            "error" if "error" in r else ("schema" if not r["schema"] else "labels")
            for r in results
        )),
    }


async def main_async(args) -> int:
    rows = [json.loads(line) for line in Path(args.eval).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        rows = rows[: args.limit]

    report = {"eval": args.eval, "url": args.url, "n": len(rows), "models": []}
    for model in args.models:
        print(f"evaluating {model} on {len(rows)} tickets", flush=True)
        outcome = await evaluate_model(args.url, model, rows, args.concurrency)
        summary = summarise(model, outcome["results"])
        summary["failures"] = outcome["failures"]
        report["models"].append(summary)
        print(f"  strict {summary['strict_json_rate']:.1%}  schema {summary['schema_rate']:.1%}  "
              f"exact {summary['exact_labels_rate']:.1%}  errors {summary['errors']}", flush=True)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    header = f"{'model':14} {'strict':>7} {'schema':>7} {'exact':>7} {'cat':>6} {'prio':>6} {'sent':>6} {'esc':>6} {'prod':>6} {'clean':>6} {'hard':>6} {'held':>6}"
    print(header)
    print("-" * len(header))
    for m in report["models"]:
        s = m["slices"]
        f = m["field_accuracy"]
        print(f"{m['model']:14} {m['strict_json_rate']:7.1%} {m['schema_rate']:7.1%} {m['exact_labels_rate']:7.1%} "
              f"{f['category']:6.1%} {f['priority']:6.1%} {f['sentiment']:6.1%} {f['escalate']:6.1%} {f['product']:6.1%} "
              f"{s.get('clean',{}).get('exact',0):6.1%} {s.get('hard',{}).get('exact',0):6.1%} {s.get('held-out',{}).get('exact',0):6.1%}")
    print(f"\nreport written to {args.out}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--eval", default="finetune/data/eval.jsonl")
    ap.add_argument("--url", default="http://localhost:8010")
    ap.add_argument("--models", nargs="+", default=["slm-small", "triage-v1", "triage-v2"])
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="bench/results/adapters-eval.json")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
