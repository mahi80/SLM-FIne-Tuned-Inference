#!/usr/bin/env python3
"""Health of the platform as deployed in a Kubernetes cluster.

Reads the cluster and the gateway; changes nothing and never prints tenant keys.

    python scripts/cluster_health.py [--namespace slm-fabric] [--allow-no-gpu]
                                     [--gateway-url URL --api-key KEY] [--json]

Checks, in the order a failure would hurt:

  * the API server answers and every node is Ready;
  * at least one node advertises `nvidia.com/gpu`, and a labelled CPU pool exists;
  * every platform Deployment has its replicas available;
  * pods are Running and not crash-looping, and restart loops are reported;
  * vLLM is scheduled on a GPU node and CPU workloads are not;
  * every Service the policy routes to has ready endpoints;
  * the gateway `/health` body agrees with the policy ConfigMap on the version;
  * optional: `/v1/policy` and `/v1/audit/verify` with an admin key.

The gateway is deliberately one replica, so its own readiness does not mean the
GPU is up — a stopped GPU leaves the gateway healthy and the breaker doing its
job. That is why this checks endpoints and breaker state, not just the probe.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

PLATFORM_DEPLOYMENTS = ("gateway", "vllm-gpu", "tiersim-cpu", "tiersim-cloud")
PLATFORM_SERVICES = PLATFORM_DEPLOYMENTS
GPU_RESOURCE = "nvidia.com/gpu"
GPU_NODE_LABEL = "nvidia.com/gpu.present"
CPU_POOL_LABEL = "slm.fabric/pool"
CPU_POOL_VALUE = "cpu"
CRASH_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "RunContainerError",
}
MAX_RESTARTS = 5
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
VERSION_LINE = re.compile(r"(?m)^version:\s*[\"']?([^\"'\s#]+)")


class ClusterUnreachable(RuntimeError):
    pass


def run_kubectl(args: list[str], *, kubectl: str = "kubectl", timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            [kubectl, *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClusterUnreachable(f"{kubectl} {' '.join(args)}: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise ClusterUnreachable(
            f"{kubectl} {' '.join(args)} failed: {detail[0] if detail else 'no output'}"
        )
    return proc


def fetch_json(args: list[str], *, kubectl: str = "kubectl", timeout: int = 30) -> dict:
    proc = run_kubectl([*args, "-o", "json"], kubectl=kubectl, timeout=timeout)
    return json.loads(proc.stdout)


def fetch_text(path: str, *, kubectl: str = "kubectl", timeout: int = 30) -> str:
    return run_kubectl(["get", "--raw", path], kubectl=kubectl, timeout=timeout).stdout


def nodes_summary(payload: dict) -> list[dict]:
    nodes = []
    for item in payload.get("items") or []:
        labels = (item.get("metadata") or {}).get("labels") or {}
        status = item.get("status") or {}
        conditions = {c.get("type"): c.get("status") for c in status.get("conditions") or []}
        allocatable = status.get("allocatable") or {}
        nodes.append(
            {
                "name": (item.get("metadata") or {}).get("name", "?"),
                "ready": conditions.get("Ready") == "True",
                "gpu": int(allocatable.get(GPU_RESOURCE) or 0),
                "cpu_pool": labels.get(CPU_POOL_LABEL) == CPU_POOL_VALUE,
            }
        )
    return nodes


def deployments_summary(payload: dict) -> dict[str, dict]:
    out = {}
    for item in payload.get("items") or []:
        name = (item.get("metadata") or {}).get("name")
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        desired = spec.get("replicas")
        out[name] = {
            "desired": 1 if desired is None else int(desired),
            "available": int(status.get("availableReplicas") or 0),
        }
    return out


def pods_summary(payload: dict) -> list[dict]:
    out = []
    for item in payload.get("items") or []:
        meta = item.get("metadata") or {}
        labels = meta.get("labels") or {}
        status = item.get("status") or {}
        statuses = status.get("containerStatuses") or []
        waiting = [
            (cs.get("state") or {}).get("waiting", {}).get("reason")
            for cs in statuses
            if (cs.get("state") or {}).get("waiting")
        ]
        out.append(
            {
                "name": meta.get("name", "?"),
                "deployment": labels.get("app.kubernetes.io/name"),
                "node": (item.get("spec") or {}).get("nodeName"),
                "phase": status.get("phase"),
                "ready": all(cs.get("ready") for cs in statuses) if statuses else False,
                "waiting": [reason for reason in waiting if reason],
                "restarts": sum(int(cs.get("restartCount") or 0) for cs in statuses),
            }
        )
    return out


def endpoints_summary(payload: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item in payload.get("items") or []:
        name = (item.get("metadata") or {}).get("name")
        addresses = [
            address.get("ip") or address.get("hostname")
            for subset in item.get("subsets") or []
            for address in subset.get("addresses") or []
            if address.get("ip") or address.get("hostname")
        ]
        out[name] = addresses
    return out


def policy_version_from_configmap(payload: dict) -> str | None:
    for item in payload.get("items") or []:
        name = str((item.get("metadata") or {}).get("name", ""))
        if not name.startswith("slm-policy"):
            continue
        body = (item.get("data") or {}).get("policy.yaml")
        if not body:
            continue
        match = VERSION_LINE.search(body)
        if match:
            return match.group(1)
    return None


def evaluate(
    nodes: list[dict],
    deployments: dict[str, dict],
    pods: list[dict],
    endpoints: dict[str, list[str]],
    health: dict | None,
    policy_version: str | None,
    *,
    allow_no_gpu: bool = False,
) -> list[dict]:
    findings: list[dict] = []

    def add(level: str, area: str, message: str) -> None:
        findings.append({"level": level, "area": area, "message": message})

    if not nodes:
        add(FAIL, "nodes", "the API server returned no nodes")
    else:
        not_ready = [node["name"] for node in nodes if not node["ready"]]
        if not_ready:
            add(FAIL, "nodes", "not Ready: " + ", ".join(not_ready))
        else:
            add(PASS, "nodes", f"{len(nodes)} node(s) Ready")

    gpu_total = sum(int(node.get("gpu") or 0) for node in nodes)
    if gpu_total < 1:
        add(
            WARN if allow_no_gpu else FAIL,
            "gpu",
            "no node advertises nvidia.com/gpu; the GPU tier cannot schedule"
            + (" (allowed by --allow-no-gpu)" if allow_no_gpu else ""),
        )
    else:
        add(
            PASS,
            "gpu",
            f"{gpu_total} GPU(s) allocatable across "
            f"{sum(1 for node in nodes if int(node.get('gpu') or 0) > 0)} node(s)",
        )

    for name in PLATFORM_DEPLOYMENTS:
        state = deployments.get(name)
        level = WARN if allow_no_gpu and name == "vllm-gpu" else FAIL
        if state is None:
            add(level, "deployments", f"Deployment/{name} not found")
        elif int(state.get("available") or 0) < int(state.get("desired") or 1):
            add(
                level,
                "deployments",
                f"Deployment/{name} has {state.get('available', 0)}/{state.get('desired', 1)} "
                "replica(s) available",
            )
        else:
            add(
                PASS,
                "deployments",
                f"Deployment/{name} {state.get('available')}/{state.get('desired')} available",
            )

    managed = [pod for pod in pods if pod.get("deployment") in PLATFORM_DEPLOYMENTS]
    for pod in managed:
        where = f"{pod['deployment']} pod {pod['name']}"
        level = WARN if allow_no_gpu and pod["deployment"] == "vllm-gpu" else FAIL
        if pod.get("waiting"):
            add(level, "pods", f"{where} is waiting: {', '.join(pod['waiting'])}")
        elif pod.get("phase") not in ("Running", "Succeeded"):
            add(level, "pods", f"{where} is {pod.get('phase')}")
        elif pod.get("ready") is False:
            add(WARN, "pods", f"{where} is Running but not ready")
        if int(pod.get("restarts") or 0) >= MAX_RESTARTS:
            add(WARN, "pods", f"{where} restarted {pod['restarts']} times")

    by_name = {node["name"]: node for node in nodes}
    vllm_pods = [
        pod for pod in managed if pod.get("deployment") == "vllm-gpu" and pod.get("node")
    ]
    if vllm_pods:
        node = by_name.get(vllm_pods[0]["node"])
        if node is not None and int(node.get("gpu") or 0) < 1:
            add(
                WARN if allow_no_gpu else FAIL,
                "placement",
                f"vllm-gpu runs on {node['name']}, which advertises no GPU",
            )
        elif node is not None:
            add(PASS, "placement", f"vllm-gpu scheduled on GPU node {node['name']}")

    cpu_pools = [node for node in nodes if node.get("cpu_pool")]
    misplaced = [
        pod
        for pod in managed
        if pod.get("deployment") != "vllm-gpu"
        and pod.get("node")
        and int((by_name.get(pod["node"]) or {}).get("gpu") or 0) > 0
    ]
    if misplaced:
        hint = (
            f"{len(cpu_pools)} CPU-pool node(s) available"
            if cpu_pools
            else f"label CPU workers {CPU_POOL_LABEL}={CPU_POOL_VALUE} to keep them off GPU nodes"
        )
        add(
            WARN,
            "placement",
            "CPU workloads on GPU nodes: "
            + ", ".join(f"{pod['deployment']}@{pod['node']}" for pod in misplaced)
            + f" ({hint})",
        )

    for name in PLATFORM_SERVICES:
        addresses = endpoints.get(name) or []
        level = WARN if allow_no_gpu and name == "vllm-gpu" else FAIL
        if addresses:
            add(PASS, "endpoints", f"Service/{name} has {len(addresses)} ready address(es)")
        else:
            add(level, "endpoints", f"Service/{name} has no ready endpoints")

    if health is None:
        add(FAIL, "gateway", "gateway /health is unreachable through the API server proxy")
    else:
        if health.get("status") != "ok":
            add(FAIL, "gateway", f"gateway /health status is {health.get('status')!r}")
        else:
            add(
                PASS,
                "gateway",
                f"gateway healthy, {health.get('aliases', '?')} alias(es), "
                f"policy {health.get('policy_version', '?')}",
            )
        aliases = health.get("aliases")
        if aliases is not None and int(aliases) < 1:
            add(FAIL, "gateway", "gateway has no aliases loaded")
        breakers = health.get("breakers") or {}
        open_count = int(breakers.get("open") or 0)
        if open_count:
            add(WARN, "gateway", f"{open_count} breaker(s) open; routing is on a standby")
        half_open = int(breakers.get("half_open") or 0)
        if half_open:
            add(WARN, "gateway", f"{half_open} breaker(s) half-open; a probe is in flight")

    served = (health or {}).get("policy_version")
    if policy_version is None:
        add(FAIL, "policy", "no slm-policy ConfigMap found in the namespace")
    elif served and served != policy_version:
        add(
            FAIL,
            "policy",
            f"gateway serves policy {served}, ConfigMap is {policy_version} (stale ConfigMap or rollout pending)",
        )
    elif served:
        add(PASS, "policy", f"gateway policy matches the ConfigMap ({policy_version})")

    return findings


def api_findings(base_url: str, api_key: str, *, timeout: int = 10) -> list[dict]:
    findings: list[dict] = []

    def get(path: str) -> dict:
        request = urllib.request.Request(
            base_url.rstrip("/") + path,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        policy = get("/v1/policy")
        findings.append(
            {
                "level": PASS,
                "area": "api",
                "message": f"/v1/policy served {len(policy.get('aliases') or {})} alias(es)",
            }
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        findings.append({"level": FAIL, "area": "api", "message": f"/v1/policy failed: {exc}"})

    try:
        audit = get("/v1/audit/verify")
        if audit.get("ok"):
            findings.append(
                {
                    "level": PASS,
                    "area": "api",
                    "message": f"/v1/audit/verify ok over {audit.get('records', '?')} record(s)",
                }
            )
        else:
            findings.append(
                {"level": FAIL, "area": "api", "message": f"/v1/audit/verify failed: {audit}"}
            )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        findings.append({"level": FAIL, "area": "api", "message": f"/v1/audit/verify failed: {exc}"})

    return findings


def counts(findings: list[dict]) -> dict[str, int]:
    summary = {PASS: 0, WARN: 0, FAIL: 0}
    for finding in findings:
        summary[finding["level"]] += 1
    return summary


def emit(findings: list[dict], *, namespace: str, as_json: bool) -> int:
    summary = counts(findings)
    if as_json:
        print(
            json.dumps(
                {"namespace": namespace, "summary": summary, "findings": findings},
                indent=2,
            )
        )
    else:
        for finding in findings:
            print(f"{finding['level']:<4} {finding['area']:<11} {finding['message']}")
        print(
            f"\n{summary[PASS]} passed, {summary[WARN]} warning(s), {summary[FAIL]} failed"
        )
    return 1 if summary[FAIL] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--namespace", default="slm-fabric")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--allow-no-gpu",
        action="store_true",
        help="treat a cluster with no GPU as a warning (kind, CPU-only checks)",
    )
    parser.add_argument("--gateway-url", help="optional base URL, e.g. http://localhost:8020 after a port-forward")
    parser.add_argument("--api-key", default=os.environ.get("SLM_API_KEY"), help="admin key for /v1/policy and /v1/audit/verify; never printed (or set SLM_API_KEY)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    namespace = args.namespace
    try:
        nodes = nodes_summary(fetch_json(["get", "nodes"], kubectl=args.kubectl, timeout=args.timeout))
        deployments = deployments_summary(
            fetch_json(["get", "deployments", "-n", namespace], kubectl=args.kubectl, timeout=args.timeout)
        )
        pods = pods_summary(
            fetch_json(["get", "pods", "-n", namespace], kubectl=args.kubectl, timeout=args.timeout)
        )
        endpoints = endpoints_summary(
            fetch_json(["get", "endpoints", "-n", namespace], kubectl=args.kubectl, timeout=args.timeout)
        )
        policy_version = policy_version_from_configmap(
            fetch_json(["get", "configmaps", "-n", namespace], kubectl=args.kubectl, timeout=args.timeout)
        )
    except (ClusterUnreachable, ValueError) as exc:
        findings = [{"level": FAIL, "area": "cluster", "message": str(exc)}]
        return emit(findings, namespace=namespace, as_json=args.json)

    try:
        health = json.loads(
            fetch_text(
                f"/api/v1/namespaces/{namespace}/services/gateway:8000/proxy/health",
                kubectl=args.kubectl,
                timeout=args.timeout,
            )
        )
    except (ClusterUnreachable, ValueError):
        health = None

    findings = evaluate(
        nodes,
        deployments,
        pods,
        endpoints,
        health,
        policy_version,
        allow_no_gpu=args.allow_no_gpu,
    )
    if args.gateway_url and args.api_key:
        findings += api_findings(args.gateway_url, args.api_key, timeout=args.timeout)
    elif args.gateway_url or args.api_key:
        findings.append(
            {
                "level": WARN,
                "area": "api",
                "message": "--gateway-url and --api-key are both required for API checks",
            }
        )

    return emit(findings, namespace=namespace, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
