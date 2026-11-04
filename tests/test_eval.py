import unittest

from eval.gate import evaluate_gate
from eval.scoring import parse_lenient, parse_strict, schema_ok, score, summarise

TARGET = {
    "category": "technical",
    "priority": "P2",
    "sentiment": "negative",
    "escalate": True,
    "product": "X200 pump",
    "summary": "Line stopped",
}
GOOD = (
    '{"category": "technical", "priority": "P2", "sentiment": "negative", '
    '"escalate": true, "product": "X200 pump", "summary": "Line stopped at plant"}'
)

GATE = {
    "thresholds": {
        "strict_json": 0.99,
        "schema": 0.99,
        "exact_labels": 0.70,
        "slice_exact": {"clean": 0.6, "hard": 0.5},
    },
    "drift": {"max_exact_drop": 0.05, "max_field_drop": 0.10},
}


def summary(exact=0.8, schema=1.0, strict=1.0, errors=0, slices=None, fields=None):
    return {
        "model": "candidate",
        "n": 10,
        "errors": errors,
        "strict_json_rate": strict,
        "lenient_json_rate": strict,
        "schema_rate": schema,
        "exact_labels_rate": exact,
        "field_accuracy": fields
        or {
            "category": 0.9,
            "priority": 0.9,
            "sentiment": 0.9,
            "escalate": 0.9,
            "product": 0.9,
        },
        "slices": slices
        or {
            "clean": {"n": 5, "schema": schema, "exact": exact},
            "hard": {"n": 5, "schema": schema, "exact": exact},
        },
    }


class ScoringTest(unittest.TestCase):
    def test_strict_rejects_fences_and_lenient_accepts(self) -> None:
        fenced = "```json\n" + GOOD + "\n```"
        self.assertIsNone(parse_strict(fenced))
        self.assertIsNotNone(parse_lenient(fenced))

    def test_schema_and_exact_match(self) -> None:
        result = score(TARGET, GOOD)
        self.assertTrue(result["schema"])
        self.assertTrue(result["exact_labels"])
        self.assertTrue(all(result["fields"].values()))

    def test_wrong_taxonomy_fails_schema(self) -> None:
        bad = GOOD.replace('"technical"', '"security"')
        self.assertFalse(schema_ok(parse_strict(bad)))
        self.assertFalse(score(TARGET, bad)["schema"])

    def test_product_comparison_is_case_insensitive(self) -> None:
        lower = GOOD.replace('"X200 pump"', '"x200 PUMP"')
        self.assertTrue(score(TARGET, lower)["fields"]["product"])

    def test_summary_records_mismatched_fields(self) -> None:
        wrong = GOOD.replace('"P2"', '"P3"')
        result = score(TARGET, wrong)
        result["id"] = "clean-1"
        result["slice"] = "clean"
        out = summarise("candidate", [result])
        self.assertEqual(out["mismatches"][0]["fields"], ["priority"])
        self.assertEqual(out["mismatches"][0]["got"], {"priority": "P3"})


class GateTest(unittest.TestCase):
    def test_current_quality_passes(self) -> None:
        result = evaluate_gate(summary(), GATE)
        self.assertTrue(result["passed"])

    def test_absolute_floor_fails(self) -> None:
        result = evaluate_gate(summary(exact=0.5), GATE)
        self.assertFalse(result["passed"])
        failed = {c["name"] for c in result["checks"] if not c["ok"]}
        self.assertIn("exact_labels", failed)

    def test_slice_floor_fails_even_when_overall_passes(self) -> None:
        slices = {
            "clean": {"n": 5, "schema": 1.0, "exact": 0.9},
            "hard": {"n": 5, "schema": 1.0, "exact": 0.3},
        }
        result = evaluate_gate(summary(exact=0.8, slices=slices), GATE)
        self.assertFalse(result["passed"])
        self.assertIn("slice:hard", {c["name"] for c in result["checks"] if not c["ok"]})

    def test_schema_drop_fails(self) -> None:
        result = evaluate_gate(summary(schema=0.95), GATE)
        self.assertFalse(result["passed"])

    def test_drift_beyond_allowance_fails(self) -> None:
        baseline = summary(exact=0.9, fields={"priority": 0.95})
        candidate = summary(exact=0.8, fields={"priority": 0.8})
        result = evaluate_gate(candidate, GATE, baseline)
        self.assertFalse(result["passed"])
        failed = {c["name"] for c in result["checks"] if not c["ok"]}
        self.assertIn("drift:exact_labels", failed)
        self.assertIn("drift:priority", failed)

    def test_small_drop_is_tolerated(self) -> None:
        baseline = summary(exact=0.82)
        candidate = summary(exact=0.80)
        self.assertTrue(evaluate_gate(candidate, GATE, baseline)["passed"])

    def test_error_always_fails(self) -> None:
        self.assertFalse(evaluate_gate(summary(errors=1), GATE)["passed"])


if __name__ == "__main__":
    unittest.main()
