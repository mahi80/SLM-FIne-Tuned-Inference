import unittest

import scripts.cluster_health as ch

HEALTH = {
    "status": "ok",
    "aliases": 4,
    "policy_version": "2026-11-04.1",
    "breakers": {"open": 0, "half_open": 0},
}


def node(name="node-1", ready=True, gpu=1, cpu_pool=False):
    return {"name": name, "ready": ready, "gpu": gpu, "cpu_pool": cpu_pool}


def pod(name, deployment, node_name, phase="Running", waiting=(), restarts=0, ready=True):
    return {
        "name": name,
        "deployment": deployment,
        "node": node_name,
        "phase": phase,
        "waiting": list(waiting),
        "restarts": restarts,
        "ready": ready,
    }


def healthy_inputs():
    nodes = [node("gpu-0", gpu=1), node("cpu-0", gpu=0, cpu_pool=True)]
    deployments = {name: {"desired": 1, "available": 1} for name in ch.PLATFORM_DEPLOYMENTS}
    pods = [
        pod("gateway-1", "gateway", "cpu-0"),
        pod("vllm-1", "vllm-gpu", "gpu-0"),
        pod("cpu-1", "tiersim-cpu", "cpu-0"),
        pod("cloud-1", "tiersim-cloud", "cpu-0"),
    ]
    endpoints = {name: ["10.0.0.1"] for name in ch.PLATFORM_SERVICES}
    return nodes, deployments, pods, endpoints


class EvaluateTest(unittest.TestCase):
    def evaluate(self, nodes, deployments, pods, endpoints, health=None, version="2026-11-04.1", **kwargs):
        return ch.evaluate(
            nodes,
            deployments,
            pods,
            endpoints,
            HEALTH if health is None else health,
            version,
            **kwargs,
        )

    def test_healthy_cluster_has_no_failures_or_warnings(self):
        findings = self.evaluate(*healthy_inputs())
        self.assertEqual([f for f in findings if f["level"] != ch.PASS], [])

    def test_not_ready_node_fails(self):
        nodes, deployments, pods, endpoints = healthy_inputs()
        nodes.append(node("cpu-1", ready=False, gpu=0, cpu_pool=True))
        findings = self.evaluate(nodes, deployments, pods, endpoints)
        self.assertTrue(any(f["level"] == ch.FAIL and "cpu-1" in f["message"] for f in findings))

    def test_no_gpu_fails_unless_allowed(self):
        nodes, deployments, pods, endpoints = healthy_inputs()
        nodes[0]["gpu"] = 0
        findings = self.evaluate(nodes, deployments, pods, endpoints)
        self.assertTrue(any(f["area"] == "gpu" and f["level"] == ch.FAIL for f in findings))

        findings = self.evaluate(nodes, deployments, pods, endpoints, allow_no_gpu=True)
        self.assertTrue(any(f["area"] == "gpu" and f["level"] == ch.WARN for f in findings))
        self.assertFalse(any(f["level"] == ch.FAIL for f in findings))

    def test_unavailable_deployment_fails(self):
        nodes, deployments, pods, endpoints = healthy_inputs()
        deployments["tiersim-cpu"] = {"desired": 1, "available": 0}
        findings = self.evaluate(nodes, deployments, pods, endpoints)
        self.assertTrue(
            any(f["level"] == ch.FAIL and "tiersim-cpu" in f["message"] for f in findings)
        )

    def test_missing_deployment_fails(self):
        nodes, deployments, pods, endpoints = healthy_inputs()
        deployments.pop("gateway")
        findings = self.evaluate(nodes, deployments, pods, endpoints)
        self.assertTrue(any("Deployment/gateway not found" in f["message"] for f in findings))

    def test_crashing_pod_fails(self):
        nodes, deployments, pods, endpoints = healthy_inputs()
        pods[2] = pod("cpu-1", "tiersim-cpu", "cpu-0", waiting=["CrashLoopBackOff"], ready=False)
        findings = self.evaluate(nodes, deployments, pods, endpoints)
        self.assertTrue(any(f["level"] == ch.FAIL and "CrashLoopBackOff" in f["message"] for f in findings))

    def test_restart_loop_warns(self):
        nodes, deployments, pods, endpoints = healthy_inputs()
        pods[1]["restarts"] = 7
        findings = self.evaluate(nodes, deployments, pods, endpoints)
        self.assertTrue(any(f["level"] == ch.WARN and "restarted 7" in f["message"] for f in findings))

    def test_vllm_on_non_gpu_node_fails(self):
        nodes, deployments, pods, endpoints = healthy_inputs()
        pods[1]["node"] = "cpu-0"
        findings = self.evaluate(nodes, deployments, pods, endpoints)
        self.assertTrue(any(f["level"] == ch.FAIL and "advertises no GPU" in f["message"] for f in findings))

    def test_cpu_workload_on_gpu_node_warns(self):
        nodes, deployments, pods, endpoints = healthy_inputs()
        pods[2]["node"] = "gpu-0"
        findings = self.evaluate(nodes, deployments, pods, endpoints)
        self.assertTrue(
            any(f["level"] == ch.WARN and "CPU workloads on GPU nodes" in f["message"] for f in findings)
        )

    def test_empty_endpoints_fail(self):
        nodes, deployments, pods, endpoints = healthy_inputs()
        endpoints["vllm-gpu"] = []
        findings = self.evaluate(nodes, deployments, pods, endpoints)
        self.assertTrue(any(f["level"] == ch.FAIL and "vllm-gpu" in f["message"] for f in findings))

    def test_policy_version_mismatch_fails(self):
        nodes, deployments, pods, endpoints = healthy_inputs()
        findings = self.evaluate(nodes, deployments, pods, endpoints, version="2026-11-05.1")
        self.assertTrue(any(f["area"] == "policy" and f["level"] == ch.FAIL for f in findings))

    def test_missing_policy_configmap_fails(self):
        nodes, deployments, pods, endpoints = healthy_inputs()
        findings = self.evaluate(nodes, deployments, pods, endpoints, version=None)
        self.assertTrue(any("no slm-policy ConfigMap" in f["message"] for f in findings))

    def test_open_breaker_warns_but_does_not_fail(self):
        nodes, deployments, pods, endpoints = healthy_inputs()
        health = dict(HEALTH, breakers={"open": 1, "half_open": 0})
        findings = self.evaluate(nodes, deployments, pods, endpoints, health=health)
        self.assertTrue(any(f["area"] == "gateway" and f["level"] == ch.WARN for f in findings))
        self.assertFalse(any(f["level"] == ch.FAIL for f in findings))

    def test_unreachable_gateway_fails(self):
        nodes, deployments, pods, endpoints = healthy_inputs()
        findings = self.evaluate(nodes, deployments, pods, endpoints, health={})
        self.assertTrue(any(f["area"] == "gateway" and f["level"] == ch.FAIL for f in findings))


class SummaryTest(unittest.TestCase):
    def test_nodes_summary_reads_conditions_and_labels(self):
        payload = {
            "items": [
                {
                    "metadata": {"name": "gpu-0", "labels": {"nvidia.com/gpu.present": "true"}},
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "allocatable": {"nvidia.com/gpu": "2"},
                    },
                },
                {
                    "metadata": {"name": "cpu-0", "labels": {"slm.fabric/pool": "cpu"}},
                    "status": {"conditions": [{"type": "Ready", "status": "False"}]},
                },
            ]
        }
        nodes = ch.nodes_summary(payload)
        self.assertEqual(nodes[0]["gpu"], 2)
        self.assertTrue(nodes[0]["ready"])
        self.assertTrue(nodes[1]["cpu_pool"])
        self.assertFalse(nodes[1]["ready"])

    def test_pods_summary_reads_waiting_reason_and_restarts(self):
        payload = {
            "items": [
                {
                    "metadata": {"name": "gateway-1", "labels": {"app.kubernetes.io/name": "gateway"}},
                    "spec": {"nodeName": "cpu-0"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {
                                "ready": False,
                                "restartCount": 3,
                                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                            }
                        ],
                    },
                }
            ]
        }
        pods = ch.pods_summary(payload)
        self.assertEqual(pods[0]["deployment"], "gateway")
        self.assertEqual(pods[0]["waiting"], ["CrashLoopBackOff"])
        self.assertEqual(pods[0]["restarts"], 3)
        self.assertFalse(pods[0]["ready"])

    def test_endpoints_summary_collects_addresses(self):
        payload = {
            "items": [
                {
                    "metadata": {"name": "gateway"},
                    "subsets": [{"addresses": [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]}],
                },
                {"metadata": {"name": "vllm-gpu"}},
            ]
        }
        endpoints = ch.endpoints_summary(payload)
        self.assertEqual(endpoints["gateway"], ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(endpoints["vllm-gpu"], [])

    def test_policy_version_is_read_from_the_configmap(self):
        payload = {
            "items": [
                {
                    "metadata": {"name": "slm-policy-abc123"},
                    "data": {"policy.yaml": 'version: "2026-11-04.1"\naliases: {}\n'},
                }
            ]
        }
        self.assertEqual(ch.policy_version_from_configmap(payload), "2026-11-04.1")

    def test_unrelated_configmaps_are_ignored(self):
        payload = {"items": [{"metadata": {"name": "kube-root-ca.crt"}, "data": {}}]}
        self.assertIsNone(ch.policy_version_from_configmap(payload))

    def test_counts_and_exit_code(self):
        findings = [
            {"level": ch.PASS, "area": "nodes", "message": "ok"},
            {"level": ch.WARN, "area": "gpu", "message": "warn"},
        ]
        self.assertEqual(ch.counts(findings), {ch.PASS: 1, ch.WARN: 1, ch.FAIL: 0})
        findings.append({"level": ch.FAIL, "area": "endpoints", "message": "bad"})
        self.assertEqual(ch.counts(findings)[ch.FAIL], 1)


if __name__ == "__main__":
    unittest.main()
