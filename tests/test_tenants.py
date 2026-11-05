"""Tenant configuration: the policy inputs the gateway reads per request.

Run with: PYTHONPATH=src python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway.config import load_tenants

REPO = Path(__file__).parents[1]

TENANTS = """
tenants:
  - id: acme
    api_key: key-acme
    rpm: 60
    region: eu
    data_class: confidential
"""


class TenantConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def load(self, text: str = TENANTS):
        path = self.root / "tenants.yaml"
        path.write_text(text, encoding="utf-8")
        return load_tenants(str(path))

    def test_missing_budget_defaults_to_zero(self) -> None:
        tenant = self.load()[0]
        self.assertEqual(tenant.latency_budget_ms, 0)

    def test_declared_budget_is_parsed(self) -> None:
        tenant = self.load(TENANTS.replace("    rpm: 60", "    rpm: 60\n    latency_budget_ms: 1200"))[0]
        self.assertEqual(tenant.latency_budget_ms, 1200)

    def test_negative_budget_is_rejected(self) -> None:
        bad = TENANTS.replace("    rpm: 60", "    rpm: 60\n    latency_budget_ms: -1")
        with self.assertRaises(ValueError):
            self.load(bad)

    def test_zero_budget_is_accepted_as_no_budget(self) -> None:
        tenant = self.load(
            TENANTS.replace("    rpm: 60", "    rpm: 60\n    latency_budget_ms: 0")
        )[0]
        self.assertEqual(tenant.latency_budget_ms, 0)

    def test_shipped_example_tenants_load(self) -> None:
        tenants = load_tenants(str(REPO / "config" / "tenants.example.yaml"))
        by_id = {tenant.id: tenant for tenant in tenants}
        self.assertEqual(by_id["acme"].latency_budget_ms, 0)
        self.assertEqual(by_id["globex"].latency_budget_ms, 2000)


if __name__ == "__main__":
    unittest.main()
