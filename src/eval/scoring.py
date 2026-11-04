"""Deterministic checks for the triage contract.

The model is judged on the contract, not the prose: strict JSON, exact schema,
and exact label agreement per field. Strict means `json.loads` of the raw answer
with no fence stripping; the lenient score is reported separately so a model
cannot pass by writing prose around the object.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict

from finetune.prompt import CATEGORIES, PRIORITIES, SENTIMENTS

KEYS = {"category", "priority", "sentiment", "escalate", "product", "summary"}
LABEL_FIELDS = ("category", "priority", "sentiment", "escalate", "product")


def slice_of(sample_id: str) -> str:
    if sample_id.startswith("heldout-"):
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
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_lenient(raw: str):
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0].strip()
    try:
        obj = json.loads(text)
    except ValueError:
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
    out["fields"]["product"] = norm_product(obj["product"]) == norm_product(
        target["product"]
    )
    out["summary_ok"] = 0 < len(obj["summary"].split()) <= 15
    out["labels"] = {f: obj[f] for f in LABEL_FIELDS}
    out["exact_labels"] = all(out["fields"][f] for f in LABEL_FIELDS)
    return out


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
        "summary_ok_rate": round(
            sum(r.get("summary_ok", False) for r in valid) / max(1, len(valid)), 3
        ),
        "slices": {
            k: {
                "n": v["n"],
                "schema": round(v["schema"] / v["n"], 3),
                "exact": round(v["exact"] / v["n"], 3),
            }
            for k, v in sorted(by_slice.items())
        },
        "invalid_counts": dict(
            Counter(
                "error" if "error" in r else ("schema" if not r["schema"] else "labels")
                for r in results
            )
        ),
        "mismatches": [
            {
                "id": r["id"],
                "slice": r.get("slice"),
                "fields": [f for f, ok in r["fields"].items() if not ok],
                "got": {
                    f: r.get("labels", {}).get(f)
                    for f, ok in r["fields"].items()
                    if not ok
                },
            }
            for r in valid
            if r.get("schema") and not r.get("exact_labels")
        ],
    }
