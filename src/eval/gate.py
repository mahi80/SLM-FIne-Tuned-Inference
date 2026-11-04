"""Release gate: absolute floors plus drift against a known-good baseline.

A floor alone cannot catch a slow regression that is still above the floor, and
a drift check alone cannot catch a bad release when the baseline is bad. The
gate uses both, and reports every check rather than only the first failure.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def load_gate_config(path: str) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("gate config must be a mapping")
    for key in ("thresholds", "drift"):
        if not isinstance(data.get(key), dict):
            raise ValueError(f"gate config is missing '{key}'")
    return data


def _check(name: str, actual, limit, ok: bool, detail: str = "") -> dict:
    return {
        "name": name,
        "actual": actual,
        "limit": limit,
        "ok": bool(ok),
        "detail": detail,
    }


def evaluate_gate(summary: dict, config: dict, baseline: dict | None = None) -> dict:
    thresholds = config["thresholds"]
    drift = config["drift"]
    checks = [
        _check(
            "errors",
            summary["errors"],
            0,
            summary["errors"] == 0,
            "every request must return a scored answer",
        ),
        _check(
            "strict_json",
            summary["strict_json_rate"],
            thresholds["strict_json"],
            summary["strict_json_rate"] >= thresholds["strict_json"],
        ),
        _check(
            "schema",
            summary["schema_rate"],
            thresholds["schema"],
            summary["schema_rate"] >= thresholds["schema"],
        ),
        _check(
            "exact_labels",
            summary["exact_labels_rate"],
            thresholds["exact_labels"],
            summary["exact_labels_rate"] >= thresholds["exact_labels"],
        ),
    ]

    for name, floor in thresholds.get("slice_exact", {}).items():
        actual = summary["slices"].get(name, {}).get("exact")
        checks.append(
            _check(
                f"slice:{name}",
                actual,
                floor,
                actual is not None and actual >= floor,
                "golden slice is missing" if actual is None else "",
            )
        )

    if baseline is not None:
        max_drop = float(drift["max_exact_drop"])
        actual = summary["exact_labels_rate"]
        reference = baseline["exact_labels_rate"]
        checks.append(
            _check(
                "drift:exact_labels",
                actual,
                round(reference - max_drop, 3),
                actual >= reference - max_drop,
                f"baseline {reference:.1%}",
            )
        )
        field_drop = float(drift["max_field_drop"])
        for field, reference in baseline["field_accuracy"].items():
            actual = summary["field_accuracy"].get(field)
            checks.append(
                _check(
                    f"drift:{field}",
                    actual,
                    round(reference - field_drop, 3),
                    actual is not None and actual >= reference - field_drop,
                    f"baseline {reference:.1%}",
                )
            )

    return {
        "model": summary["model"],
        "passed": all(c["ok"] for c in checks),
        "checks": checks,
    }
