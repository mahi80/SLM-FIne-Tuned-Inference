"""The copy-ready examples must survive the same checks as shipped config.

`deploy/cloud/examples/` is what an operator copies when the first external
pool is wired. An example that no longer matches the schema is worse than no
example: it fails at review time with the validator's error instead of teaching
the shape. These tests run the examples through the real validators.
"""

import unittest
from pathlib import Path

import yaml

from tools import validate_deploy as vd

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "deploy/cloud/examples"


class CloudExamplesTest(unittest.TestCase):
    def test_external_pools_satisfy_the_registry_rules(self):
        capacity = vd.load_capacity(EXAMPLES / "external-pool.yaml")
        self.assertEqual(
            set(capacity["pools"]), {"cloud-prod", "cloud-gpu-vllm"}
        )
        for pool in capacity["pools"].values():
            self.assertTrue(pool["external"])
            self.assertGreater(pool["slo"]["expected_latency_ms"], 0)

    def test_candidates_agree_with_their_pools(self):
        capacity = vd.load_capacity(EXAMPLES / "external-pool.yaml")
        errors = vd.check_pools(capacity, EXAMPLES / "policy-candidates.yaml")
        self.assertEqual(errors, [])

    def test_all_three_credential_sources_are_shown(self):
        policy = yaml.safe_load(
            (EXAMPLES / "policy-candidates.yaml").read_text(encoding="utf-8")
        )
        sources = {
            candidate["access"].get("source", "static")
            for alias in policy["aliases"].values()
            for candidate in alias["candidates"]
        }
        self.assertEqual(sources, {"static", "file", "oidc"})

    def test_egress_example_opens_two_narrow_rules(self):
        policy = yaml.safe_load(
            (EXAMPLES / "egress-policy.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(vd.check_cloud_egress(policy), [])
        self.assertEqual(len(policy["spec"]["egress"]), 2)


if __name__ == "__main__":
    unittest.main()
