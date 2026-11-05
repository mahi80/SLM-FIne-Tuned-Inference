"""Unit tests for the router package.

Run with: PYTHONPATH=src python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from router import AuditLog, load_policy
from router.breakers import CLOSED, HALF_OPEN, OPEN, CircuitBreakers, key
from router.engine import PolicyEngine, PolicyStore
from router.policy import BreakerPolicy

POLICY = """
version: "test-1"
breaker:
  failures: 2
  window_s: 60
  open_s: 10
aliases:
  general:
    primary: gpu
    canary:
      candidate: cpu
      percent: 100
      sticky: tenant
    shadow:
      candidate: cpu
      percent: 100
      max_tokens: 8
    candidates:
      - name: gpu
        compute: gpu
        deployment: onprem
        pool: gpu-pool
        backend: http://gpu:8000
        upstream_model: model-gpu
        serves_regions: [eu, us]
        sovereign: true
        priority: 0
        max_context: 100
      - name: cpu
        compute: cpu
        deployment: onprem
        pool: cpu-pool
        backend: http://cpu:8001
        upstream_model: model-cpu
        serves_regions: [eu, us]
        sovereign: true
        priority: 10
        max_context: 50
      - name: cloud
        compute: gpu
        deployment: cloud
        pool: cloud-pool
        backend: http://cloud:8001
        upstream_model: model-cloud
        serves_regions: [us]
        sovereign: false
        priority: 5
        max_context: 100
  fleet:
    primary: gpu
    candidates:
      - name: gpu
        compute: gpu
        deployment: onprem
        pool: gpu-pool
        backend: http://gpu:8000
        upstream_model: model-gpu
        serves_regions: [eu, us]
        sovereign: true
        priority: 0
        max_context: 100
      - name: cpu
        compute: cpu
        deployment: onprem
        pool: cpu-pool
        backend: http://cpu:8001
        upstream_model: model-cpu
        serves_regions: [eu, us]
        sovereign: true
        priority: 10
        max_context: 50
      - name: cloud
        compute: gpu
        deployment: cloud
        pool: cloud-pool
        backend: http://cloud:8001
        upstream_model: model-cloud
        serves_regions: [us]
        sovereign: false
        priority: 5
        max_context: 100
  pinned:
    primary: gpu
    candidates:
      - name: gpu
        compute: gpu
        deployment: onprem
        pool: gpu-pool
        backend: http://gpu:8000
        upstream_model: model-gpu
        serves_regions: [eu, us]
        sovereign: true
"""

EXTERNAL_POLICY = """
version: "test-external"
aliases:
  burst:
    primary: gpu
    candidates:
      - name: gpu
        compute: gpu
        deployment: onprem
        pool: gpu-pool
        backend: http://gpu:8000
        upstream_model: model-gpu
        serves_regions: [eu, us]
        sovereign: true
        max_context: 100
      - name: cloud
        compute: gpu
        deployment: cloud
        pool: cloud-pool
        backend: https://inference.example.com
        upstream_model: model-cloud
        serves_regions: [eu, us]
        sovereign: false
        priority: 5
        max_context: 100
        access:
          key: external-prod
"""


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def tenant(**overrides):
    values = {
        "id": "t1",
        "region": "eu",
        "data_class": "confidential",
        "allowed_aliases": (),
        "allowed_tiers": (),
        "allowed_compute": (),
        "allowed_deployment": (),
        "allow_cloud": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def policy_file(self, text: str = POLICY) -> Path:
        path = self.root / "policy.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def engine(self, text: str = POLICY, backend_ready=None):
        policy = load_policy(str(self.policy_file(text)))
        clock = Clock()
        return (
            PolicyEngine(policy, CircuitBreakers(policy.breaker, clock), backend_ready),
            clock,
        )


class PolicyLoadingTest(Workspace):
    def test_repository_policy_loads(self) -> None:
        policy = load_policy(str(Path(__file__).parents[1] / "config" / "policy.yaml"))
        self.assertIn("slm-small", policy.aliases)
        self.assertEqual(policy.aliases["slm-small"].primary, "gpu")
        self.assertEqual(policy.aliases["slm-triage"].canary.candidate, "v2")

    def test_unknown_candidate_key_is_rejected(self) -> None:
        bad = POLICY.replace("max_context: 100", "max_contextt: 100", 1)
        with self.assertRaises(ValueError):
            load_policy(str(self.policy_file(bad)))

    def test_unknown_compute_type_is_rejected(self) -> None:
        bad = POLICY.replace("compute: gpu", "compute: tpu", 1)
        with self.assertRaises(ValueError):
            load_policy(str(self.policy_file(bad)))

    def test_candidate_must_name_a_pool(self) -> None:
        bad = POLICY.replace("        pool: gpu-pool\n", "", 1)
        with self.assertRaises(ValueError):
            load_policy(str(self.policy_file(bad)))

    def test_tier_is_derived_not_declared(self) -> None:
        bad = POLICY.replace("        pool: gpu-pool\n", "        pool: gpu-pool\n        tier: gpu\n", 1)
        with self.assertRaises(ValueError):
            load_policy(str(self.policy_file(bad)))

    def test_primary_must_exist(self) -> None:
        bad = POLICY.replace("primary: gpu", "primary: nope", 1)
        with self.assertRaises(ValueError):
            load_policy(str(self.policy_file(bad)))

    def test_canary_must_differ_from_primary(self) -> None:
        bad = POLICY.replace("candidate: cpu\n      percent: 100", "candidate: gpu\n      percent: 100", 1)
        with self.assertRaises(ValueError):
            load_policy(str(self.policy_file(bad)))


class AdmissionTest(Workspace):
    def test_primary_is_preferred_when_admissible(self) -> None:
        engine, _ = self.engine()
        decision = engine.decide("fleet", tenant(), "req-1", 10)
        self.assertEqual(decision.kind, "primary")
        self.assertEqual(decision.chosen.name, "gpu")

    def test_region_veto_beats_cheap_standby(self) -> None:
        engine, _ = self.engine()
        for _ in range(2):
            engine.breakers.record_failure(key("fleet", "gpu"))
        decision = engine.decide("fleet", tenant(), "req-1", 10)
        self.assertEqual(decision.chosen.name, "cpu")
        reasons = {item.candidate: item.reason for item in decision.considered}
        self.assertEqual(reasons["cloud"], "region-veto")

    def test_cloud_is_used_for_allowed_region(self) -> None:
        engine, _ = self.engine()
        for _ in range(2):
            engine.breakers.record_failure(key("fleet", "gpu"))
        tenant_us = tenant(id="t2", region="us", data_class="internal")
        decision = engine.decide("fleet", tenant_us, "req-1", 10)
        self.assertEqual(decision.chosen.name, "cloud")
        self.assertEqual(decision.kind, "standby")

    def test_confidential_data_rejects_non_sovereign_candidate(self) -> None:
        engine, _ = self.engine()
        for _ in range(2):
            engine.breakers.record_failure(key("fleet", "gpu"))
        tenant_us = tenant(id="t2", region="us", data_class="confidential")
        decision = engine.decide("fleet", tenant_us, "req-1", 10)
        self.assertEqual(decision.chosen.name, "cpu")
        reasons = {item.candidate: item.reason for item in decision.considered}
        self.assertEqual(reasons["cloud"], "sovereignty-veto")

    def test_alias_denial_refuses_instead_of_falling_back(self) -> None:
        engine, _ = self.engine()
        decision = engine.decide(
            "fleet", tenant(allowed_aliases=("other",)), "req-1", 10
        )
        self.assertIsNone(decision.chosen)
        self.assertEqual(decision.refusal, "alias-denied")
        self.assertTrue(all(not item.admitted for item in decision.considered))

    def test_context_budget_can_exhaust_every_candidate(self) -> None:
        engine, _ = self.engine()
        for _ in range(2):
            engine.breakers.record_failure(key("fleet", "gpu"))
        decision = engine.decide("fleet", tenant(), "req-1", 60)
        self.assertIsNone(decision.chosen)
        self.assertEqual(decision.refusal, "no-capacity")
        reasons = {item.candidate: item.reason for item in decision.considered}
        self.assertEqual(reasons["cpu"], "context-exceeded")

    def test_tenant_tier_allowlist_is_enforced(self) -> None:
        engine, _ = self.engine()
        decision = engine.decide(
            "fleet", tenant(allowed_tiers=("cpu",)), "req-1", 10
        )
        self.assertEqual(decision.chosen.name, "cpu")

    def test_tenant_compute_allowlist_is_enforced(self) -> None:
        engine, _ = self.engine()
        decision = engine.decide(
            "fleet", tenant(allowed_compute=("cpu",)), "req-1", 10
        )
        self.assertEqual(decision.chosen.name, "cpu")
        reasons = {item.candidate: item.reason for item in decision.considered}
        self.assertEqual(reasons["gpu"], "compute-denied")

    def test_gpu_onprem_only_tenant_rejects_the_cloud_gpu(self) -> None:
        engine, _ = self.engine()
        for _ in range(2):
            engine.breakers.record_failure(key("fleet", "gpu"))
        scoped = tenant(
            region="us",
            allowed_compute=("gpu",),
            allowed_deployment=("onprem",),
        )
        decision = engine.decide("fleet", scoped, "req-1", 10)
        self.assertIsNone(decision.chosen)
        reasons = {item.candidate: item.reason for item in decision.considered}
        self.assertEqual(reasons["cloud"], "deployment-denied")
        self.assertEqual(reasons["cpu"], "compute-denied")


class ExternalBackendTest(Workspace):
    def test_access_is_parsed_with_safe_defaults(self) -> None:
        policy = load_policy(str(self.policy_file(EXTERNAL_POLICY)))
        cloud = policy.aliases["burst"].candidates["cloud"]
        self.assertTrue(cloud.external)
        self.assertEqual(cloud.access.key, "external-prod")
        self.assertEqual(cloud.access.header, "authorization")
        self.assertEqual(cloud.access.prefix, "Bearer ")

    def test_in_cluster_candidate_is_not_external(self) -> None:
        policy = load_policy(str(self.policy_file(EXTERNAL_POLICY)))
        self.assertFalse(policy.aliases["burst"].candidates["gpu"].external)

    def test_external_backend_must_use_https(self) -> None:
        bad = EXTERNAL_POLICY.replace(
            "backend: https://inference.example.com", "backend: http://inference.example.com"
        )
        with self.assertRaises(ValueError):
            load_policy(str(self.policy_file(bad)))

    def test_external_backend_must_not_embed_credentials(self) -> None:
        bad = EXTERNAL_POLICY.replace(
            "backend: https://inference.example.com",
            "backend: https://user:secret@inference.example.com",
        )
        with self.assertRaises(ValueError):
            load_policy(str(self.policy_file(bad)))

    def test_external_backend_must_not_carry_a_query(self) -> None:
        bad = EXTERNAL_POLICY.replace(
            "backend: https://inference.example.com",
            "backend: https://inference.example.com?key=leak",
        )
        with self.assertRaises(ValueError):
            load_policy(str(self.policy_file(bad)))

    def test_access_key_must_be_a_name(self) -> None:
        bad = EXTERNAL_POLICY.replace("key: external-prod", "key: ''")
        with self.assertRaises(ValueError):
            load_policy(str(self.policy_file(bad)))

    def test_access_header_cannot_inject_headers(self) -> None:
        bad = EXTERNAL_POLICY.replace(
            "key: external-prod", "key: external-prod\n          header: \"X-Key: a\\r\\nHost: evil\""
        )
        with self.assertRaises(ValueError):
            load_policy(str(self.policy_file(bad)))

    def test_access_prefix_cannot_contain_a_line_break(self) -> None:
        bad = EXTERNAL_POLICY.replace(
            "key: external-prod", "key: external-prod\n          prefix: \"Bearer \\n\""
        )
        with self.assertRaises(ValueError):
            load_policy(str(self.policy_file(bad)))

    def test_unconfigured_external_backend_is_vetoed(self) -> None:
        engine, _ = self.engine(EXTERNAL_POLICY, backend_ready=lambda candidate: False)
        cloud_only = tenant(
            region="eu", data_class="internal", allowed_deployment=("cloud",)
        )
        decision = engine.decide("burst", cloud_only, "req-1", 10)
        self.assertIsNone(decision.chosen)
        reasons = {item.candidate: item.reason for item in decision.considered}
        self.assertEqual(reasons["cloud"], "backend-unconfigured")

    def test_configured_external_backend_is_a_standby(self) -> None:
        engine, _ = self.engine(EXTERNAL_POLICY, backend_ready=lambda candidate: True)
        for _ in range(3):
            engine.breakers.record_failure(key("burst", "gpu"))
        decision = engine.decide("burst", tenant(region="eu", data_class="internal"), "req-1", 10)
        self.assertEqual(decision.chosen.name, "cloud")
        self.assertEqual(decision.kind, "standby")

    def test_reload_keeps_the_backend_readiness_view(self) -> None:
        store = PolicyStore(self.policy_file(EXTERNAL_POLICY), backend_ready=lambda candidate: False)
        policy = store.reload()
        self.assertIn("burst", policy.aliases)
        cloud_only = tenant(
            region="eu", data_class="internal", allowed_deployment=("cloud",)
        )
        decision = store.engine.decide("burst", cloud_only, "req-1", 10)
        reasons = {item.candidate: item.reason for item in decision.considered}
        self.assertEqual(reasons["cloud"], "backend-unconfigured")


class CanaryTest(Workspace):
    def test_canary_is_sticky_per_tenant(self) -> None:
        engine, _ = self.engine()
        first = engine.decide("general", tenant(), "req-1", 10)
        second = engine.decide("general", tenant(), "req-2", 10)
        self.assertEqual(first.kind, "canary")
        self.assertEqual(second.kind, "canary")
        self.assertEqual(first.bucket, second.bucket)

    def test_canary_falls_back_to_primary_when_probe_is_open(self) -> None:
        engine, _ = self.engine()
        for _ in range(2):
            engine.breakers.record_failure(key("general", "cpu"))
        decision = engine.decide("general", tenant(), "req-1", 10)
        self.assertEqual(decision.kind, "primary")
        self.assertFalse(decision.canary_hit)

    def test_shadow_target_is_not_the_served_candidate(self) -> None:
        engine, _ = self.engine()
        decision = engine.decide("general", tenant(), "req-1", 10)
        self.assertEqual(decision.chosen.name, "cpu")
        self.assertIsNone(decision.shadow)


class BreakerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.breakers = CircuitBreakers(
            BreakerPolicy(failures=2, window_s=60, open_s=10), self.clock
        )

    def test_opens_after_threshold_and_recovers_after_cooldown(self) -> None:
        self.breakers.record_failure("a")
        self.assertEqual(self.breakers.state("a"), CLOSED)
        self.breakers.record_failure("a")
        self.assertEqual(self.breakers.state("a"), OPEN)
        self.assertFalse(self.breakers.allow("a"))
        self.clock.now = 11
        self.assertEqual(self.breakers.state("a"), HALF_OPEN)
        self.assertTrue(self.breakers.allow("a"))
        self.assertFalse(self.breakers.allow("a"))
        self.breakers.record_success("a")
        self.assertEqual(self.breakers.state("a"), CLOSED)

    def test_failed_probe_reopens(self) -> None:
        self.breakers.record_failure("a")
        self.breakers.record_failure("a")
        self.clock.now = 11
        self.assertTrue(self.breakers.allow("a"))
        self.breakers.record_failure("a")
        self.assertEqual(self.breakers.state("a"), OPEN)
        self.clock.now = 12
        self.assertEqual(self.breakers.state("a"), OPEN)

    def test_failure_window_expires(self) -> None:
        self.breakers.record_failure("a")
        self.clock.now = 61
        self.breakers.record_failure("a")
        self.assertEqual(self.breakers.state("a"), CLOSED)
        self.assertAlmostEqual(self.breakers.failure_rate("a"), 0.5)


class AuditTest(Workspace):
    def test_chain_survives_reopen_and_continues(self) -> None:
        path = self.root / "audit.jsonl"
        log = AuditLog(path)
        log.append({"event": "a"})
        log.append({"event": "b"})
        self.assertEqual(log.verify()["records"], 2)
        log.close()

        log = AuditLog(path)
        record = log.append({"event": "c"})
        self.assertEqual(record["seq"], 3)
        self.assertTrue(log.verify()["ok"])
        log.close()

    def test_tampering_is_detected(self) -> None:
        path = self.root / "audit.jsonl"
        log = AuditLog(path)
        log.append({"event": "a"})
        log.append({"event": "b"})
        log.close()
        text = path.read_text(encoding="utf-8").replace('"event":"b"', '"event":"x"')
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(RuntimeError):
            AuditLog(path)

    def test_torn_tail_is_dropped(self) -> None:
        path = self.root / "audit.jsonl"
        log = AuditLog(path)
        log.append({"event": "a"})
        log.close()
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"seq":2,"ts":')
        log = AuditLog(path)
        self.assertTrue(log.verify()["ok"])
        self.assertEqual(log.verify()["records"], 1)
        log.append({"event": "b"})
        log.close()
        reopened = AuditLog(path)
        self.assertEqual(reopened.verify()["records"], 2)
        reopened.close()

    def test_reserved_keys_are_rejected(self) -> None:
        log = AuditLog(self.root / "audit.jsonl")
        with self.assertRaises(ValueError):
            log.append({"seq": 99})
        log.close()

    def test_truncation_while_running_is_detected(self) -> None:
        path = self.root / "audit.jsonl"
        log = AuditLog(path)
        log.append({"event": "a"})
        path.write_text("", encoding="utf-8")
        self.assertFalse(log.verify()["ok"])
        log.close()


class ReloadTest(Workspace):
    def test_invalid_reload_keeps_previous_policy(self) -> None:
        path = self.policy_file()
        store = PolicyStore(path)
        path.write_text("version: [", encoding="utf-8")
        with self.assertRaises(Exception):
            store.reload()
        self.assertEqual(store.policy.version, "test-1")

    def test_reload_swaps_policy_and_prunes_breakers(self) -> None:
        path = self.policy_file()
        store = PolicyStore(path)
        store.breakers.record_failure(key("pinned", "gpu"))
        updated = POLICY.replace('version: "test-1"', 'version: "test-2"').replace(
            "  pinned:\n", "  removed:\n", 1
        )
        path.write_text(updated, encoding="utf-8")
        policy = store.reload()
        self.assertEqual(policy.version, "test-2")
        self.assertNotIn("pinned", policy.aliases)
        self.assertNotIn(key("pinned", "gpu"), store.breakers.snapshot())


if __name__ == "__main__":
    unittest.main()
