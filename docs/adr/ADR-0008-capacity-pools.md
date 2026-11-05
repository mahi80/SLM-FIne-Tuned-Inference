# ADR-0008 — Capacity is a pool registry; `tier` is derived from compute and deployment

- **Status:** Accepted
- **Date:** 2026-11-05
- **Deciders:** Platform architecture
- **Related:** ADR-0004 (policy router), ADR-0006 (deployment), ADR-0007 (signed releases)

## Context

The policy schema described a backend with one field, `tier`, drawn from
`{gpu, cpu, cloud}`. That field answered two different questions at once — what hardware the
work runs on, and whose premises it runs on. The consequences showed up as soon as the
platform grew past one on-prem machine:

- A cloud GPU and a cloud CPU were both `tier: cloud`, so a tenant could not ask for "any
  GPU, on-prem only" without forbidding the cloud entirely.
- `config/capacity.yaml` described exactly one workload's scaling bounds. Adding a second
  pool meant either a second bespoke file or a second document with the same shape.
- Nothing connected a policy candidate to the capacity that backed it. A candidate naming a
  backend that no pool, node pool or deployment provided was valid YAML and passed review.
- Provisioning instructions for AWS, Azure, GCP and on-prem workers did not exist, so
  "add a cloud pool" was an idea rather than a contract.

## Decision

1. **Split the two questions.** A candidate declares `compute: gpu|cpu` and
   `deployment: onprem|cloud`, and names a `pool`. The client-facing `tier` is derived:
   `cloud` when `deployment` is cloud, otherwise `compute`. Response headers,
   `/v1/policy` and audit records keep their meaning, so clients and evidence do not change.
2. **Make `config/capacity.yaml` a registry.** It lists pools, each declaring compute,
   deployment, provider, endpoint, workload, replica bounds, scaling metrics,
   prerequisites and the node-pool label/taint. One file answers "where may work land, and
   how much of it is there?" for every tier.
3. **Admission gains two allowlists.** Tenants may set `allowed_compute` and
   `allowed_deployment` beside `allowed_tiers`; all three are veto checks that run before
   scoring, and the audit record names the veto that fired.
4. **Check the three-way agreement.** `tools/validate_deploy.py` fails if a candidate names
   an undeclared pool, if its compute or deployment disagrees with the pool, or if its
   backend is not the pool's endpoint. `config/policy.yaml` is therefore signed together
   with the capacity it depends on.
5. **Ship node-pool contracts, not cloud claims.** `deploy/cloud/` contains Karpenter
   `NodePool`s for EKS, `az aks nodepool add` and `gcloud container node-pools create`
   scripts, and a label/taint script for on-prem OpenShift workers. They apply the same
   `nvidia.com/gpu.present` label, `nvidia.com/gpu` taint and `slm.fabric/pool=cpu` CPU
   contract the manifests already select. A cloud pool stays `provider: simulated` and
   `modelled: true` until external backends, TLS, credentials and egress exist.

## Consequences

**Positive:** the router's vocabulary now matches the two questions operators actually ask,
and one registry is the review artifact for capacity. Adding a pool is a data change plus a
deployment, not a router change. A policy edit that points at capacity nobody declared fails
CI rather than 503 at runtime.

**Negative / limits, stated rather than hidden:**

- `tier` remains in the API for compatibility, so two representations exist. It is derived
  in one place, but a future reader can still be confused by `tier: cloud` on a GPU.
- The registry describes capacity; it does not verify that a cluster provides it. A `max`
  above the provisioned GPUs leaves extra pods Pending — see R-17.
- The node-pool snippets were written against current provider APIs but have not been
  applied. They are contracts for a cluster owner, not tested infrastructure.
- A cloud route is still unreachable. The gateway egress policy allows only in-cluster
  Pods, and there is no external TLS or credential model. That is the next slice, not a
  consequence of this one.

**Revisit when:** an external backend type lands (Stage 3), a tenant needs guaranteed
capacity rather than best-effort standbys, or a per-pool latency budget becomes an admission
input (Stage 4).
