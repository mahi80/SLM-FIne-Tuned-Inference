import asyncio
import json
import unittest
from unittest import mock

from bench.cli import load_config, render_markdown, summarise_prefix_probe
from bench.runner import (
    Sample,
    _parse_sse,
    build_messages,
    percentile,
    summarize,
    time_per_output_token,
)


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

    def test_decode_phase_metrics(self) -> None:
        samples = [
            self.sample(tpot_ms=20.0, itl_gaps_ms=[18.0, 20.0, 22.0]),
            self.sample(tpot_ms=40.0, itl_gaps_ms=[30.0, 90.0]),
        ]
        summary = summarize(samples, 1.0)
        self.assertEqual(summary["tpot_ms"]["p50"], 20.0)
        self.assertEqual(summary["itl_ms"]["p50"], 22.0)
        self.assertEqual(summary["itl_ms"]["p99"], 90.0)
        self.assertEqual(summary["decode_tokens_per_s_per_stream"], 50.0)
        # 20 prompt tokens / 10 ms TTFT.
        self.assertEqual(summary["prefill_tokens_per_s_est"], 2000.0)

    def test_non_streaming_has_no_decode_metrics(self) -> None:
        summary = summarize([self.sample(ttft_ms=100.0)], 1.0)
        self.assertIsNone(summary["tpot_ms"]["p50"])
        self.assertIsNone(summary["itl_ms"]["p50"])
        self.assertIsNone(summary["decode_tokens_per_s_per_stream"])
        self.assertIsNone(summary["prefill_tokens_per_s_est"])

    def test_failures_omit_gap_lists(self) -> None:
        summary = summarize(
            [self.sample(status=500, error="boom", itl_gaps_ms=[1.0] * 50)], 1.0
        )
        self.assertNotIn("itl_gaps_ms", summary["failures"][0])


class TimePerOutputTokenTest(unittest.TestCase):
    def test_excludes_first_token(self) -> None:
        # First token at 100 ms, tenth at 280 ms -> 9 decode steps of 20 ms.
        self.assertEqual(time_per_output_token(100.0, 280.0, 10), 20.0)

    def test_single_token_has_no_decode_phase(self) -> None:
        self.assertIsNone(time_per_output_token(100.0, 100.0, 1))
        self.assertIsNone(time_per_output_token(None, 100.0, 5))


class ParseSseTest(unittest.TestCase):
    def test_records_ttft_and_gaps(self) -> None:
        def chunk(text: str) -> str:
            return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]})

        lines = [
            chunk("a"),
            chunk("b"),
            chunk("c"),
            "data: " + json.dumps({"usage": {"prompt_tokens": 7, "completion_tokens": 3}}),
            "data: [DONE]",
        ]
        clock = iter([0.050, 0.060, 0.080])

        async def feed():
            for line in lines:
                yield line

        with mock.patch("bench.runner.time.perf_counter", side_effect=lambda: next(clock)):
            timing = asyncio.run(_parse_sse(feed(), 0.0))
        self.assertEqual(timing.ttft_ms, 50.0)
        self.assertEqual(timing.itl_gaps_ms, [10.0, 20.0])
        self.assertEqual(timing.last_token_ms, 80.0)
        self.assertEqual(timing.prompt_tokens, 7)
        self.assertEqual(timing.completion_tokens, 3)
        self.assertEqual(time_per_output_token(timing.ttft_ms, timing.last_token_ms, 3), 15.0)


class MessagesTest(unittest.TestCase):
    def test_shared_prefix_is_stable(self) -> None:
        a = build_messages("q", "system text", unique_prefix=False)
        b = build_messages("q", "system text", unique_prefix=False)
        self.assertEqual(a, b)
        self.assertEqual(a[0], {"role": "system", "content": "system text"})

    def test_unique_prefix_puts_nonce_first(self) -> None:
        a = build_messages("q", "system text", unique_prefix=True)
        b = build_messages("q", "system text", unique_prefix=True)
        self.assertNotEqual(a[0]["content"], b[0]["content"])
        self.assertTrue(a[0]["content"].startswith("[request "))
        self.assertTrue(a[0]["content"].endswith("system text"))

    def test_no_system_prompt(self) -> None:
        self.assertEqual(
            build_messages("q", None, unique_prefix=False),
            [{"role": "user", "content": "q"}],
        )


class PrefixProbeTest(unittest.TestCase):
    def test_summary_and_markdown(self) -> None:
        shared = {"ttft_ms": {"p50": 40.0}, "source": "measured", "prompt_tokens_avg": 380.0}
        unique = {"ttft_ms": {"p50": 100.0}, "source": "measured", "prompt_tokens_avg": 392.0}
        probe = summarise_prefix_probe("gpu-direct", shared, unique)
        self.assertEqual(probe["ttft_saved_pct"], 60.0)
        markdown = render_markdown({"runs": [], "prefix_cache": [probe]})
        self.assertIn("| gpu-direct | measured | 380.0 | 40 ms | 100 ms | 60% |", markdown)

    def test_repo_config_probe_is_valid(self) -> None:
        config = load_config("config/bench.yaml")
        self.assertIn("gpu-direct", config["prefix_probe"]["targets"])

    def test_probe_rejects_unknown_target(self) -> None:
        import tempfile, pathlib

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bench.yaml"
            path.write_text(
                "prompt: p\nmax_tokens: 1\nconcurrency: [1]\nrequests: 1\n"
                "targets:\n  - {label: a, base_url: http://x, model: m}\n"
                "prefix_probe: {targets: [b], system_prompt: s}\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(str(path))


if __name__ == "__main__":
    unittest.main()
