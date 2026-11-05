"""Static checks on the rendered deployment tree.

No cluster and no `oc` required: this renders the Kustomize overlays with
`kubectl kustomize` and asserts the invariants the platform depends on.

    python tools/validate_deploy.py [--overlay openshift|vanilla|airgap]

What it protects, in order of how expensive the mistake is:

  * the policy ConfigMap is byte-identical to config/policy.yaml, and every
    backend URL in the policy resolves to a Service in the rendered set;
  * the GPU runtime and model revision are the pinned ones from Phase 0;
  * no Secret material is in Git, and no workload can reach the host;
  * every container declares resources and every long-running workload has a
    readiness probe;
  * the GPU tier is pinned to GPU nodes and the remaining workloads prefer a
    tainted CPU pool, so simulators cannot consume GPU capacity;
  * the five platform alerts exist on every overlay that installs the
    Prometheus Operator, each with a window and a severity;
  * the GPU scaler matches config/capacity.yaml, keeps a warm floor, shares an
    RWX claim and preloads every declared adapter — and never scales the
    gateway, whose quota, breaker and audit state is in-process.
  * the gateway is a single replica, because its quota, breaker and audit state
    is in-process.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

import yaml

REPO = Path(__file__).resolve().parent.parent
OVERLAY_ROOT = REPO / "deploy/kustomize/overlays"
OVERLAYS = ("openshift", "vanilla", "airgap")

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
LONG_RUNNING_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}
CLUSTER_SCOPED = {
    "Namespace",
    "ClusterPolicy",
    "ImageDigestMirrorSet",
    "ImageTagMirrorSet",
}

VLLM_IMAGE = "vllm/vllm-openai:v0.33.0"
MODEL_REVISION = "b37d1ccb77e27a70fe4222d995fda27940eead5a"
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def release_identity(path: Path = REPO / "config/release.yaml") -> tuple[str, str, str]:
    """Pinned runtime, revision and served name come from the release identity,
    not from constants in this file, so the static checks and the signed
    manifest cannot drift apart."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    model = data.get("model") or {}
    vllm = next(
        (image for image in data.get("images") or [] if image.get("name") == "vllm/vllm-openai"),
        None,
    )
    try:
        return (
            f"vllm/vllm-openai:{vllm['tag']}",
            str(model["revision"]),
            str(model["served_name"]),
        )
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"{path} does not declare the vLLM runtime and model pin") from exc


VLLM_IMAGE, MODEL_REVISION, SERVED_MODEL_NAME = release_identity()
REQUIRED_DEPLOYMENTS = {"vllm-gpu", "gateway", "tiersim-cpu", "tiersim-cloud"}
REQUIRED_SERVICES = {"vllm-gpu", "gateway", "tiersim-cpu", "tiersim-cloud"}

GPU_LABEL = "slm.fabric/tier"
VLLM_LABEL = "app.kubernetes.io/name"
READ_ONLY_EXEMPT = {"vllm-gpu"}
GPU_NODE_LABEL = "nvidia.com/gpu.present"
GPU_TAINT = "nvidia.com/gpu"
CPU_POOL_LABEL = "slm.fabric/pool"
CPU_POOL_VALUE = "cpu"
CPU_DEPLOYMENTS = ("gateway", "tiersim-cpu", "tiersim-cloud")
REQUIRED_ALERTS = {
    "SlmGatewayUnavailable",
    "SlmGpuTierUnavailable",
    "SlmNoGpuCapacity",
    "SlmBreakerOpen",
    "SlmPodRestartLoop",
}


def load_documents(text: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def render_overlay(overlay: str, kubectl: str = "kubectl") -> list[dict]:
    path = OVERLAY_ROOT / overlay
    proc = subprocess.run(
        [kubectl, "kustomize", "--load-restrictor", "LoadRestrictionsNone", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"kubectl kustomize failed for {overlay}: {proc.stderr.strip()}")
    return load_documents(proc.stdout)


def workloads(docs: Iterable[dict]):
    for doc in docs:
        if doc.get("kind") in WORKLOAD_KINDS:
            yield doc


def pod_spec(doc: dict) -> dict:
    return doc.get("spec", {}).get("template", {}).get("spec", {}) or {}


def pod_labels(doc: dict) -> dict:
    return doc.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {}) or {}


def containers(spec: dict) -> list[dict]:
    return list(spec.get("containers") or []) + list(spec.get("initContainers") or [])


def parse_image(image: str) -> tuple[str, str | None, str | None]:
    """Split `name[:tag][@sha256:...]`; a registry port stays part of the name."""
    name, _, digest = image.partition("@")
    tail = name.rsplit("/", 1)[-1]
    tag = None
    if ":" in tail:
        name, tag = name.rsplit(":", 1)
    return name, tag, digest or None


def label(doc: dict) -> str:
    return f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')}"


def find(docs: Iterable[dict], kind: str, name: str | None = None) -> dict | None:
    for doc in docs:
        if doc.get("kind") != kind:
            continue
        if name is None or doc.get("metadata", {}).get("name") == name:
            return doc
    return None


def check_images(docs: list[dict]) -> list[str]:
    errors = []
    for doc in workloads(docs):
        for container in containers(pod_spec(doc)):
            image = container.get("image")
            where = f"{label(doc)} container {container.get('name')}"
            if not image:
                errors.append(f"{where}: no image")
                continue
            _, tag, digest = parse_image(image)
            if not digest:
                errors.append(f"{where}: {image} is not pinned by digest")
            elif not DIGEST.match(digest):
                errors.append(f"{where}: {image} has a malformed digest")
            if tag == "latest":
                errors.append(f"{where}: {image} uses the latest tag")
    return errors


def check_model_pin(docs: list[dict]) -> list[str]:
    errors = []
    for doc in workloads(docs):
        spec = pod_spec(doc)
        arg_list = [
            str(a) for c in spec.get("containers") or [] for a in (c.get("args") or [])
        ]
        args = " ".join(arg_list)
        if "--served-model-name" not in args:
            continue
        for container in spec.get("containers") or []:
            if (container.get("image") or "").split("@")[0] != VLLM_IMAGE:
                errors.append(f"{label(doc)}: vLLM image is {container.get('image')}, expected {VLLM_IMAGE}")
        if MODEL_REVISION not in args:
            errors.append(f"{label(doc)}: model revision is not pinned to {MODEL_REVISION}")
        index = arg_list.index("--served-model-name")
        if index + 1 >= len(arg_list) or arg_list[index + 1] != SERVED_MODEL_NAME:
            errors.append(f"{label(doc)}: served model name is not {SERVED_MODEL_NAME}")
        if "--enable-lora" not in args:
            errors.append(f"{label(doc)}: multi-LoRA serving is not enabled")
    return errors


def check_security(docs: list[dict]) -> list[str]:
    errors = []
    for doc in workloads(docs):
        spec = pod_spec(doc)
        where = label(doc)
        pod_sc = spec.get("securityContext") or {}
        if pod_sc.get("runAsNonRoot") is not True:
            errors.append(f"{where}: pod securityContext.runAsNonRoot is not true")
        if (pod_sc.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
            errors.append(f"{where}: seccompProfile is not RuntimeDefault")
        if pod_sc.get("runAsUser") == 0:
            errors.append(f"{where}: pod runs as UID 0")
        for field in ("hostNetwork", "hostPID", "hostIPC"):
            if spec.get(field):
                errors.append(f"{where}: {field} is enabled")
        for volume in spec.get("volumes") or []:
            if "hostPath" in volume:
                errors.append(f"{where}: hostPath volume {volume.get('name')}")
        exempt = pod_labels(doc).get(VLLM_LABEL) in READ_ONLY_EXEMPT
        for container in containers(spec):
            name = f"{where} container {container.get('name')}"
            sc = container.get("securityContext") or {}
            if sc.get("allowPrivilegeEscalation") is not False:
                errors.append(f"{name}: allowPrivilegeEscalation is not false")
            if "ALL" not in ((sc.get("capabilities") or {}).get("drop") or []):
                errors.append(f"{name}: capabilities do not drop ALL")
            if sc.get("privileged") is True:
                errors.append(f"{name}: privileged")
            if not exempt and sc.get("readOnlyRootFilesystem") is not True:
                errors.append(f"{name}: root filesystem is writable")
    return errors


def check_resources(docs: list[dict]) -> list[str]:
    errors = []
    for doc in workloads(docs):
        spec = pod_spec(doc)
        gpu = pod_labels(doc).get(GPU_LABEL) == "gpu"
        for container in spec.get("containers") or []:
            name = f"{label(doc)} container {container.get('name')}"
            resources = container.get("resources") or {}
            requests = resources.get("requests") or {}
            limits = resources.get("limits") or {}
            for field in ("cpu", "memory"):
                if field not in requests:
                    errors.append(f"{name}: no {field} request")
                if field not in limits:
                    errors.append(f"{name}: no {field} limit")
            if gpu:
                requested = requests.get("nvidia.com/gpu")
                limited = limits.get("nvidia.com/gpu")
                if requested is None or limited is None:
                    errors.append(f"{name}: GPU pod without nvidia.com/gpu request/limit")
                elif str(requested) != str(limited):
                    errors.append(f"{name}: GPU request {requested} != limit {limited}")
    return errors


def check_probes(docs: list[dict]) -> list[str]:
    errors = []
    for doc in workloads(docs):
        if doc.get("kind") not in LONG_RUNNING_KINDS:
            continue
        for container in pod_spec(doc).get("containers") or []:
            probe = container.get("readinessProbe") or {}
            if not (probe.get("httpGet") or {}).get("path"):
                errors.append(
                    f"{label(doc)} container {container.get('name')}: no HTTP readiness probe"
                )
    return errors


def check_mounts(docs: list[dict]) -> list[str]:
    """Nested volume mounts fail on a real kubelet, not in review.

    A subPath mount inside a read-only ConfigMap mount cannot be created
    ("not a directory"), so every mount gets its own directory.
    """
    errors = []
    for doc in workloads(docs):
        for container in containers(pod_spec(doc)):
            paths = [
                str(mount.get("mountPath", "")).rstrip("/")
                for mount in container.get("volumeMounts") or []
            ]
            for outer in paths:
                for inner in paths:
                    if inner != outer and inner.startswith(f"{outer}/"):
                        errors.append(
                            f"{label(doc)} container {container.get('name')}: "
                            f"{inner} is mounted inside {outer}"
                        )
    return errors


def tolerates(spec: dict, key: str) -> bool:
    return any(term.get("key") == key for term in spec.get("tolerations") or [])


def check_placement(docs: list[dict]) -> list[str]:
    """The GPU tier must be pinned to GPU nodes; everything else must prefer the
    CPU pool. Affinity is preferred rather than required so a single-node kind
    cluster without the pool label still schedules; the taints do the enforcing.
    """
    errors = []
    vllm = find(docs, "Deployment", "vllm-gpu")
    if vllm is None:
        errors.append("missing Deployment/vllm-gpu")
    else:
        spec = pod_spec(vllm)
        if (spec.get("nodeSelector") or {}).get(GPU_NODE_LABEL) != "true":
            errors.append(f"Deployment/vllm-gpu does not select GPU nodes ({GPU_NODE_LABEL}=true)")
        if not tolerates(spec, GPU_TAINT):
            errors.append(f"Deployment/vllm-gpu does not tolerate the {GPU_TAINT} taint")

    for name in CPU_DEPLOYMENTS:
        deployment = find(docs, "Deployment", name)
        if deployment is None:
            continue
        spec = pod_spec(deployment)
        affinity = spec.get("affinity") or {}
        terms = (
            (affinity.get("nodeAffinity") or {}).get(
                "preferredDuringSchedulingIgnoredDuringExecution"
            )
            or []
        )
        prefers_cpu = any(
            expression.get("key") == CPU_POOL_LABEL
            and expression.get("operator") == "In"
            and CPU_POOL_VALUE in (expression.get("values") or [])
            for term in terms
            for expression in (term.get("preference") or {}).get("matchExpressions") or []
        )
        if not prefers_cpu:
            errors.append(f"Deployment/{name} does not prefer {CPU_POOL_LABEL}={CPU_POOL_VALUE}")
        if not tolerates(spec, CPU_POOL_LABEL):
            errors.append(f"Deployment/{name} does not tolerate the {CPU_POOL_LABEL} taint")

    return errors


def check_services(docs: list[dict]) -> list[str]:
    errors = []
    names = set()
    ports: dict[str, set[int]] = {}
    for doc in docs:
        if doc.get("kind") != "Service":
            continue
        name = doc.get("metadata", {}).get("name")
        names.add(name)
        ports[name] = {
            int(p["port"]) for p in doc.get("spec", {}).get("ports") or [] if "port" in p
        }
    for name in sorted(REQUIRED_SERVICES - names):
        errors.append(f"missing Service/{name}")
    deployments = {d.get("metadata", {}).get("name") for d in workloads(docs) if d.get("kind") == "Deployment"}
    for name in sorted(REQUIRED_DEPLOYMENTS - deployments):
        errors.append(f"missing Deployment/{name}")
    return errors


def check_policy_binding(docs: list[dict], policy_path: Path) -> list[str]:
    errors = []
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    ports: dict[str, set[int]] = {}
    for doc in docs:
        if doc.get("kind") != "Service":
            continue
        name = doc.get("metadata", {}).get("name")
        ports[name] = {
            int(port["port"]) for port in doc.get("spec", {}).get("ports") or [] if "port" in port
        }
    for alias, body in (policy.get("aliases") or {}).items():
        for candidate in body.get("candidates") or []:
            backend = candidate.get("backend", "")
            parsed = urlsplit(backend)
            host, port = parsed.hostname, parsed.port
            if host not in ports:
                errors.append(f"alias {alias}: backend {backend} has no Service/{host}")
            elif port not in ports[host]:
                errors.append(f"alias {alias}: Service/{host} does not expose port {port}")

    configmap = next(
        (
            doc
            for doc in docs
            if doc.get("kind") == "ConfigMap"
            and str(doc.get("metadata", {}).get("name", "")).startswith("slm-policy")
        ),
        None,
    )
    if configmap is None:
        errors.append("no generated slm-policy ConfigMap in the rendered set")
    else:
        rendered = (configmap.get("data") or {}).get("policy.yaml")
        expected = policy_path.read_text(encoding="utf-8")
        if rendered is None:
            errors.append("slm-policy ConfigMap has no policy.yaml key")
        elif rendered.replace("\r\n", "\n").strip() != expected.replace("\r\n", "\n").strip():
            errors.append(
                "slm-policy ConfigMap does not match config/policy.yaml "
                "(rendered stale or edited out of band)"
            )
    return errors


def check_gateway_state(docs: list[dict]) -> list[str]:
    errors = []
    gateway = find(docs, "Deployment", "gateway")
    if gateway is None:
        return ["missing Deployment/gateway"]
    replicas = gateway.get("spec", {}).get("replicas", 1)
    if replicas != 1:
        errors.append(f"Deployment/gateway runs {replicas} replicas; quota/breaker/audit state is in-process")
    if (gateway.get("spec", {}).get("strategy") or {}).get("type") != "Recreate":
        errors.append("Deployment/gateway must use the Recreate strategy")
    mounts = (pod_spec(gateway).get("volumes") or [])
    if not any("configMap" in volume and str(volume["configMap"].get("name", "")).startswith("slm-policy") for volume in mounts):
        errors.append("Deployment/gateway does not mount the generated policy ConfigMap")
    if not any("secret" in volume and volume["secret"].get("secretName") == "slm-tenants" for volume in mounts):
        errors.append("Deployment/gateway does not mount the slm-tenants Secret")
    if (pod_spec(gateway).get("securityContext") or {}).get("runAsNonRoot") is not True:
        errors.append("Deployment/gateway does not run as non-root")
    return errors


def check_no_secrets(docs: list[dict]) -> list[str]:
    return [
        f"{label(doc)} is Secret material and must not be committed"
        for doc in docs
        if doc.get("kind") == "Secret"
    ]


def check_namespaces(docs: list[dict]) -> list[str]:
    errors = []
    for doc in docs:
        if doc.get("kind") in CLUSTER_SCOPED:
            continue
        namespace = (doc.get("metadata") or {}).get("namespace")
        if namespace != "slm-fabric":
            errors.append(f"{label(doc)} is in namespace {namespace!r}, expected 'slm-fabric'")
    return errors


def _env(container: dict) -> dict[str, str]:
    return {item.get("name"): item.get("value") for item in container.get("env") or []}


def check_alerts(docs: list[dict]) -> list[str]:
    """An alert nobody wrote is an outage nobody hears about.

    The five rules are the ones that separate 'a tier is slow' from 'routing has
    stopped'. A rule without a `for` window or a severity label fires either
    too late or into no route.
    """
    errors = []
    rule = find(docs, "PrometheusRule", "slm-fabric")
    if rule is None:
        return ["no PrometheusRule/slm-fabric in the rendered set"]
    rules = [
        item
        for group in (rule.get("spec") or {}).get("groups") or []
        for item in group.get("rules") or []
        if item.get("alert")
    ]
    present = {item["alert"] for item in rules}
    for name in sorted(REQUIRED_ALERTS - present):
        errors.append(f"PrometheusRule/slm-fabric is missing alert {name}")
    for item in rules:
        name = item["alert"]
        if not str(item.get("expr") or "").strip():
            errors.append(f"alert {name} has no expression")
        if not item.get("for"):
            errors.append(f"alert {name} has no for duration")
        if (item.get("labels") or {}).get("severity") not in {"warning", "critical"}:
            errors.append(f"alert {name} has no warning/critical severity")
    return errors


CAPACITY_PATH = REPO / "config/capacity.yaml"
SCALER_KINDS = {"ScaledObject", "HorizontalPodAutoscaler"}
SHARED_CLAIMS = ("slm-model-cache", "slm-adapters")


def load_capacity(path: Path = CAPACITY_PATH) -> dict:
    """The scaling bounds live in config/capacity.yaml so the deployment, the
    scaling controller and this validator cannot disagree about them."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    gpu = data.get("gpu") or {}
    try:
        bounds = (int(gpu["min_replicas"]), int(gpu["max_replicas"]))
        workload = str(gpu["workload"])
        metrics = list(gpu["metrics"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{path} does not declare GPU replica bounds and metrics") from exc
    if not workload or not metrics:
        raise RuntimeError(f"{path} does not declare a GPU workload and at least one metric")
    return {"workload": workload, "min_replicas": bounds[0], "max_replicas": bounds[1],
            "metrics": metrics}


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def check_autoscaling(docs: list[dict], capacity: dict) -> list[str]:
    """Scaling is the one place where a manifest can be valid YAML and still
    take the platform down: a scaler on the gateway replicates in-process state,
    a GPU floor of zero turns a warm tier cold, a shared claim that is not RWX
    deadlocks the second replica, and a replica without preloaded adapters
    answers alias traffic with a 404.
    """
    errors = []
    workload = capacity["workload"]
    minimum = capacity["min_replicas"]
    maximum = capacity["max_replicas"]
    if minimum < 1:
        errors.append(f"config/capacity.yaml allows {workload} to scale to zero; the GPU tier keeps a warm floor")
    if maximum < minimum:
        errors.append(f"config/capacity.yaml has max_replicas {maximum} below min_replicas {minimum}")

    scalers = [doc for doc in docs if doc.get("kind") in SCALER_KINDS]
    for scaler in scalers:
        if ((scaler.get("spec") or {}).get("scaleTargetRef") or {}).get("name") == "gateway":
            errors.append(
                f"{label(scaler)} scales the gateway; its quota, breaker and audit state "
                "is in-process and cannot be replicated"
            )

    scaled_object = find(docs, "ScaledObject", workload)
    if scaled_object is None:
        return errors

    spec = scaled_object.get("spec") or {}
    target = spec.get("scaleTargetRef") or {}
    deployment = find(docs, "Deployment", workload)
    if target.get("kind") != "Deployment" or target.get("name") != workload:
        errors.append(
            f"ScaledObject/{workload} targets {target.get('kind')}/{target.get('name')}, "
            f"expected Deployment/{workload}"
        )
    if spec.get("minReplicaCount") != minimum:
        errors.append(
            f"ScaledObject/{workload} minReplicaCount is {spec.get('minReplicaCount')}, "
            f"config/capacity.yaml says {minimum}"
        )
    if spec.get("maxReplicaCount") != maximum:
        errors.append(
            f"ScaledObject/{workload} maxReplicaCount is {spec.get('maxReplicaCount')}, "
            f"config/capacity.yaml says {maximum}"
        )
    if deployment is not None and "replicas" in (deployment.get("spec") or {}):
        errors.append(
            f"Deployment/{workload} pins spec.replicas while a ScaledObject owns it; "
            "Argo selfHeal would undo every scale event"
        )

    triggers = {
        str((trigger.get("metadata") or {}).get("metricName")): trigger
        for trigger in spec.get("triggers") or []
    }
    for metric in capacity["metrics"]:
        name = str(metric.get("name"))
        trigger = triggers.get(str(metric.get("keda_metric")))
        if trigger is None:
            errors.append(f"ScaledObject/{workload} has no {name} trigger")
            continue
        if trigger.get("type") != "prometheus":
            errors.append(f"ScaledObject/{workload} trigger {name} is {trigger.get('type')!r}, expected prometheus")
        metadata = trigger.get("metadata") or {}
        if metadata.get("metricName") != metric.get("keda_metric"):
            errors.append(
                f"ScaledObject/{workload} trigger {name} publishes {metadata.get('metricName')!r}, "
                f"expected {metric.get('keda_metric')!r}"
            )
        if _norm(metadata.get("query")) != _norm(metric.get("query")):
            errors.append(f"ScaledObject/{workload} trigger {name} query differs from config/capacity.yaml")
        if str(metadata.get("threshold")) != str(metric.get("threshold")):
            errors.append(
                f"ScaledObject/{workload} trigger {name} threshold is {metadata.get('threshold')}, "
                f"config/capacity.yaml says {metric.get('threshold')}"
            )
        if str(metadata.get("serverAddress") or "").startswith("https://") and not trigger.get(
            "authenticationRef"
        ):
            errors.append(
                f"ScaledObject/{workload} trigger {name} queries HTTPS without authenticationRef"
            )

    if maximum > 1:
        for claim in SHARED_CLAIMS:
            pvc = find(docs, "PersistentVolumeClaim", claim)
            if pvc is None:
                continue
            modes = (pvc.get("spec") or {}).get("accessModes") or []
            if "ReadWriteMany" not in modes:
                errors.append(
                    f"PersistentVolumeClaim/{claim} is {modes}; {maximum} {workload} replicas "
                    "need ReadWriteMany to land on more than one node"
                )

    adapter_set = find(docs, "ConfigMap", "slm-adapter-set")
    if adapter_set is not None and deployment is not None:
        declared = [
            line.strip()
            for line in str((adapter_set.get("data") or {}).get("adapters.txt", "")).splitlines()
            if line.strip()
        ]
        args = list(((pod_spec(deployment).get("containers") or [{}])[0].get("args") or []))
        if "--lora-modules" not in args:
            errors.append(
                f"Deployment/{workload} starts no adapters; a replica added by the scaler "
                "would refuse the fine-tuned aliases"
            )
        else:
            preloaded = []
            for item in args[args.index("--lora-modules") + 1:]:
                if item.startswith("--"):
                    break
                preloaded.append(item)
            missing = [name for name in declared if f"{name}=/adapters/{name}" not in preloaded]
            if missing:
                errors.append(
                    f"Deployment/{workload} preloads {', '.join(missing)} not at "
                    f"/adapters/<name>; slm-adapter-set declares {', '.join(declared)}"
                )
    return errors


def check_overlay(overlay: str, docs: list[dict]) -> list[str]:
    errors = []
    route = find(docs, "Route", "slm-gateway")
    ingress = find(docs, "Ingress", "slm-gateway")
    servicemonitor = find(docs, "ServiceMonitor")
    vllm = find(docs, "Deployment", "vllm-gpu")

    if overlay in {"openshift", "airgap"}:
        if route is None:
            errors.append("OpenShift overlay has no Route/slm-gateway")
        else:
            if (route.get("spec", {}).get("tls") or {}).get("termination") != "edge":
                errors.append("Route/slm-gateway does not terminate TLS at the edge")
            if (route.get("spec", {}).get("to") or {}).get("name") != "gateway":
                errors.append("Route/slm-gateway does not target the gateway Service")
        if servicemonitor is None:
            errors.append("OpenShift overlay has no ServiceMonitor")
        if find(docs, "ScaledObject") is None:
            errors.append(f"{overlay} overlay has no ScaledObject; the GPU tier would stay at one replica")
        errors.extend(check_alerts(docs))
        if not any(d.get("kind") == "NetworkPolicy" and d.get("metadata", {}).get("name") == "allow-monitoring-scrape" for d in docs):
            errors.append("OpenShift overlay does not allow Prometheus to scrape the backends")

    if overlay == "vanilla":
        if ingress is None:
            errors.append("vanilla overlay has no Ingress/slm-gateway")
        else:
            backends = [
                rule.get("http", {}).get("paths", [{}])[0].get("backend", {}).get("service", {})
                for rule in ingress.get("spec", {}).get("rules") or []
            ]
            if not any(b.get("name") == "gateway" and b.get("port", {}).get("number") == 8000 for b in backends):
                errors.append("Ingress/slm-gateway does not target gateway:8000")
        if route is not None:
            errors.append("vanilla overlay must not contain an OpenShift Route")
        if any(doc.get("kind") in SCALER_KINDS for doc in docs):
            errors.append("vanilla overlay must not contain a scaler; kind has no KEDA or metrics API")
        for doc in workloads(docs):
            sc = pod_spec(doc).get("securityContext") or {}
            if sc.get("runAsUser") != 1001:
                errors.append(f"{label(doc)}: vanilla overlay does not pin runAsUser 1001")

    if overlay == "airgap":
        if any(
            doc.get("kind") == "NetworkPolicy"
            and doc.get("metadata", {}).get("name") == "allow-vllm-model-download"
            for doc in docs
        ):
            errors.append("airgap overlay still allows vLLM to reach the internet")
        if vllm is None:
            errors.append("airgap overlay has no Deployment/vllm-gpu")
        else:
            env = _env((pod_spec(vllm).get("containers") or [{}])[0])
            if env.get("HF_HUB_OFFLINE") != "1":
                errors.append("airgap overlay does not set HF_HUB_OFFLINE=1 on vLLM")

    if overlay in {"openshift", "vanilla"} and vllm is not None:
        if not any(
            doc.get("kind") == "NetworkPolicy"
            and doc.get("metadata", {}).get("name") == "allow-vllm-model-download"
            for doc in docs
        ):
            errors.append(f"{overlay} overlay cannot download the model at first start")
    return errors


def validate(docs: list[dict], overlay: str, policy_path: Path, capacity: dict | None = None) -> list[str]:
    return [
        *check_images(docs),
        *check_model_pin(docs),
        *check_security(docs),
        *check_resources(docs),
        *check_probes(docs),
        *check_mounts(docs),
        *check_placement(docs),
        *check_services(docs),
        *check_policy_binding(docs, policy_path),
        *check_gateway_state(docs),
        *check_no_secrets(docs),
        *check_namespaces(docs),
        *check_autoscaling(docs, capacity if capacity is not None else load_capacity()),
        *check_overlay(overlay, docs),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overlay", action="append", choices=OVERLAYS, help="default: all")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--policy", type=Path, default=REPO / "config/policy.yaml")
    parser.add_argument("--capacity", type=Path, default=CAPACITY_PATH)
    args = parser.parse_args(argv)

    overlays = args.overlay or list(OVERLAYS)
    try:
        capacity = load_capacity(args.capacity)
    except (RuntimeError, OSError) as exc:
        print(f"FAIL capacity: {exc}")
        return 1
    failed = False
    for overlay in overlays:
        try:
            docs = render_overlay(overlay, args.kubectl)
        except (RuntimeError, FileNotFoundError) as exc:
            print(f"FAIL {overlay}: {exc}")
            failed = True
            continue
        errors = validate(docs, overlay, args.policy, capacity)
        if errors:
            failed = True
            print(f"FAIL {overlay}: {len(errors)} problem(s) across {len(docs)} objects")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"OK   {overlay}: {len(docs)} objects")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
