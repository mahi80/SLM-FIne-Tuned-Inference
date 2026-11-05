# ADR-0012 — The fleet is an inventory, not a routing table

- **Status:** Accepted
- **Date:** 2026-11-05
- **Deciders:** Platform architecture
- **Related:** ADR-0008 (capacity pools), ADR-0009 (external backends), ADR-0006 (deployment)

## Context

ADR-0008 made `config/capacity.yaml` the registry of every pool the router may send work to,
and ADR-0009 made an out-of-cluster backend describable and reachable. Both files answer
"what capacity exists"; neither answers **where it lives**. That gap showed up three ways:

- The pool registry and the policy describe endpoints as Service names
  (`http://vllm-gpu:8000`). A Service name is meaningful only inside one cluster, so nothing
  in the repo said how many clusters the platform was expected to span.
- `deploy/cloud/` contained node-pool contracts for AWS, Azure, GCP and on-prem workers, but
  no document said which of those pools belonged to a cluster that exists, and which were
  still a plan. A reviewer could not tell a provisioned pool from a wish.
- `scripts/cluster_health.py` inspects one cluster, selected by the ambient kube context. It
  is the right tool for "is this cluster healthy", and the wrong tool for "what is in the
  fleet" — it requires a cluster, and it reports health rather than composition.

The tempting next step was to make the gateway route across clusters: a candidate per
cluster, a health probe per cluster, a failover rule. That is a distributed-systems project
— cross-cluster identity, traffic policy, split-brain rules for the in-process breaker and
quota state, a control plane to publish policy to more than one gateway. Shipping the
appearance of it in a `fleet.yaml` would be worse than shipping nothing.

## Decision

1. **`config/fleet.yaml` is inventory.** It maps cluster name → kube context → role →
   the pools that physically run there, with an optional `https` gateway URL. It is
   committed, reviewed and versioned beside the policy it describes.
2. **The inventory is checked, offline.** `tools/fleet_status.py --check` fails unless:
   cluster names and kube contexts are unique; exactly one cluster is `primary` and every
   role is `primary` or `standby`; every cluster names at least one pool; every pool in
   `config/capacity.yaml` is assigned to exactly one cluster; every named pool exists; every
   pool a policy candidate routes to is assigned to a cluster; and a `gateway_url` is
   `https` with a host and no userinfo, query or fragment. The last rule exists because a
   URL is the easiest place to leak a credential into Git. `make fleet-check` and CI run it
   after the deployment validator.
3. **A status view, not a control plane.** `tools/fleet_status.py --live` (and
   `make fleet-status`) runs `kubectl --context <context> get nodes -o json` per cluster with
   a 15-second timeout and prints the node count and the number of nodes labelled
   `nvidia.com/gpu.present=true`. One unreachable cluster does not abort the others; any
   failure or a cluster with zero nodes exits non-zero. The kubectl runner is injectable, so
   the tests never touch a real cluster.
4. **The inventory never carries credentials.** No kubeconfig is read or embedded; the only
   cluster-specific value that reaches output is the context name. A gateway URL that fails
   validation is reported without echoing the URL, so a pasted token is not repeated in a CI
   log.
5. **One cluster is real.** `local` (`kind-slm`) is the only cluster that exists in this
   build. Any further entry is a declaration of intent with no traffic behind it.

## Consequences

**Positive:** "which pool lives where" is now a reviewed file rather than a paragraph in a
runbook, and drift between the pool registry, the policy and the inventory fails CI instead
of surfacing as a 503 or a Pending pod. An operator can see the composition of the fleet and
its node counts without reading manifests, and adding a cluster is a data change plus a
provisioning step.

**Negative / limits, stated rather than hidden:**

- **This is not routing.** Cross-cluster routing, failover, traffic weighting and a control
  plane are **not implemented**. The gateway routes only to candidates in one policy, all of
  which resolve to Services in the cluster it runs in. Moving a pool to another cluster means
  a new external candidate with its own TLS, key and egress rule (ADR-0009) — and a new
  gateway deployment there, not a fleet-file edit.
- **`role` is documentation.** `standby` does not mean warm, replicated or failover-ready;
  nothing promotes it. A single `primary` is enforced because two would imply a
  coordination problem this platform does not solve.
- **Only one cluster was actually reachable.** The live view was exercised against
  `kind-slm`; the multi-cluster path is proven by injected-runner tests, not by a second
  cluster. Node counts are a census, not a health check: a NotReady node still counts.
- **The inventory does not verify provisioning.** A cluster entry naming a pool whose
  hardware was never ordered passes the check, exactly as the capacity registry's `max`
  can exceed the GPUs that exist (R-17).
- **Nothing re-signs the fleet file.** `config/fleet.yaml` is not part of the release
  manifest or the sign-off artifacts; it is validated on every push but not bound into the
  signed release, so an inventory-only change does not require a new signature.

**Revisit when:** a second cluster is actually provisioned and needs traffic, a tenant
requires cross-cluster residency guarantees, or a control plane is asked for. The first of
those should start with per-cluster gateways and explicit external candidates, not with
routing logic in the fleet file.
