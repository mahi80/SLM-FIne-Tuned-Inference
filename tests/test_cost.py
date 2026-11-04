import json
import tempfile
import unittest
from pathlib import Path

from cost.model import build_report, cost_per_1m_output, hourly_cost, OwnedGpu

CONFIG = {
    "version": "test",
    "workload": {"prompt_tokens": 1000, "output_tokens": 100, "requests_per_day": 20000},
    "owned_gpu": {
        "name": "test node",
        "capex_usd": 2628,
        "amortisation_months": 36,
        "power_w": 100,
        "pue": 1.5,
        "electricity_usd_per_kwh": 0.2,
        "ops_overhead_pct": 0,
        "utilisation": 0.5,
        "fallback_output_tps": 50,
        "bench_path": "",
        "bench_label_prefix": "gpu-direct",
        "bench_latency_label": "gpu-direct c=1",
    },
}


class ArithmeticTest(unittest.TestCase):
    def test_hourly_cost_adds_power_and_depreciation(self) -> None:
        hw = OwnedGpu(**CONFIG["owned_gpu"])
        # 2628 / (36 * 730) = 0.1; (0.1 kW * 1.5 * 0.2) = 0.03
        self.assertEqual(hourly_cost(hw), 0.13)

    def test_cost_per_million_output(self) -> None:
        self.assertEqual(cost_per_1m_output(0.13, 360, 0.5), 0.2006)

    def test_invalid_inputs_raise(self) -> None:
        with self.assertRaises(ValueError):
            cost_per_1m_output(0.13, 0, 0.5)
        with self.assertRaises(ValueError):
            cost_per_1m_output(0.13, 360, 0)


class ReportTest(unittest.TestCase):
    def bench(self, runs):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"runs": runs}, tmp)
        tmp.close()
        return tmp.name

    def report(self, bench_path=""):
        config = json.loads(json.dumps(CONFIG))
        config["owned_gpu"]["bench_path"] = bench_path
        return build_report(config)

    def test_fallback_throughput_is_labelled(self) -> None:
        report = self.report()
        self.assertEqual(report["owned_gpu"]["output_tps"], 50)
        self.assertEqual(report["tiers"][0]["source"], "MODELLED fallback")

    def test_bench_picks_highest_measured_run_and_ignores_simulated(self) -> None:
        path = self.bench(
            [
                {
                    "label": "gpu-direct c=1",
                    "source": "measured",
                    "output_tokens_per_s": 100,
                    "concurrency": 1,
                },
                {
                    "label": "gpu-direct c=4",
                    "source": "measured",
                    "output_tokens_per_s": 300,
                    "concurrency": 4,
                },
                {
                    "label": "gpu-direct c=8",
                    "source": "MODELLED",
                    "output_tokens_per_s": 1000,
                    "concurrency": 8,
                },
            ]
        )
        try:
            report = self.report(path)
        finally:
            Path(path).unlink()
        self.assertEqual(report["owned_gpu"]["output_tps"], 300)
        self.assertEqual(report["owned_gpu"]["throughput_source"]["concurrency"], 4)
        self.assertEqual(report["tiers"][0]["source"], "measured")

    def test_workload_cost_uses_fixed_monthly_spend(self) -> None:
        report = self.report()
        gpu = report["tiers"][0]
        expected = round(gpu["usd_per_month_at_workload"] * 1e6 / (100 * 20000 * 30), 4)
        self.assertEqual(gpu["usd_per_1m_at_workload"], expected)
        self.assertGreater(gpu["usd_per_1m_at_workload"], gpu["usd_per_1m_output"])

    def test_modelled_tiers_are_linear_and_ordered(self) -> None:
        report = self.report()
        by_name = {tier["tier"]: tier for tier in report["tiers"]}
        self.assertLess(
            by_name["gpu-l4-4b"]["usd_per_1m_output"],
            by_name["cpu-xeon-4b"]["usd_per_1m_output"],
        )
        break_even = {
            entry["tier"]: entry["requests_per_day"]
            for entry in report["break_even_vs_owned"]
        }
        self.assertLess(break_even["cpu-xeon-4b"], break_even["gpu-l4-4b"])

    def test_capacity_shortfall_is_reported(self) -> None:
        config = json.loads(json.dumps(CONFIG))
        config["workload"]["requests_per_day"] = 10_000_000
        report = build_report(config)
        self.assertTrue(report["owned_gpu"]["capacity_shortfall"])


if __name__ == "__main__":
    unittest.main()
