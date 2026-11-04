import unittest

from bench.runner import Sample, percentile, summarize


class PercentileTest(unittest.TestCase):
    def test_nearest_rank(self) -> None:
        values = [10, 20, 30, 40]
        self.assertEqual(percentile(values, 50), 20)
        self.assertEqual(percentile(values, 95), 40)
        self.assertEqual(percentile(values, 99), 40)

    def test_empty_is_none(self) -> None:
        self.assertIsNone(percentile([], 50))


class SummariseTest(unittest.TestCase):
    def sample(self, **overrides) -> Sample:
        base = dict(
            status=200,
            total_ms=100.0,
            ttft_ms=10.0,
            prompt_tokens=20,
            completion_tokens=64,
            simulated=False,
            tier="gpu",
        )
        base.update(overrides)
        return Sample(**base)

    def test_aggregate_throughput_and_percentiles(self) -> None:
        samples = [
            self.sample(total_ms=100.0, ttft_ms=10.0),
            self.sample(total_ms=200.0, ttft_ms=20.0),
            self.sample(total_ms=300.0, ttft_ms=30.0),
        ]
        summary = summarize(samples, 1.0)
        self.assertEqual(summary["ok"], 3)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["output_tokens_per_s"], 192.0)
        self.assertEqual(summary["ttft_ms"]["p50"], 20.0)
        self.assertEqual(summary["total_ms"]["p95"], 300.0)
        self.assertEqual(summary["source"], "measured")

    def test_errors_are_counted_but_excluded_from_latency(self) -> None:
        samples = [
            self.sample(),
            self.sample(
                status=0,
                total_ms=1.0,
                ttft_ms=None,
                prompt_tokens=None,
                completion_tokens=None,
                error="ConnectError",
            ),
        ]
        summary = summarize(samples, 1.0)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["error_rate"], 0.5)
        self.assertEqual(summary["total_ms"]["p50"], 100.0)

    def test_all_simulated_is_labelled_modelled(self) -> None:
        summary = summarize([self.sample(simulated=True, tier="cpu")], 1.0)
        self.assertTrue(summary["simulated"])
        self.assertEqual(summary["source"], "MODELLED")
        self.assertEqual(summary["tier"], "cpu")


if __name__ == "__main__":
    unittest.main()
