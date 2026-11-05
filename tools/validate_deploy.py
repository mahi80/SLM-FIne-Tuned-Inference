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
  * every policy candidate names a pool that config/capacity.yaml declares,
    and its compute, deployment and backend match that pool, so the router
    cannot route to capacity nobody has described;
  * the GPU scaler matches config/capacity.yaml, keeps a warm floor, shares an
    RWX claim and preloads every declared adapter — and never scales the
    gateway, whose quota, breaker and audit state is in-process.
  * the gateway is a single replica, because its quota, breaker and audit state
    is in-process.
  * an external (cloud) candidate can only be routed to when the gateway mounts
    its optional key Secret, points SLM_BACKEND_KEYS at it, and an opt-in egress
    rule opens TCP 443 to a public provider range — never to the cluster or the
    whole internet.
"""

from __future__ import annotations

import argparse
import ipaddress
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
            if candidate.get("access") is not None:
                # An external backend has no Service by design; admission and
                # the egress policy are what gate it instead.
                continue
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
COMPUTE_TYPES = {"gpu", "cpu"}
DEPLOYMENT_TYPES = {"onprem", "cloud"}
AUTOSCALERS = {"external", "none"}
BACKEND_KEYS_SECRET = "slm-backend-keys"
CLOUD_EGRESS_POLICY = "allow-gateway-cloud-egress"
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
)


def load_capacity(path: Path = CAPACITY_PATH) -> dict:
    """The pool registry lives in config/capacity.yaml so the router's policy,
    the scaling controllers and this validator cannot disagree about where work
    may run or how far each pool may grow."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw_pools = data.get("pools")
    if not isinstance(raw_pools, list) or not raw_pools:
        raise RuntimeError(f"{path} declares no pools")
    pools: dict[str, dict] = {}
    for index, raw in enumerate(raw_pools):
        where = f"{path}: pools[{index}]"
        if not isinstance(raw, dict):
            raise RuntimeError(f"{where} must be a mapping")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise RuntimeError(f"{where} has no name")
        if name in pools:
            raise RuntimeError(f"{path}: duplicate pool {name!r}")
        compute = str(raw.get("compute") or "")
        if compute not in COMPUTE_TYPES:
            raise RuntimeError(f"{where}: compute must be one of {sorted(COMPUTE_TYPES)}")
        deployment = str(raw.get("deployment") or "")
        if deployment not in DEPLOYMENT_TYPES:
            raise RuntimeError(f"{where}: deployment must be one of {sorted(DEPLOYMENT_TYPES)}")
        external = bool(raw.get("external", False))
        replicas = raw.get("replicas") or {}
        if external and not replicas:
            minimum, maximum = 0, 0
        else:
            try:
                minimum, maximum = int(replicas["min"]), int(replicas["max"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"{where} does not declare replicas.min and replicas.max") from exc
        if minimum < 0 or maximum < minimum:
            raise RuntimeError(f"{where}: replicas must satisfy 0 <= min <= max")
        endpoint = str(raw.get("endpoint") or "").rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            raise RuntimeError(f"{where}: endpoint must be an http(s) URL")
        workload = str(raw.get("workload") or "").strip()
        metrics = raw.get("metrics") or []
        if not isinstance(metrics, list):
            raise RuntimeError(f"{where}: metrics must be a list")
        node_pool = raw.get("node_pool") or {}
        if external:
            if not endpoint.startswith("https://"):
                raise RuntimeError(f"{where}: an external pool endpoint must be https")
            if workload:
                raise RuntimeError(
                    f"{where}: an external pool has no local Deployment; drop 'workload'"
                )
            if node_pool:
                raise RuntimeError(
                    f"{where}: an external pool's nodes belong to the provider; drop 'node_pool'"
                )
        else:
            if not workload:
                raise RuntimeError(f"{where} has no workload")
            if not isinstance(node_pool, dict) or not str(node_pool.get("label") or "").strip():
                raise RuntimeError(f"{where} has no node_pool.label")
            if str(node_pool.get("autoscaler") or "") not in AUTOSCALERS:
                raise RuntimeError(
                    f"{where}: node_pool.autoscaler must be one of {sorted(AUTOSCALERS)}"
                )
        pools[name] = {
            "name": name,
            "compute": compute,
            "deployment": deployment,
            "external": external,
            "provider": str(raw.get("provider") or ""),
            "modelled": bool(raw.get("modelled", False)),
            "endpoint": endpoint,
            "workload": workload,
            "min_replicas": minimum,
            "max_replicas": maximum,
            "metrics": metrics,
            "prerequisites": raw.get("prerequisites") or {},
            "node_pool": node_pool,
        }
    return {"version": data.get("version"), "pools": pools}


def check_pools(capacity: dict, policy_path: Path) -> list[str]:
    """A candidate is only routable if its pool is declared and the two agree.

    The candidate states compute and deployment because admission reasons about
    them; the pool states where the work actually lands. If those two drift, a
    policy review approves a route nobody deployed — or forbids one that exists.
    """
    errors = []
    policy = yaml.safe_load(Path(policy_path).read_text(encoding="utf-8")) or {}
    pools = capacity["pools"]
    for alias, body in (policy.get("aliases") or {}).items():
        for raw in (body or {}).get("candidates") or []:
            if not isinstance(raw, dict):
                continue
            where = f"alias {alias} candidate {raw.get('name')}"
            name = str(raw.get("pool") or "")
            pool = pools.get(name)
            if pool is None:
                errors.append(
                    f"{where} names pool {name!r}, which config/capacity.yaml does not declare"
                )
                continue
            if str(raw.get("compute")) != pool["compute"]:
                errors.append(
                    f"{where} declares compute {raw.get('compute')!r}, "
                    f"pool {name} is {pool['compute']!r}"
                )
            if str(raw.get("deployment")) != pool["deployment"]:
                errors.append(
                    f"{where} declares deployment {raw.get('deployment')!r}, "
                    f"pool {name} is {pool['deployment']!r}"
                )
            backend = str(raw.get("backend") or "").rstrip("/")
            if backend != pool["endpoint"]:
                errors.append(
                    f"{where} points at {backend!r}, pool {name} endpoint is {pool['endpoint']!r}"
                )
            has_access = raw.get("access") is not None
            if has_access != pool["external"]:
                if pool["external"]:
                    errors.append(
                        f"{where} has no access block, but pool {name} is external; "
                        "an external backend must reference a key from the mounted Secret"
                    )
                else:
                    errors.append(
                        f"{where} declares access, but pool {name} is in-cluster; "
                        "mark the pool external before sending credentials"
                    )
    return errors


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def _check_scaled_object(scaled_object: dict, pool: dict, docs: list[dict]) -> list[str]:
    errors = []
    workload = pool["workload"]
    name = f"ScaledObject/{workload}"
    spec = scaled_object.get("spec") or {}
    target = spec.get("scaleTargetRef") or {}
    deployment = find(docs, "Deployment", workload)
    if target.get("kind") != "Deployment" or target.get("name") != workload:
        errors.append(
            f"{name} targets {target.get('kind')}/{target.get('name')}, "
            f"expected Deployment/{workload}"
        )
    if spec.get("minReplicaCount") != pool["min_replicas"]:
        errors.append(
            f"{name} minReplicaCount is {spec.get('minReplicaCount')}, "
            f"pool {pool['name']} says {pool['min_replicas']}"
        )
    if spec.get("maxReplicaCount") != pool["max_replicas"]:
        errors.append(
            f"{name} maxReplicaCount is {spec.get('maxReplicaCount')}, "
            f"pool {pool['name']} says {pool['max_replicas']}"
        )
    if deployment is not None and "replicas" in (deployment.get("spec") or {}):
        errors.append(
            f"Deployment/{workload} pins spec.replicas while a ScaledObject owns it; "
            "Argo selfHeal would undo every scale event"
        )
    if not pool["metrics"]:
        errors.append(f"{name} scales a pool that declares no metric in config/capacity.yaml")

    triggers = {
        str((trigger.get("metadata") or {}).get("metricName")): trigger
        for trigger in spec.get("triggers") or []
    }
    for metric in pool["metrics"]:
        metric_name = str(metric.get("name"))
        trigger = triggers.get(str(metric.get("keda_metric")))
        if trigger is None:
            errors.append(f"{name} has no {metric_name} trigger")
            continue
        if trigger.get("type") != "prometheus":
            errors.append(
                f"{name} trigger {metric_name} is {trigger.get('type')!r}, expected prometheus"
            )
        metadata = trigger.get("metadata") or {}
        if metadata.get("metricName") != metric.get("keda_metric"):
            errors.append(
                f"{name} trigger {metric_name} publishes {metadata.get('metricName')!r}, "
                f"expected {metric.get('keda_metric')!r}"
            )
        if _norm(metadata.get("query")) != _norm(metric.get("query")):
            errors.append(f"{name} trigger {metric_name} query differs from config/capacity.yaml")
        if str(metadata.get("threshold")) != str(metric.get("threshold")):
            errors.append(
                f"{name} trigger {metric_name} threshold is {metadata.get('threshold')}, "
                f"config/capacity.yaml says {metric.get('threshold')}"
            )
        if str(metadata.get("serverAddress") or "").startswith("https://") and not trigger.get(
            "authenticationRef"
        ):
            errors.append(
                f"{name} trigger {metric_name} queries HTTPS without authenticationRef"
            )

    if deployment is not None and pool["max_replicas"] > 1:
        for volume in pod_spec(deployment).get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            if not claim:
                continue
            pvc = find(docs, "PersistentVolumeClaim", claim)
            if pvc is None:
                continue
            modes = (pvc.get("spec") or {}).get("accessModes") or []
            if "ReadWriteMany" not in modes:
                errors.append(
                    f"PersistentVolumeClaim/{claim} is {modes}; {pool['max_replicas']} "
                    f"{workload} replicas need ReadWriteMany to land on more than one node"
                )

    adapter_set = find(docs, "ConfigMap", "slm-adapter-set")
    if deployment is not None and pool["prerequisites"].get("preload_adapters") and adapter_set:
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
            missing = [item for item in declared if f"{item}=/adapters/{item}" not in preloaded]
            if missing:
                errors.append(
                    f"Deployment/{workload} preloads {', '.join(missing)} not at "
                    f"/adapters/<name>; slm-adapter-set declares {', '.join(declared)}"
                )
    return errors


def check_autoscaling(docs: list[dict], capacity: dict) -> list[str]:
    """Scaling is the one place where a manifest can be valid YAML and still
    take the platform down: a scaler on the gateway replicates in-process state,
    a GPU floor of zero turns a warm tier cold, a shared claim that is not RWX
    deadlocks the second replica, and a replica without preloaded adapters
    answers alias traffic with a 404.
    """
    errors = []
    pools = capacity["pools"]
    for pool in pools.values():
        if pool["compute"] == "gpu" and pool["deployment"] == "onprem" and pool["min_replicas"] < 1:
            errors.append(
                f"pool {pool['name']} allows GPU replicas to reach zero; "
                "the on-prem tier keeps a warm floor"
            )
        if pool["max_replicas"] > 1 and not pool["metrics"]:
            errors.append(
                f"pool {pool['name']} allows {pool['max_replicas']} replicas "
                "but declares no scaling metric"
            )

    by_workload = {pool["workload"]: pool for pool in pools.values()}
    for scaler in (doc for doc in docs if doc.get("kind") in SCALER_KINDS):
        target = (scaler.get("spec") or {}).get("scaleTargetRef") or {}
        if target.get("name") == "gateway":
            errors.append(
                f"{label(scaler)} scales the gateway; its quota, breaker and audit state "
                "is in-process and cannot be replicated"
            )
            continue
        if scaler.get("kind") != "ScaledObject":
            errors.append(
                f"{label(scaler)} is a HorizontalPodAutoscaler; tiers scale through a "
                "ScaledObject so the policy stays in config/capacity.yaml"
            )
            continue
        pool = by_workload.get(str(target.get("name")))
        if pool is None:
            errors.append(
                f"{label(scaler)} targets {target.get('name')!r}, "
                "which is not the workload of a declared pool"
            )
            continue
        errors.extend(_check_scaled_object(scaler, pool, docs))
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
        if find(docs, "NetworkPolicy", CLOUD_EGRESS_POLICY) is not None:
            errors.append("airgap overlay must not open egress to a cloud provider")
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


def external_candidates(policy_path: Path) -> list[tuple[str, str]]:
    policy = yaml.safe_load(Path(policy_path).read_text(encoding="utf-8")) or {}
    found: list[tuple[str, str]] = []
    for alias, body in (policy.get("aliases") or {}).items():
        for candidate in (body or {}).get("candidates") or []:
            if isinstance(candidate, dict) and candidate.get("access") is not None:
                found.append((str(alias), str(candidate.get("name"))))
    return found


def check_cloud_egress(network_policy: dict) -> list[str]:
    """Egress to a cloud provider is the one rule that can undo default-deny.

    A rule that names no peer, or a CIDR that covers the cluster or the whole
    internet, turns the gateway into an arbitrary outbound proxy. Only the
    provider's public range on 443 may be opened.
    """
    name = f"NetworkPolicy/{CLOUD_EGRESS_POLICY}"
    errors: list[str] = []
    spec = network_policy.get("spec") or {}
    selector = (spec.get("podSelector") or {}).get("matchLabels") or {}
    if selector.get(VLLM_LABEL) != "gateway":
        errors.append(f"{name} does not select the gateway pods")
    if "Egress" not in (spec.get("policyTypes") or []):
        errors.append(f"{name} does not declare the Egress policy type")
    rules = spec.get("egress") or []
    if not rules:
        errors.append(f"{name} has no egress rule")
    for index, rule in enumerate(rules):
        where = f"{name} egress[{index}]"
        peers = rule.get("to") or []
        if not peers:
            errors.append(f"{where}: allows every peer")
        for peer in peers:
            block = peer.get("ipBlock")
            if block is None:
                errors.append(
                    f"{where}: peer is not an ipBlock; in-cluster backends are already allowed"
                )
                continue
            cidr = str(block.get("cidr") or "")
            try:
                network = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                errors.append(f"{where}: {cidr!r} is not a CIDR")
                continue
            if network.prefixlen == 0:
                errors.append(f"{where}: {cidr} allows the whole internet")
            elif any(network.overlaps(private) for private in PRIVATE_NETWORKS):
                errors.append(
                    f"{where}: {cidr} overlaps private, loopback or link-local space; "
                    "an external backend must be reached at a public address"
                )
        ports = rule.get("ports") or []
        if not ports:
            errors.append(f"{where}: opens every port")
        for port in ports:
            if str(port.get("protocol") or "TCP") != "TCP" or port.get("port") != 443:
                errors.append(f"{where}: only TCP 443 may leave the cluster")
    return errors


def check_external_backends(docs: list[dict], policy_path: Path) -> list[str]:
    """An external candidate is inert until three things line up: a key the
    gateway can read, a CA it can trust, and an egress rule that names the
    provider. Each missing piece fails closed at runtime, so catch it here."""
    errors: list[str] = []
    candidates = external_candidates(policy_path)
    gateway = find(docs, "Deployment", "gateway")
    if gateway is None:
        return ["missing Deployment/gateway"]
    spec = pod_spec(gateway)
    containers_ = spec.get("containers") or []
    env = _env(containers_[0]) if containers_ else {}
    keys_volume = next(
        (
            volume
            for volume in spec.get("volumes") or []
            if (volume.get("secret") or {}).get("secretName") == BACKEND_KEYS_SECRET
        ),
        None,
    )
    keys_mount = next(
        (
            mount
            for container in containers_
            for mount in container.get("volumeMounts") or []
            if keys_volume is not None and mount.get("name") == keys_volume.get("name")
        ),
        None,
    )
    egress = find(docs, "NetworkPolicy", CLOUD_EGRESS_POLICY)

    if not candidates:
        if egress is not None:
            errors.append(
                f"{label(egress)} opens egress but no policy candidate is external"
            )
        return errors

    if keys_mount is None:
        errors.append(
            f"Deployment/gateway does not mount Secret/{BACKEND_KEYS_SECRET}; "
            "external candidates would be vetoed as backend-unconfigured"
        )
    else:
        if (keys_volume.get("secret") or {}).get("optional") is not True:
            errors.append(
                f"Secret/{BACKEND_KEYS_SECRET} mount is not optional; an in-cluster-only "
                "install would fail to start"
            )
        keys_env = str(env.get("SLM_BACKEND_KEYS") or "")
        expected = str(keys_mount.get("mountPath", "")).rstrip("/") + "/keys.yaml"
        if keys_env != expected:
            errors.append(
                f"Deployment/gateway SLM_BACKEND_KEYS is {keys_env!r}, "
                f"expected {expected!r} from the {BACKEND_KEYS_SECRET} mount"
            )
    ca_env = str(env.get("SLM_BACKEND_CA") or "")
    if ca_env:
        mounts = [
            str(mount.get("mountPath", "")).rstrip("/")
            for container in containers_
            for mount in container.get("volumeMounts") or []
        ]
        if not any(ca_env.startswith(f"{path}/") for path in mounts):
            errors.append(
                f"Deployment/gateway SLM_BACKEND_CA is {ca_env!r}, which is not inside a mounted volume"
            )
    if egress is None:
        errors.append(
            f"policy has external candidates {sorted({name for _, name in candidates})} "
            f"but the rendered set has no NetworkPolicy/{CLOUD_EGRESS_POLICY}; the "
            "default-deny would blackhole them"
        )
    else:
        errors.extend(check_cloud_egress(egress))
    return errors


def validate(docs: list[dict], overlay: str, policy_path: Path, capacity: dict | None = None) -> list[str]:
    capacity = capacity if capacity is not None else load_capacity()
    return [
        *check_images(docs),
        *check_model_pin(docs),
        *check_security(docs),
        *check_resources(docs),
        *check_probes(docs),
        *check_mounts(docs),
        *check_placement(docs),
        *check_services(docs),
        *check_pools(capacity, policy_path),
        *check_policy_binding(docs, policy_path),
        *check_gateway_state(docs),
        *check_external_backends(docs, policy_path),
        *check_no_secrets(docs),
        *check_namespaces(docs),
        *check_autoscaling(docs, capacity),
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
