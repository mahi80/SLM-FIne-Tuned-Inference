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


class AutoscalingChecks(unittest.TestCase):
    CAPACITY = vd.load_capacity()

    @classmethod
    def scaled_object(cls, **overrides):
        spec = {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "vllm-gpu"},
            "minReplicaCount": cls.CAPACITY["min_replicas"],
            "maxReplicaCount": cls.CAPACITY["max_replicas"],
            "triggers": [
                {
                    "type": "prometheus",
                    "name": metric["name"].replace("_", "-"),
                    "metadata": {
                        "metricName": metric["keda_metric"],
                        "query": metric["query"],
                        "threshold": metric["threshold"],
                        "serverAddress": "http://prometheus-operated.monitoring.svc:9090",
                    },
                }
                for metric in cls.CAPACITY["metrics"]
            ],
        }
        spec.update(overrides)
        return {
            "apiVersion": "keda.sh/v1alpha1",
            "kind": "ScaledObject",
            "metadata": {"name": "vllm-gpu", "namespace": "slm-fabric"},
            "spec": spec,
        }

    @staticmethod
    def pvc(name, *modes):
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": "slm-fabric"},
            "spec": {"accessModes": list(modes)},
        }

    @staticmethod
    def adapter_set(*names):
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "slm-adapter-set", "namespace": "slm-fabric"},
            "data": {"adapters.txt": "\n".join(names) + "\n"},
        }

    @staticmethod
    def gpu_deployment(*args):
        doc = deployment(name="vllm-gpu", args=list(args) or None)
        del doc["spec"]["replicas"]
        return doc

    def docs(self, deployment_doc=None, adapter_configmap=None, **overrides):
        return [
            deployment_doc
            or self.gpu_deployment("--lora-modules", "triage-v1=/adapters/triage-v1", "triage-v2=/adapters/triage-v2"),
            self.pvc("slm-model-cache", "ReadWriteMany"),
            self.pvc("slm-adapters", "ReadWriteMany"),
            adapter_configmap or self.adapter_set("triage-v1", "triage-v2"),
            self.scaled_object(**overrides),
        ]

    def test_matching_scaler_passes(self):
        self.assertEqual(vd.check_autoscaling(self.docs(), self.CAPACITY), [])

    def test_scaler_cannot_target_the_gateway(self):
        scaled = self.scaled_object(
            scaleTargetRef={"apiVersion": "apps/v1", "kind": "Deployment", "name": "gateway"}
        )
        errors = vd.check_autoscaling([scaled], self.CAPACITY)
        self.assertTrue(any("scales the gateway" in error for error in errors))

    def test_gpu_floor_cannot_be_zero(self):
        capacity = {**self.CAPACITY, "min_replicas": 0}
        errors = vd.check_autoscaling(self.docs(), capacity)
        self.assertTrue(any("warm floor" in error for error in errors))

    def test_pinned_replicas_fight_the_scaler(self):
        doc = self.gpu_deployment("--lora-modules", "triage-v1=/adapters/triage-v1", "triage-v2=/adapters/triage-v2")
        doc["spec"]["replicas"] = 1
        errors = vd.check_autoscaling(self.docs(deployment_doc=doc), self.CAPACITY)
        self.assertTrue(any("selfHeal" in error for error in errors))

    def test_bounds_and_thresholds_come_from_capacity(self):
        errors = vd.check_autoscaling(self.docs(minReplicaCount=2, maxReplicaCount=9), self.CAPACITY)
        self.assertTrue(any("minReplicaCount" in error for error in errors))
        self.assertTrue(any("maxReplicaCount" in error for error in errors))

        scaled = self.scaled_object()
        scaled["spec"]["triggers"][0]["metadata"]["threshold"] = "99"
        docs = self.docs()
        docs[-1] = scaled
        errors = vd.check_autoscaling(docs, self.CAPACITY)
        self.assertTrue(any("threshold" in error for error in errors))

    def test_https_scraper_needs_authentication(self):
        scaled = self.scaled_object()
        for trigger in scaled["spec"]["triggers"]:
            trigger["metadata"]["serverAddress"] = "https://thanos-querier:9091"
        errors = vd.check_autoscaling([scaled], self.CAPACITY)
        self.assertEqual(sum("without authenticationRef" in error for error in errors), 2)

    def test_read_write_once_claim_blocks_a_second_replica(self):
        docs = self.docs()
        docs[1] = self.pvc("slm-model-cache", "ReadWriteOnce")
        errors = vd.check_autoscaling(docs, self.CAPACITY)
        self.assertTrue(any("slm-model-cache" in error and "ReadWriteMany" in error for error in errors))

    def test_replica_must_preload_declared_adapters(self):
        errors = vd.check_autoscaling(self.docs(deployment_doc=self.gpu_deployment("--enable-lora")), self.CAPACITY)
        self.assertTrue(any("starts no adapters" in error for error in errors))

        partial = self.gpu_deployment("--lora-modules", "triage-v1=/adapters/triage-v1")
        errors = vd.check_autoscaling(self.docs(deployment_doc=partial), self.CAPACITY)
        self.assertTrue(any("triage-v2" in error for error in errors))


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
        self.assertNotIn("ScaledObject", kinds)

    def test_gpu_scaler_ships_with_operator_overlays(self):
        capacity = vd.load_capacity()
        for overlay in ("openshift", "airgap"):
            with self.subTest(overlay=overlay):
                docs = vd.render_overlay(overlay)
                scaled = next(doc for doc in docs if doc.get("kind") == "ScaledObject")
                self.assertEqual(scaled["spec"]["minReplicaCount"], capacity["min_replicas"])
                self.assertEqual(scaled["spec"]["maxReplicaCount"], capacity["max_replicas"])
                self.assertEqual(vd.check_autoscaling(docs, capacity), [])
                vllm = next(doc for doc in docs if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "vllm-gpu")
                self.assertNotIn("replicas", vllm["spec"])


if __name__ == "__main__":
    unittest.main()
