"""Which pool lives in which cluster: offline checks and a read-only status view.

    python tools/fleet_status.py --check   # inventory only; no cluster needed
    python tools/fleet_status.py --live    # + node and GPU counts per cluster

`config/fleet.yaml` is inventory, not a routing table. It says where the pools in
`config/capacity.yaml` physically run; the gateway still routes only to the
candidates in `config/policy.yaml`, all of which are in the primary cluster
today. A pool in another cluster would need an external candidate with its own
TLS, key and egress rule (ADR-0009), and cross-cluster routing, failover and a
control plane do not exist (ADR-0012).

Offline checks, in the order they protect the inventory:

  * cluster names and kube contexts are unique;
  * exactly one cluster is `primary`, and every role is primary or standby;
  * every cluster names at least one pool;
  * every pool in config/capacity.yaml is assigned to exactly one cluster, and
    every pool a cluster names is declared there;
  * every pool a policy candidate routes to is assigned to a cluster;
  * a `gateway_url`, when present, is https with a host and no userinfo, query
    or fragment.

`--live` additionally runs `kubectl --context <context> get nodes -o json` per
cluster with a 15-second timeout, and reports the node count and how many nodes
carry `nvidia.com/gpu.present=true`. One cluster failing does not stop the
others, but any failure — or a cluster with zero nodes — exits non-zero.

Neither mode reads a kubeconfig, resolves a Secret or prints a token: the only
cluster-specific value that reaches the output is the context name. An injected
runner may be passed to `main`, so tests never touch a real cluster.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    # `python tools/fleet_status.py` puts tools/ first on sys.path, but the pool
    # registry loader lives in the tools package and needs the repo root.
    sys.path.insert(0, str(REPO))

from tools.validate_deploy import load_capacity  # noqa: E402

FLEET_PATH = REPO / "config/fleet.yaml"
CAPACITY_PATH = REPO / "config/capacity.yaml"
POLICY_PATH = REPO / "config/policy.yaml"

ROLES = ("primary", "standby")
GPU_NODE_LABEL = "nvidia.com/gpu.present"
LIVE_TIMEOUT_S = 15


def load_fleet(path: Path = FLEET_PATH) -> dict:
    """Read the inventory into a normalised cluster list.

    Structural problems raise; disagreement *between* files is `check_fleet`'s
    job, so one bad entry is reported alongside the others rather than aborting
    the run before the table is printed.
    """
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must be a mapping")
    raw_clusters = data.get("clusters")
    if not isinstance(raw_clusters, list) or not raw_clusters:
        raise RuntimeError(f"{path} declares no clusters")
    clusters = []
    for index, raw in enumerate(raw_clusters):
        where = f"{path}: clusters[{index}]"
        if not isinstance(raw, dict):
            raise RuntimeError(f"{where} must be a mapping")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise RuntimeError(f"{where} has no name")
        pools = raw.get("pools")
        if pools is None:
            pools = []
        if not isinstance(pools, list):
            raise RuntimeError(f"{where}: pools must be a list")
        clusters.append(
            {
                "name": name,
                "context": str(raw.get("context") or "").strip(),
                "role": str(raw.get("role") or "").strip(),
                "gateway_url": str(raw.get("gateway_url") or "").strip(),
                "pools": [str(pool).strip() for pool in pools],
            }
        )
    return {"version": data.get("version"), "clusters": clusters}


def policy_pool_references(policy: dict) -> list[tuple[str, str]]:
    """Every (alias, pool) a candidate routes to, in file order."""
    references: list[tuple[str, str]] = []
    for alias, body in (policy.get("aliases") or {}).items():
        for candidate in (body or {}).get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            pool = str(candidate.get("pool") or "").strip()
            if pool:
                references.append((str(alias), pool))
    return references


def check_gateway_url(cluster: dict) -> list[str]:
    """A gateway URL is an address, never a credential or a query.

    The URL itself is not echoed in an error: if an operator pasted a token into
    userinfo, repeating it in a CI log is the second mistake.
    """
    url = cluster.get("gateway_url") or ""
    if not url:
        return []
    where = f"cluster {cluster.get('name')}: gateway_url"
    parts = urlsplit(url)
    errors = []
    if parts.scheme != "https":
        errors.append(f"{where} is not https; an external gateway must be TLS")
    if not parts.hostname:
        errors.append(f"{where} has no host")
    if parts.username is not None or parts.password is not None:
        errors.append(f"{where} carries userinfo; a credential belongs in a Secret, not a URL")
    if parts.query:
        errors.append(f"{where} carries a query string")
    if parts.fragment:
        errors.append(f"{where} carries a fragment")
    return errors


def check_fleet(fleet: dict, capacity: dict, policy: dict) -> list[str]:
    """Every error between the inventory, the pool registry and the policy.

    The inventory is the only file that says *where*; the registry says *what*;
    the policy says *who may be sent there*. A pool nothing hosts, a pool hosted
    twice, or a candidate pointing at an unhosted pool is a review-time failure,
    not a runtime 503.
    """
    errors: list[str] = []
    clusters = list(fleet.get("clusters") or [])
    pools = (capacity or {}).get("pools") or {}

    for field, description in (("name", "cluster name"), ("context", "kube context")):
        seen: dict[str, int] = {}
        for cluster in clusters:
            value = cluster.get(field) or ""
            if value:
                seen[value] = seen.get(value, 0) + 1
        for value in sorted(seen):
            if seen[value] > 1:
                errors.append(
                    f"duplicate {description} {value!r}, used by {seen[value]} clusters; "
                    "each must be unique"
                )

    primaries = [cluster["name"] for cluster in clusters if cluster.get("role") == "primary"]
    if len(primaries) != 1:
        named = ", ".join(primaries) or "none"
        errors.append(
            f"fleet declares {len(primaries)} primary cluster(s) ({named}); expected exactly 1"
        )
    for cluster in clusters:
        if cluster.get("role") not in ROLES:
            errors.append(
                f"cluster {cluster['name']}: role {cluster.get('role')!r} is not one of {list(ROLES)}"
            )

    assigned: dict[str, list[str]] = {}
    for cluster in clusters:
        named = list(cluster.get("pools") or [])
        if not named:
            errors.append(f"cluster {cluster['name']} names no pool")
        for pool in named:
            if pool not in pools:
                errors.append(
                    f"cluster {cluster['name']} names pool {pool!r}, "
                    "which config/capacity.yaml does not declare"
                )
                continue
            owners = assigned.setdefault(pool, [])
            if cluster["name"] in owners:
                errors.append(f"cluster {cluster['name']} names pool {pool} more than once")
            else:
                owners.append(cluster["name"])

    for pool in sorted(assigned):
        owners = assigned[pool]
        if len(owners) > 1:
            errors.append(
                f"pool {pool} is assigned to {len(owners)} clusters ({', '.join(owners)}); "
                "a pool lives in exactly one"
            )
    for pool in sorted(pools):
        if pool not in assigned:
            errors.append(
                f"pool {pool} is declared in config/capacity.yaml but assigned to no cluster"
            )

    for alias, pool in policy_pool_references(policy):
        if pool in assigned:
            continue
        if pool in pools:
            errors.append(
                f"alias {alias} routes to pool {pool}, which no cluster in config/fleet.yaml hosts"
            )
        else:
            errors.append(
                f"alias {alias} routes to pool {pool}, which config/capacity.yaml does not declare"
            )

    for cluster in clusters:
        errors.extend(check_gateway_url(cluster))
    return errors


def parse_nodes(payload) -> dict:
    """Count nodes and GPU-labelled nodes from `kubectl get nodes -o json`.

    A malformed document yields zeroes and an item that is not a mapping is
    skipped: the status view reports what it can see rather than raising on the
    one entry it cannot parse. A mapping without labels still counts as a node,
    because it is one.
    """
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {"nodes": 0, "gpu_nodes": 0}
    nodes = 0
    gpu_nodes = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        nodes += 1
        metadata = item.get("metadata")
        labels = metadata.get("labels") if isinstance(metadata, dict) else None
        if isinstance(labels, dict) and str(labels.get(GPU_NODE_LABEL)).lower() == "true":
            gpu_nodes += 1
    return {"nodes": nodes, "gpu_nodes": gpu_nodes}


def node_args(context: str, kubectl: str = "kubectl") -> list[str]:
    return [kubectl, "--context", context, "get", "nodes", "-o", "json"]


def default_runner(args: list[str]) -> subprocess.CompletedProcess:
    """Run kubectl under the fleet's 15-second budget. Injectable for tests."""
    return subprocess.run(
        args, capture_output=True, text=True, timeout=LIVE_TIMEOUT_S, check=False
    )


def check_live(
    fleet: dict, *, runner=default_runner, kubectl: str = "kubectl"
) -> tuple[list[dict], list[str]]:
    """Node and GPU counts per cluster; one cluster's failure does not hide the rest."""
    results: list[dict] = []
    errors: list[str] = []
    for cluster in fleet.get("clusters") or []:
        context = cluster.get("context") or ""
        record = {
            "name": cluster.get("name"),
            "context": context,
            "nodes": None,
            "gpu_nodes": None,
        }
        if not context:
            record["skipped"] = "no context in config/fleet.yaml"
            results.append(record)
            continue
        try:
            proc = runner(node_args(context, kubectl))
            if proc.returncode != 0:
                detail = f"{proc.stderr or ''}{proc.stdout or ''}".strip().splitlines()
                raise RuntimeError(
                    detail[0] if detail else f"kubectl exited {proc.returncode}"
                )
            try:
                payload = json.loads(proc.stdout or "")
            except ValueError as exc:
                raise RuntimeError(f"kubectl returned invalid JSON: {exc}") from exc
            counts = parse_nodes(payload)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            record["error"] = str(exc)
            errors.append(f"cluster {record['name']} ({context}): {record['error']}")
            results.append(record)
            continue
        record.update(counts)
        if counts["nodes"] == 0:
            record["error"] = "kubectl returned no nodes"
            errors.append(f"cluster {record['name']} ({context}): {record['error']}")
        results.append(record)
    return results, errors


def render_table(fleet: dict) -> list[str]:
    rows = [("CLUSTER", "CONTEXT", "ROLE", "POOLS", "POOL NAMES")]
    for cluster in fleet.get("clusters") or []:
        pools = cluster.get("pools") or []
        rows.append(
            (
                str(cluster.get("name")),
                cluster.get("context") or "-",
                cluster.get("role") or "-",
                str(len(pools)),
                ", ".join(pools) or "-",
            )
        )
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    return [
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
        for row in rows
    ]


def render_live(results: list[dict]) -> list[str]:
    lines = []
    for record in results:
        where = f"{record['name']} ({record['context'] or 'no context'})"
        if record.get("error"):
            lines.append(f"FAIL {where}: {record['error']}")
        elif record.get("skipped"):
            lines.append(f"SKIP {where}: {record['skipped']}")
        else:
            lines.append(
                f"LIVE {where}: {record['nodes']} node(s), {record['gpu_nodes']} GPU node(s)"
            )
    return lines


def main(argv: list[str] | None = None, *, runner=default_runner) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="offline inventory checks (default)")
    mode.add_argument("--live", action="store_true", help="also read node and GPU counts")
    parser.add_argument("--fleet", type=Path, default=FLEET_PATH)
    parser.add_argument("--capacity", type=Path, default=CAPACITY_PATH)
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    parser.add_argument("--kubectl", default="kubectl", help="kubectl binary for --live")
    args = parser.parse_args(argv)

    try:
        fleet = load_fleet(args.fleet)
        capacity = load_capacity(args.capacity)
        policy = yaml.safe_load(Path(args.policy).read_text(encoding="utf-8")) or {}
        if not isinstance(policy, dict):
            raise RuntimeError(f"{args.policy} must be a mapping")
    except (RuntimeError, OSError, yaml.YAMLError) as exc:
        print(f"FAIL {exc}")
        return 1

    errors = check_fleet(fleet, capacity, policy)
    for line in render_table(fleet):
        print(line)
    if errors:
        print(f"\nFAIL fleet: {len(errors)} problem(s)")
        for error in errors:
            print(f"  - {error}")
        return 1

    clusters = fleet.get("clusters") or []
    pool_count = sum(len(cluster.get("pools") or []) for cluster in clusters)
    print(f"\nOK fleet {len(clusters)} clusters / {pool_count} pools")
    if not args.live:
        return 0

    results, live_errors = check_live(fleet, runner=runner, kubectl=args.kubectl)
    print()
    for line in render_live(results):
        print(line)
    if live_errors:
        print(f"\nFAIL live: {len(live_errors)} problem(s) across {len(results)} cluster(s)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
