import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from tools import fleet_status as fs
from tools.validate_deploy import load_capacity

REPO = Path(__file__).resolve().parent.parent
CAPACITY = REPO / "config/capacity.yaml"
POLICY = REPO / "config/policy.yaml"


def cluster(
    name="local",
    context="kind-slm",
    role="primary",
    gateway_url="",
    pools=("onprem-gpu", "onprem-cpu", "cloud-burst"),
):
    return {
        "name": name,
        "context": context,
        "role": role,
        "gateway_url": gateway_url,
        "pools": list(pools),
    }


def fleet(*clusters):
    return {"version": 1, "clusters": list(clusters)}


def policy():
    return yaml.safe_load(POLICY.read_text(encoding="utf-8"))


def capacity():
    return load_capacity(CAPACITY)


def gpu_node(name="gpu-0"):
    return {"metadata": {"name": name, "labels": {"nvidia.com/gpu.present": "true"}}}


def cpu_node(name="cpu-0"):
    return {"metadata": {"name": name, "labels": {"slm.fabric/pool": "cpu"}}}


def kubectl(payload, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        ["kubectl"], returncode, stdout=json.dumps(payload), stderr=stderr
    )


def run_main(argv, runner=None):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = fs.main(argv, **({"runner": runner} if runner else {}))
    return code, out.getvalue()


class Workspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def write_fleet(self, document) -> Path:
        path = Path(self._tmp.name) / "fleet.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return path


class ShippedFleet(Workspace):
    def test_shipped_fleet_passes_every_check(self):
        self.assertEqual(fs.check_fleet(fs.load_fleet(), capacity(), policy()), [])

    def test_shipped_fleet_hosts_every_declared_pool(self):
        loaded = fs.load_fleet()
        assigned = [
            pool for one in loaded["clusters"] for pool in one["pools"]
        ]
        self.assertEqual(sorted(assigned), sorted(capacity()["pools"]))

    def test_check_prints_the_table_and_the_summary(self):
        code, output = run_main(["--check"])
        self.assertEqual(code, 0)
        self.assertIn("CLUSTER", output)
        self.assertIn("kind-slm", output)
        self.assertIn("primary", output)
        self.assertIn("onprem-gpu, onprem-cpu, cloud-burst", output)
        self.assertIn("OK fleet 1 clusters / 3 pools", output)

    def test_check_does_not_need_a_cluster(self):
        def runner(args):  # pragma: no cover - a check must never call this
            raise AssertionError(f"offline check ran kubectl: {args}")

        code, _ = run_main(["--check"], runner=runner)
        self.assertEqual(code, 0)


class OfflineChecks(unittest.TestCase):
    def errors(self, fleet_document, policy_document=None):
        return fs.check_fleet(fleet_document, capacity(), policy_document or policy())

    def test_duplicate_cluster_name_is_rejected(self):
        errors = self.errors(
            fleet(cluster(), cluster(name="local", context="kind-other", role="standby"))
        )
        self.assertTrue(any("duplicate" in error and "cluster name 'local'" in error for error in errors))

    def test_duplicate_context_is_rejected(self):
        errors = self.errors(
            fleet(
                cluster(pools=["onprem-gpu"]),
                cluster(
                    name="edge",
                    context="kind-slm",
                    role="standby",
                    pools=["onprem-cpu", "cloud-burst"],
                ),
            )
        )
        self.assertTrue(any("kube context 'kind-slm'" in error for error in errors))

    def test_zero_primaries_is_rejected(self):
        errors = self.errors(fleet(cluster(role="standby")))
        self.assertTrue(any("0 primary cluster(s)" in error for error in errors))

    def test_two_primaries_are_rejected(self):
        errors = self.errors(
            fleet(
                cluster(pools=["onprem-gpu"]),
                cluster(
                    name="edge",
                    context="kind-edge",
                    role="primary",
                    pools=["onprem-cpu", "cloud-burst"],
                ),
            )
        )
        self.assertTrue(any("2 primary cluster(s)" in error for error in errors))

    def test_unknown_role_is_rejected(self):
        errors = self.errors(fleet(cluster(role="witness")))
        self.assertTrue(any("'witness' is not one of" in error for error in errors))

    def test_cluster_with_no_pools_is_rejected(self):
        errors = self.errors(fleet(cluster(pools=[])))
        self.assertTrue(any("names no pool" in error for error in errors))

    def test_unknown_pool_is_rejected(self):
        errors = self.errors(
            fleet(cluster(pools=["onprem-gpu", "onprem-cpu", "cloud-burst", "ghost-gpu"]))
        )
        self.assertTrue(any("pool 'ghost-gpu'" in error for error in errors))

    def test_unassigned_pool_is_rejected(self):
        errors = self.errors(fleet(cluster(pools=["onprem-gpu", "onprem-cpu"])))
        self.assertTrue(
            any("cloud-burst" in error and "assigned to no cluster" in error for error in errors)
        )

    def test_pool_assigned_twice_is_rejected(self):
        errors = self.errors(
            fleet(
                cluster(pools=["onprem-gpu", "onprem-cpu"]),
                cluster(
                    name="edge",
                    context="kind-edge",
                    role="standby",
                    pools=["cloud-burst", "onprem-cpu"],
                ),
            )
        )
        self.assertTrue(
            any("onprem-cpu is assigned to 2 clusters" in error for error in errors)
        )

    def test_duplicate_pool_inside_one_cluster_is_rejected(self):
        errors = self.errors(
            fleet(cluster(pools=["onprem-gpu", "onprem-cpu", "cloud-burst", "onprem-gpu"]))
        )
        self.assertTrue(any("names pool onprem-gpu more than once" in error for error in errors))

    def test_candidate_pool_not_assigned_is_rejected(self):
        # The policy routes to cloud-burst; the inventory stopped hosting it.
        errors = self.errors(fleet(cluster(pools=["onprem-gpu", "onprem-cpu"])))
        self.assertTrue(
            any("alias slm-small routes to pool cloud-burst" in error for error in errors),
            errors,
        )

    def test_candidate_pool_unknown_to_capacity_is_rejected(self):
        document = policy()
        document["aliases"]["slm-small"]["candidates"][0]["pool"] = "nowhere-gpu"
        errors = self.errors(fleet(), document)
        self.assertTrue(
            any("alias slm-small routes to pool nowhere-gpu" in error for error in errors),
            errors,
        )

    def test_plain_http_gateway_url_is_rejected(self):
        errors = self.errors(
            fleet(cluster(gateway_url="http://gateway.slm-fabric.svc.cluster.local:8000"))
        )
        self.assertTrue(any("gateway_url is not https" in error for error in errors))

    def test_gateway_url_with_userinfo_query_or_fragment_is_rejected(self):
        errors = self.errors(
            fleet(
                cluster(
                    gateway_url="https://user:secret@gateway.example/v1?token=abc#frag"
                )
            )
        )
        self.assertTrue(any("carries userinfo" in error for error in errors))
        self.assertTrue(any("carries a query string" in error for error in errors))
        self.assertTrue(any("carries a fragment" in error for error in errors))
        self.assertFalse(any("secret" in error for error in errors))

    def test_gateway_url_is_optional(self):
        self.assertEqual(self.errors(fleet(cluster(gateway_url=""))), [])


class NodeParsing(unittest.TestCase):
    def test_counts_nodes_and_gpu_labelled_nodes(self):
        payload = {
            "items": [
                gpu_node("gpu-0"),
                cpu_node("cpu-0"),
                {"metadata": {"name": "unlabelled"}},
            ]
        }
        self.assertEqual(fs.parse_nodes(payload), {"nodes": 3, "gpu_nodes": 1})

    def test_malformed_entries_are_handled_deterministically(self):
        payload = {
            "items": [
                gpu_node(),
                "not-a-node",
                {"metadata": "labels should be a mapping"},
                {"metadata": {"labels": {"nvidia.com/gpu.present": True}}},
            ]
        }
        self.assertEqual(fs.parse_nodes(payload), {"nodes": 3, "gpu_nodes": 2})

    def test_missing_items_is_zero(self):
        self.assertEqual(fs.parse_nodes({}), {"nodes": 0, "gpu_nodes": 0})
        self.assertEqual(fs.parse_nodes([]), {"nodes": 0, "gpu_nodes": 0})
        self.assertEqual(fs.parse_nodes(None), {"nodes": 0, "gpu_nodes": 0})

    def test_run_as_kubectl_does_not_count_a_gpu(self):
        payload = {"items": [{"metadata": {"labels": {"nvidia.com/gpu.present": "false"}}}]}
        self.assertEqual(fs.parse_nodes(payload), {"nodes": 1, "gpu_nodes": 0})


class LiveStatus(Workspace):
    def two_cluster_document(self):
        return fleet(
            cluster(pools=["onprem-gpu"]),
            cluster(
                name="edge",
                context="kind-edge",
                role="standby",
                pools=["onprem-cpu", "cloud-burst"],
            ),
        )

    def test_live_reports_nodes_and_gpus_per_cluster(self):
        seen = []

        def runner(args):
            seen.append(args)
            return kubectl({"items": [gpu_node("gpu-0"), cpu_node("cpu-0")]})

        code, output = run_main(["--live"], runner=runner)
        self.assertEqual(code, 0)
        self.assertIn("LIVE local (kind-slm): 2 node(s), 1 GPU node(s)", output)
        self.assertEqual(
            seen[0], ["kubectl", "--context", "kind-slm", "get", "nodes", "-o", "json"]
        )

    def test_live_reports_a_failing_cluster_without_aborting_the_others(self):
        def runner(args):
            if "kind-edge" in args:
                return kubectl({}, returncode=1, stderr="error: context not found")
            return kubectl({"items": [gpu_node("gpu-0")]})

        path = self.write_fleet(self.two_cluster_document())
        code, output = run_main(["--live", "--fleet", str(path)], runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("LIVE local (kind-slm): 1 node(s), 1 GPU node(s)", output)
        self.assertIn("FAIL edge (kind-edge): error: context not found", output)

    def test_live_zero_nodes_is_a_failure(self):
        def runner(args):
            return kubectl({"items": []})

        code, output = run_main(["--live"], runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("kubectl returned no nodes", output)

    def test_live_timeout_is_reported_per_cluster(self):
        def runner(args):
            raise subprocess.TimeoutExpired(args, fs.LIVE_TIMEOUT_S)

        code, output = run_main(["--live"], runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("FAIL local (kind-slm)", output)
        self.assertIn("timed out", output)

    def test_live_skips_a_cluster_without_a_context(self):
        path = self.write_fleet(fleet(cluster(context="")))
        code, output = run_main(["--live", "--fleet", str(path)], runner=lambda args: kubectl({}))
        self.assertEqual(code, 0)
        self.assertIn("SKIP local (no context)", output)

    def test_live_does_not_run_when_the_inventory_is_invalid(self):
        def runner(args):  # pragma: no cover - must not be reached
            raise AssertionError(f"kubectl ran against an invalid inventory: {args}")

        path = self.write_fleet(fleet(cluster(role="standby", pools=[])))
        code, output = run_main(["--live", "--fleet", str(path)], runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("FAIL fleet", output)


class NoSecretMaterial(Workspace):
    def test_failure_output_never_repeats_a_credential(self):
        document = fleet(
            cluster(
                gateway_url="https://operator:hunter2@gateway.example/v1?token=abc123#frag"
            )
        )
        path = self.write_fleet(document)
        code, output = run_main(["--check", "--fleet", str(path)])
        self.assertEqual(code, 1)
        for secret in ("hunter2", "abc123", "operator"):
            self.assertNotIn(secret, output)

    def test_status_output_contains_only_the_context_name(self):
        path = self.write_fleet(fleet(cluster(context="kind-slm")))
        code, output = run_main(["--live", "--fleet", str(path)], runner=lambda args: kubectl({"items": [gpu_node()]}))
        self.assertEqual(code, 0)
        self.assertNotIn("token", output.lower())
        self.assertNotIn("kubeconfig", output.lower())


if __name__ == "__main__":
    unittest.main()
