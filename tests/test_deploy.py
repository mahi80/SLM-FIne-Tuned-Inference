import shutil
import tempfile
import unittest
from pathlib import Path

from tools import validate_deploy as vd

REPO = Path(__file__).resolve().parent.parent
DIGEST = "sha256:" + "a" * 64


def deployment(
    name="gateway",
    *,
    image=f"slm/gateway:0.5.0@{DIGEST}",
    labels=None,
    args=None,
    pod_security=None,
    container_security=None,
    resources=None,
    replicas=1,
):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": "slm-fabric"},
        "spec": {
            "replicas": replicas,
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {"labels": labels or {"app.kubernetes.io/name": name}},
                "spec": {
                    "securityContext": pod_security
                    if pod_security is not None
                    else {
                        "runAsNonRoot": True,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "app",
                            "image": image,
                            "args": args or [],
                            "resources": resources
                            or {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi"},
                            },
                            "securityContext": container_security
                            if container_security is not None
                            else {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "readOnlyRootFilesystem": True,
                            },
                            "readinessProbe": {"httpGet": {"path": "/health", "port": 8000}},
                        }
                    ],
                },
            },
        },
    }


def service(name, port):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": "slm-fabric"},
        "spec": {"ports": [{"name": "http", "port": port}]},
    }


class ImageChecks(unittest.TestCase):
    def test_digest_is_required(self):
        self.assertEqual(vd.check_images([deployment()]), [])
        errors = vd.check_images([deployment(image="slm/gateway:0.5.0")])
        self.assertTrue(any("is not pinned by digest" in error for error in errors))

    def test_untagged_image_is_rejected(self):
        errors = vd.check_images([deployment(image="slm/gateway")])
        self.assertTrue(any("is not pinned by digest" in error for error in errors))

    def test_latest_is_rejected(self):
        errors = vd.check_images([deployment(image=f"slm/gateway:latest@{DIGEST}")])
        self.assertTrue(any("latest" in error for error in errors))

    def test_digest_is_accepted(self):
        self.assertEqual(vd.check_images([deployment(image=f"slm/gateway@{DIGEST}")]), [])

    def test_malformed_digest_is_rejected(self):
        errors = vd.check_images([deployment(image="slm/gateway:0.5.0@sha256:abc")])
        self.assertTrue(any("malformed digest" in error for error in errors))

    def test_registry_port_stays_in_the_name(self):
        image = f"registry.internal:5000/gateway:0.5.0@{DIGEST}"
        self.assertEqual(vd.parse_image(image), ("registry.internal:5000/gateway", "0.5.0", DIGEST))
        self.assertEqual(vd.check_images([deployment(image=image)]), [])

    def test_registry_port_without_tag_still_needs_a_digest(self):
        errors = vd.check_images([deployment(image="registry.internal:5000/gateway")])
        self.assertTrue(any("is not pinned by digest" in error for error in errors))


class SecurityChecks(unittest.TestCase):
    def test_pod_must_be_restricted(self):
        errors = vd.check_security([deployment(pod_security={})])
        self.assertTrue(any("runAsNonRoot" in error for error in errors))
        self.assertTrue(any("seccompProfile" in error for error in errors))

    def test_container_must_drop_capabilities(self):
        security = {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": []},
            "readOnlyRootFilesystem": True,
        }
        errors = vd.check_security([deployment(container_security=security)])
        self.assertTrue(any("drop ALL" in error for error in errors))

    def test_vllm_keeps_writable_rootfs(self):
        security = {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "readOnlyRootFilesystem": False,
        }
        labels = {"app.kubernetes.io/name": "vllm-gpu"}
        self.assertEqual(
            vd.check_security([deployment(name="vllm-gpu", labels=labels, container_security=security)]),
            [],
        )
        errors = vd.check_security([deployment(container_security=security)])
        self.assertTrue(any("writable" in error for error in errors))

    def test_host_access_is_rejected(self):
        doc = deployment()
        doc["spec"]["template"]["spec"]["hostNetwork"] = True
        doc["spec"]["template"]["spec"]["volumes"] = [{"name": "root", "hostPath": {"path": "/"}}]
        errors = vd.check_security([doc])
        self.assertTrue(any("hostNetwork" in error for error in errors))
        self.assertTrue(any("hostPath" in error for error in errors))


class MountChecks(unittest.TestCase):
    def test_nested_mount_is_rejected(self):
        doc = deployment()
        doc["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] = [
            {"name": "policy", "mountPath": "/app/config"},
            {"name": "tenants", "mountPath": "/app/config/tenants.yaml"},
        ]
        errors = vd.check_mounts([doc])
        self.assertTrue(any("is mounted inside /app/config" in e for e in errors))

    def test_sibling_mounts_are_accepted(self):
        doc = deployment()
        doc["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] = [
            {"name": "policy", "mountPath": "/app/config"},
            {"name": "tenants", "mountPath": "/app/secrets"},
        ]
        self.assertEqual(vd.check_mounts([doc]), [])


class ResourceChecks(unittest.TestCase):
    def test_requests_and_limits_are_required(self):
        errors = vd.check_resources([deployment(resources={"requests": {"cpu": "1"}, "limits": {}})])
        self.assertTrue(any("memory request" in error for error in errors))
        self.assertTrue(any("cpu limit" in error for error in errors))

    def test_gpu_requests_equal_limits(self):
        doc = deployment(name="vllm-gpu", labels={"slm.fabric/tier": "gpu"})
        doc["spec"]["template"]["spec"]["containers"][0]["resources"] = {
            "requests": {"cpu": "4", "memory": "16Gi", "nvidia.com/gpu": "1"},
            "limits": {"cpu": "8", "memory": "24Gi", "nvidia.com/gpu": "2"},
        }
        errors = vd.check_resources([doc])
        self.assertTrue(any("GPU request" in error for error in errors))


def placed_deployment(name):
    """A deployment that satisfies the node-placement rules."""
    doc = deployment(name=name)
    spec = doc["spec"]["template"]["spec"]
    if name == "vllm-gpu":
        spec["nodeSelector"] = {"nvidia.com/gpu.present": "true"}
        spec["tolerations"] = [
            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
        ]
    else:
        spec["affinity"] = {
            "nodeAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "preference": {
                            "matchExpressions": [
                                {"key": "slm.fabric/pool", "operator": "In", "values": ["cpu"]}
                            ]
                        },
                    }
                ]
            }
        }
        spec["tolerations"] = [
            {"key": "slm.fabric/pool", "operator": "Equal", "value": "cpu", "effect": "NoSchedule"}
        ]
    return doc


class PlacementChecks(unittest.TestCase):
    def test_placed_workloads_pass(self):
        docs = [
            placed_deployment("vllm-gpu"),
            placed_deployment("gateway"),
            placed_deployment("tiersim-cpu"),
            placed_deployment("tiersim-cloud"),
        ]
        self.assertEqual(vd.check_placement(docs), [])

    def test_gpu_tier_must_select_gpu_nodes(self):
        doc = placed_deployment("vllm-gpu")
        doc["spec"]["template"]["spec"].pop("nodeSelector")
        errors = vd.check_placement([doc])
        self.assertTrue(any("select GPU nodes" in error for error in errors))

    def test_gpu_tier_must_tolerate_gpu_taint(self):
        doc = placed_deployment("vllm-gpu")
        doc["spec"]["template"]["spec"].pop("tolerations")
        errors = vd.check_placement([doc])
        self.assertTrue(any("nvidia.com/gpu taint" in error for error in errors))

    def test_cpu_tier_must_prefer_the_cpu_pool(self):
        doc = placed_deployment("tiersim-cpu")
        doc["spec"]["template"]["spec"].pop("affinity")
        errors = vd.check_placement([doc])
        self.assertTrue(any("does not prefer slm.fabric/pool=cpu" in error for error in errors))

    def test_cpu_tier_must_tolerate_the_cpu_taint(self):
        doc = placed_deployment("tiersim-cloud")
        doc["spec"]["template"]["spec"].pop("tolerations")
        errors = vd.check_placement([doc])
        self.assertTrue(any("slm.fabric/pool taint" in error for error in errors))

    def test_wrong_pool_does_not_count(self):
        doc = placed_deployment("gateway")
        expressions = doc["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
            "preferredDuringSchedulingIgnoredDuringExecution"
        ][0]["preference"]["matchExpressions"]
        expressions[0]["values"] = ["system"]
        errors = vd.check_placement([doc])
        self.assertTrue(any("does not prefer" in error for error in errors))


class AlertChecks(unittest.TestCase):
    @staticmethod
    def rule(*alerts):
        return {
            "apiVersion": "monitoring.coreos.com/v1",
            "kind": "PrometheusRule",
            "metadata": {"name": "slm-fabric", "namespace": "slm-fabric"},
            "spec": {
                "groups": [
                    {
                        "name": "slm-fabric.platform",
                        "rules": [
                            {
                                "alert": name,
                                "expr": "up == 0",
                                "for": "5m",
                                "labels": {"severity": "warning"},
                            }
                            for name in alerts
                        ],
                    }
                ]
            },
        }

    def test_all_platform_alerts_are_required(self):
        errors = vd.check_alerts([self.rule("SlmGatewayUnavailable")])
        self.assertTrue(any("SlmBreakerOpen" in error for error in errors))

    def test_complete_rule_set_passes(self):
        alerts = sorted(vd.REQUIRED_ALERTS)
        self.assertEqual(vd.check_alerts([self.rule(*alerts)]), [])

    def test_missing_prometheusrule_is_an_error(self):
        errors = vd.check_alerts([])
        self.assertTrue(any("no PrometheusRule" in error for error in errors))

    def test_unrouted_alert_is_rejected(self):
        doc = self.rule(*vd.REQUIRED_ALERTS)
        rules = doc["spec"]["groups"][0]["rules"]
        rules[0]["labels"] = {}
        rules[1]["for"] = ""
        errors = vd.check_alerts([doc])
        self.assertTrue(any("no warning/critical severity" in error for error in errors))
        self.assertTrue(any("no for duration" in error for error in errors))


class WiringChecks(unittest.TestCase):
    def test_policy_backends_must_have_services(self):
        policy_text = (
            "aliases:\n"
            "  slm-small:\n"
            "    candidates:\n"
            "      - name: gpu\n"
            "        backend: http://vllm-gpu:8000\n"
            "      - name: cpu\n"
            "        backend: http://tiersim-cpu:8001\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.yaml"
            path.write_text(policy_text, encoding="utf-8")
            configmap = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "slm-policy-abc", "namespace": "slm-fabric"},
                "data": {"policy.yaml": policy_text},
            }
            docs = [service("vllm-gpu", 8000), service("tiersim-cpu", 8001), configmap]
            self.assertEqual(vd.check_policy_binding(docs, path), [])

            errors = vd.check_policy_binding(docs[:1] + [configmap], path)
            self.assertTrue(any("tiersim-cpu" in error for error in errors))

            configmap["data"]["policy.yaml"] = policy_text.replace("8001", "9999")
            errors = vd.check_policy_binding(docs, path)
            self.assertTrue(any("does not match" in error for error in errors))

    def test_service_port_must_match(self):
        policy_text = (
            "aliases:\n"
            "  slm-small:\n"
            "    candidates:\n"
            "      - name: gpu\n"
            "        backend: http://vllm-gpu:8000\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.yaml"
            path.write_text(policy_text, encoding="utf-8")
            configmap = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "slm-policy-abc", "namespace": "slm-fabric"},
                "data": {"policy.yaml": policy_text},
            }
            errors = vd.check_policy_binding([service("vllm-gpu", 9000), configmap], path)
            self.assertTrue(any("does not expose port 8000" in error for error in errors))

    def test_gateway_is_single_replica_and_mounts_config(self):
        doc = deployment(name="gateway")
        doc["spec"]["template"]["spec"]["volumes"] = [
            {"name": "policy", "configMap": {"name": "slm-policy-abc"}},
            {"name": "tenants", "secret": {"secretName": "slm-tenants"}},
        ]
        self.assertEqual(vd.check_gateway_state([doc]), [])

        doc["spec"]["replicas"] = 2
        doc["spec"]["template"]["spec"]["volumes"] = []
        errors = vd.check_gateway_state([doc])
        self.assertTrue(any("replicas" in error for error in errors))
        self.assertTrue(any("policy ConfigMap" in error for error in errors))
        self.assertTrue(any("slm-tenants" in error for error in errors))

    def test_secrets_are_never_rendered(self):
        docs = [{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "x"}}]
        self.assertTrue(vd.check_no_secrets(docs))

    def test_namespaced_objects_are_in_slm_fabric(self):
        doc = deployment()
        doc["metadata"]["namespace"] = "default"
        errors = vd.check_namespaces([doc])
        self.assertTrue(any("slm-fabric" in error for error in errors))


@unittest.skipUnless(shutil.which("kubectl"), "kubectl is required to render overlays")
class RenderedOverlays(unittest.TestCase):
    def test_every_overlay_validates(self):
        for overlay in vd.OVERLAYS:
            with self.subTest(overlay=overlay):
                docs = vd.render_overlay(overlay)
                errors = vd.validate(docs, overlay, REPO / "config/policy.yaml")
                self.assertEqual(errors, [], f"{overlay}: {errors}")

    def test_policy_configmap_is_the_source_file(self):
        docs = vd.render_overlay("openshift")
        expected = (REPO / "config/policy.yaml").read_text(encoding="utf-8").strip()
        configmap = next(
            d for d in docs if d.get("kind") == "ConfigMap" and str(d["metadata"]["name"]).startswith("slm-policy")
        )
        self.assertEqual(configmap["data"]["policy.yaml"].strip(), expected)

    def test_platform_alerts_ship_with_operator_overlays(self):
        for overlay in ("openshift", "airgap"):
            with self.subTest(overlay=overlay):
                self.assertEqual(vd.check_alerts(vd.render_overlay(overlay)), [])

    def test_vanilla_has_no_operator_custom_resources(self):
        kinds = {doc.get("kind") for doc in vd.render_overlay("vanilla")}
        self.assertNotIn("PrometheusRule", kinds)
        self.assertNotIn("ServiceMonitor", kinds)


if __name__ == "__main__":
    unittest.main()
