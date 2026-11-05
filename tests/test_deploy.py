import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

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
    POOL = CAPACITY["pools"]["onprem-gpu"]

    @classmethod
    def registry(cls, **pool_overrides):
        return {
            "version": cls.CAPACITY["version"],
            "pools": {"onprem-gpu": {**cls.POOL, **pool_overrides}},
        }

    @classmethod
    def scaled_object(cls, **overrides):
        spec = {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "vllm-gpu"},
            "minReplicaCount": cls.POOL["min_replicas"],
            "maxReplicaCount": cls.POOL["max_replicas"],
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
                for metric in cls.POOL["metrics"]
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
        doc["spec"]["template"]["spec"]["volumes"] = [
            {"name": "model-cache", "persistentVolumeClaim": {"claimName": "slm-model-cache"}},
            {"name": "adapters", "persistentVolumeClaim": {"claimName": "slm-adapters"}},
        ]
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
        errors = vd.check_autoscaling(self.docs(), self.registry(min_replicas=0))
        self.assertTrue(any("warm floor" in error for error in errors))

    def test_a_pool_that_scales_needs_a_metric(self):
        errors = vd.check_autoscaling(self.docs(), self.registry(metrics=[]))
        self.assertTrue(any("declares no metric" in error for error in errors))
        self.assertTrue(any("declares no scaling metric" in error for error in errors))

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


class PoolChecks(unittest.TestCase):
    """The registry is the one place a route may point, so drift is an error."""

    CAPACITY = vd.load_capacity()

    def check(self, candidate):
        policy = {"aliases": {"slm-small": {"candidates": [candidate]}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.yaml"
            path.write_text(yaml.safe_dump(policy), encoding="utf-8")
            return vd.check_pools(self.CAPACITY, path)

    def candidate(self, **overrides):
        body = {
            "name": "gpu",
            "compute": "gpu",
            "deployment": "onprem",
            "pool": "onprem-gpu",
            "backend": "http://vllm-gpu:8000",
        }
        body.update(overrides)
        return body

    def test_repository_candidates_resolve_to_declared_pools(self):
        self.assertEqual(vd.check_pools(self.CAPACITY, REPO / "config/policy.yaml"), [])

    def test_unknown_pool_is_rejected(self):
        errors = self.check(self.candidate(pool="onprem-h100"))
        self.assertTrue(any("does not declare" in error for error in errors))

    def test_candidate_compute_must_match_the_pool(self):
        errors = self.check(self.candidate(compute="cpu"))
        self.assertTrue(any("compute 'cpu'" in error for error in errors))

    def test_candidate_deployment_must_match_the_pool(self):
        errors = self.check(self.candidate(deployment="cloud"))
        self.assertTrue(any("deployment 'cloud'" in error for error in errors))

    def test_candidate_backend_must_be_the_pool_endpoint(self):
        errors = self.check(self.candidate(backend="http://vllm-gpu:8001"))
        self.assertTrue(any("endpoint" in error for error in errors))

    def test_trailing_slash_normalises(self):
        self.assertEqual(self.check(self.candidate(backend="http://vllm-gpu:8000/")), [])

    def test_registry_rejects_duplicate_pools(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capacity.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "version": 2,
                        "pools": [
                            {
                                "name": "onprem-gpu",
                                "compute": "gpu",
                                "deployment": "onprem",
                                "endpoint": "http://vllm-gpu:8000",
                                "workload": "vllm-gpu",
                                "replicas": {"min": 1, "max": 1},
                                "node_pool": {"label": "nvidia.com/gpu.present"},
                            }
                        ]
                        * 2,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                vd.load_capacity(path)

    def test_registry_rejects_inverted_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capacity.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "pools": [
                            {
                                "name": "onprem-gpu",
                                "compute": "gpu",
                                "deployment": "onprem",
                                "endpoint": "http://vllm-gpu:8000",
                                "workload": "vllm-gpu",
                                "replicas": {"min": 3, "max": 1},
                                "node_pool": {"label": "nvidia.com/gpu.present"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                vd.load_capacity(path)


class CloudPoolSnippets(unittest.TestCase):
    """Node pools are provisioned outside the app tree, so nothing else checks them."""

    CLOUD = REPO / "deploy/cloud"
    CAPACITY = vd.load_capacity()

    def test_yaml_pools_carry_the_gpu_contract(self):
        gpu_pool = self.CAPACITY["pools"]["onprem-gpu"]["node_pool"]
        documents = [
            doc
            for path in sorted(self.CLOUD.glob("*.yaml"))
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if doc and doc.get("kind") == "NodePool" and "gpu" in doc["metadata"]["name"]
        ]
        self.assertTrue(documents)
        for doc in documents:
            with self.subTest(pool=doc["metadata"]["name"]):
                template = doc["spec"]["template"]
                self.assertEqual(template["metadata"]["labels"][gpu_pool["label"]], "true")
                taints = {taint["key"] for taint in template["spec"]["taints"]}
                self.assertIn(gpu_pool["taint"], taints)

    def test_scripts_are_parseable_and_apply_the_contract(self):
        gpu_pool = self.CAPACITY["pools"]["onprem-gpu"]["node_pool"]
        cpu_pool = self.CAPACITY["pools"]["onprem-cpu"]["node_pool"]
        scripts = sorted(self.CLOUD.glob("*.sh"))
        self.assertTrue(scripts)
        for path in scripts:
            with self.subTest(script=path.name):
                subprocess.run(
                    ["bash", "-n", path.relative_to(REPO).as_posix()],
                    cwd=REPO,
                    check=True,
                    capture_output=True,
                )
                text = path.read_text(encoding="utf-8")
                self.assertIn(f"{gpu_pool['label']}=true", text)
                self.assertIn(f"{gpu_pool['taint']}=", text)
                if "nodepools" in path.name:
                    self.assertIn(f"{cpu_pool['label']}=cpu", text)
                    self.assertIn(f"{cpu_pool['taint']}=cpu", text)


class ExternalBackendChecks(unittest.TestCase):
    """An external route fails closed unless key, CA and egress all line up."""

    EXTERNAL_POLICY = (
        "aliases:\n"
        "  burst:\n"
        "    candidates:\n"
        "      - name: cloud\n"
        "        compute: gpu\n"
        "        deployment: cloud\n"
        "        pool: cloud-prod\n"
        "        backend: https://inference.example.com\n"
        "        upstream_model: model-cloud\n"
        "        access:\n"
        "          key: external-prod\n"
    )

    @staticmethod
    def policy_file(tmp: str, text: str) -> Path:
        path = Path(tmp) / "policy.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    def capacity(*pools) -> dict:
        return {"version": 2, "pools": {pool["name"]: pool for pool in pools}}

    @staticmethod
    def external_pool(**overrides) -> dict:
        pool = {
            "name": "cloud-prod",
            "compute": "gpu",
            "deployment": "cloud",
            "external": True,
            "provider": "aws",
            "modelled": False,
            "endpoint": "https://inference.example.com",
            "workload": "",
            "min_replicas": 0,
            "max_replicas": 0,
            "metrics": [],
            "prerequisites": {},
            "node_pool": {},
        }
        pool.update(overrides)
        return pool

    def gateway(self, *, optional=True, env=None, mount=True) -> dict:
        doc = deployment(name="gateway")
        spec = doc["spec"]["template"]["spec"]
        spec["volumes"] = [
            {"name": "policy", "configMap": {"name": "slm-policy-abc"}},
            {"name": "tenants", "secret": {"secretName": "slm-tenants"}},
            {
                "name": "backend-keys",
                "secret": {"secretName": "slm-backend-keys", "optional": optional},
            },
        ]
        container = spec["containers"][0]
        if mount:
            container["volumeMounts"] = [
                {"name": "backend-keys", "mountPath": "/app/backend-keys", "readOnly": True}
            ]
        container["env"] = env if env is not None else [
            {"name": "SLM_BACKEND_KEYS", "value": "/app/backend-keys/keys.yaml"}
        ]
        return doc

    @staticmethod
    def cloud_egress(**overrides) -> dict:
        rule = {
            "to": [{"ipBlock": {"cidr": "198.51.100.0/24"}}],
            "ports": [{"protocol": "TCP", "port": 443}],
        }
        rule.update(overrides)
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "allow-gateway-cloud-egress", "namespace": "slm-fabric"},
            "spec": {
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": "gateway"}},
                "policyTypes": ["Egress"],
                "egress": [rule],
            },
        }

    def check(self, docs, policy_text=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.policy_file(tmp, policy_text or self.EXTERNAL_POLICY)
            return vd.check_external_backends(docs, path)

    def test_shipped_overlays_need_no_external_wiring(self):
        docs = [deployment(name="gateway")]
        with tempfile.TemporaryDirectory() as tmp:
            path = self.policy_file(tmp, "aliases:\n  a:\n    candidates:\n      - backend: http://vllm-gpu:8000\n")
            self.assertEqual(vd.check_external_backends(docs, path), [])

    def test_external_candidate_needs_a_keys_mount(self):
        errors = self.check([self.gateway(mount=False), self.cloud_egress()])
        self.assertTrue(any("SLM_BACKEND_KEYS" in error or "mount" in error for error in errors))

    def test_keys_secret_must_be_optional(self):
        errors = self.check([self.gateway(optional=False), self.cloud_egress()])
        self.assertTrue(any("not optional" in error for error in errors))

    def test_external_candidate_needs_an_egress_rule(self):
        errors = self.check([self.gateway()])
        self.assertTrue(any("allow-gateway-cloud-egress" in error for error in errors))

    def test_complete_external_wiring_passes(self):
        self.assertEqual(self.check([self.gateway(), self.cloud_egress()]), [])

    def test_egress_rule_without_a_consumer_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.policy_file(
                tmp, "aliases:\n  a:\n    candidates:\n      - backend: http://vllm-gpu:8000\n"
            )
            docs = [deployment(name="gateway"), self.cloud_egress()]
            errors = vd.check_external_backends(docs, path)
        self.assertTrue(any("no policy candidate is external" in error for error in errors))

    def test_egress_cannot_open_the_whole_internet(self):
        policy = self.cloud_egress(to=[{"ipBlock": {"cidr": "0.0.0.0/0"}}])
        errors = vd.check_cloud_egress(policy)
        self.assertTrue(any("whole internet" in error for error in errors))

    def test_egress_cannot_reach_private_space(self):
        for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
            with self.subTest(cidr=cidr):
                errors = vd.check_cloud_egress(
                    self.cloud_egress(to=[{"ipBlock": {"cidr": cidr}}])
                )
                self.assertTrue(any("private" in error for error in errors))

    def test_egress_to_a_pod_is_not_a_cloud_route(self):
        errors = vd.check_cloud_egress(
            self.cloud_egress(to=[{"podSelector": {"matchLabels": {"a": "b"}}}])
        )
        self.assertTrue(any("not an ipBlock" in error for error in errors))

    def test_egress_cannot_open_another_port(self):
        errors = vd.check_cloud_egress(
            self.cloud_egress(ports=[{"protocol": "TCP", "port": 80}])
        )
        self.assertTrue(any("only TCP 443" in error for error in errors))

    def test_egress_must_target_the_gateway(self):
        policy = self.cloud_egress()
        policy["spec"]["podSelector"]["matchLabels"] = {"app.kubernetes.io/name": "other"}
        self.assertTrue(vd.check_cloud_egress(policy))

    def test_shipped_component_is_valid(self):
        path = REPO / "deploy/kustomize/components/cloud-egress/networkpolicy.yaml"
        docs = vd.load_documents(path.read_text(encoding="utf-8"))
        self.assertEqual(len(docs), 1)
        self.assertEqual(vd.check_cloud_egress(docs[0]), [])

    def test_airgap_overlay_cannot_open_cloud_egress(self):
        errors = vd.check_overlay("airgap", [self.cloud_egress()])
        self.assertTrue(any("cloud provider" in error for error in errors))

    def test_candidate_access_and_pool_external_must_agree(self):
        credentialed_internal = (
            "aliases:\n"
            "  burst:\n"
            "    candidates:\n"
            "      - name: cloud\n"
            "        compute: gpu\n"
            "        deployment: cloud\n"
            "        pool: cloud-burst\n"
            "        backend: http://tiersim-cloud:8001\n"
            "        access: {key: external-prod}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self.policy_file(tmp, credentialed_internal)
            errors = vd.check_pools(vd.load_capacity(), path)
            self.assertTrue(any("is in-cluster" in error for error in errors))

            capacity = self.capacity(self.external_pool())
            no_access = self.EXTERNAL_POLICY.replace(
                "        access:\n          key: external-prod\n", ""
            )
            path = self.policy_file(tmp, no_access)
            errors = vd.check_pools(capacity, path)
            self.assertTrue(any("no access block" in error for error in errors))

            path = self.policy_file(tmp, self.EXTERNAL_POLICY)
            self.assertEqual(vd.check_pools(capacity, path), [])

    def test_registry_rejects_an_insecure_external_pool(self):
        for overrides in (
            {"endpoint": "http://inference.example.com"},
            {"workload": "remote-gpu"},
            {"node_pool": {"label": "nvidia.com/gpu.present"}},
        ):
            with self.subTest(overrides=overrides):
                pool = {
                    "name": "cloud-prod",
                    "compute": "gpu",
                    "deployment": "cloud",
                    "external": True,
                    "provider": "aws",
                    "endpoint": "https://inference.example.com",
                    "workload": "",
                    "node_pool": {},
                }
                pool.update(overrides)
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "capacity.yaml"
                    path.write_text(
                        yaml.safe_dump({"version": 2, "pools": [pool]}), encoding="utf-8"
                    )
                    with self.assertRaises(RuntimeError):
                        vd.load_capacity(path)

    def test_external_candidate_needs_no_service(self):
        policy_text = (
            "aliases:\n"
            "  burst:\n"
            "    candidates:\n"
            "      - name: cloud\n"
            "        backend: https://inference.example.com\n"
            "        access: {key: external-prod}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self.policy_file(tmp, policy_text)
            configmap = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "slm-policy-abc", "namespace": "slm-fabric"},
                "data": {"policy.yaml": policy_text},
            }
            self.assertEqual(vd.check_policy_binding([configmap], path), [])


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

    def test_gateway_ships_optional_backend_key_plumbing(self):
        docs = vd.render_overlay("vanilla")
        gateway = next(
            doc
            for doc in docs
            if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "gateway"
        )
        spec = vd.pod_spec(gateway)
        volume = next(
            volume
            for volume in spec["volumes"]
            if (volume.get("secret") or {}).get("secretName") == vd.BACKEND_KEYS_SECRET
        )
        self.assertTrue(volume["secret"]["optional"])
        env = vd._env(spec["containers"][0])
        self.assertEqual(env["SLM_BACKEND_KEYS"], "/app/backend-keys/keys.yaml")
        mounts = {
            mount["name"]: mount["mountPath"]
            for mount in spec["containers"][0]["volumeMounts"]
        }
        self.assertEqual(mounts["backend-keys"], "/app/backend-keys")

    def test_gpu_scaler_ships_with_operator_overlays(self):
        capacity = vd.load_capacity()
        pool = capacity["pools"]["onprem-gpu"]
        for overlay in ("openshift", "airgap"):
            with self.subTest(overlay=overlay):
                docs = vd.render_overlay(overlay)
                scaled = next(doc for doc in docs if doc.get("kind") == "ScaledObject")
                self.assertEqual(scaled["spec"]["minReplicaCount"], pool["min_replicas"])
                self.assertEqual(scaled["spec"]["maxReplicaCount"], pool["max_replicas"])
                self.assertEqual(vd.check_autoscaling(docs, capacity), [])
                vllm = next(doc for doc in docs if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "vllm-gpu")
                self.assertNotIn("replicas", vllm["spec"])


if __name__ == "__main__":
    unittest.main()
