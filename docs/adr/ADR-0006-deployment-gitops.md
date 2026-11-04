# ADR-0006 — Deploy with Kustomize overlays, reconcile with Argo CD, enforce at admission

- **Status:** Accepted
- **Date:** 2026-11-04
- **Deciders:** Platform architecture
- **Related:** ADR-0001 (runtime), ADR-0004 (policy router), ADR-0005 (release gate)

## Context

Phases 0–4 produced a working stack and the evidence to defend its behaviour, but it ran
under `docker compose` on one host. The review target is a regulated, OpenShift-first
estate with three deployment shapes: connected OpenShift, plain Kubernetes, and an
air-gapped site that pulls every byte from an internal mirror. The existing directory
scaffolding (`deploy/openshift`, `deploy/kustomize`, `deploy/gitops`, `deploy/policies`)
was empty.

Three properties from earlier phases made the deployment design non-obvious:

1. **`config/policy.yaml` is the routing contract.** Its backend URLs are hostnames. If
   Kubernetes Services are named differently, the cluster runs a different policy from the
   one this repository tests, and the divergence is invisible until a failover.
2. **Gateway state is in-process.** Rate-limit counters, breaker state and the audit chain
   assume one writer. A rolling update with two replicas partitions quotas and interleaves
   two writers into one audit file.
3. **One GPU, one model copy.** A surge pod needs a second GPU; a rolling update cannot be
   used for the vLLM tier without halving KV-cache headroom or buying hardware.

## Options considered

### Manifest tooling

| Option | Where it wins | Where it loses |
|---|---|---|
| Helm chart | Values per environment, familiar to operators | Templates the security and exposure surface behind conditionals; a diff is harder to review |
| Plain YAML per environment | No tooling | Three copies of every workload drift immediately |
| **Kustomize base + overlays** | Environment deltas are patches on a reviewed base; native `kubectl`; Argo CD support | Overlay paths can escape the tree; needs a load-restrictor flag for the shared policy file |
| Operators / CRDs for the stack | Reconciliation built in | Writing an operator for four workloads is more code than the platform it serves |

### Release strategy

| Option | Where it wins | Where it loses |
|---|---|---|
| Rolling update gateway | Zero downtime | Two writers for in-process quotas, breakers and audit |
| Argo Rollouts canary gateway | Fine-grained traffic split | Still two gateway replicas; needs an external state store first |
| **`Recreate` gateway, model canary in policy** | One writer; canary traffic already exists per tenant in `policy.yaml` | A gateway pod restart is brief downtime behind the Route |
| Argo Rollouts canary vLLM | Standard pattern | Requires a second GPU and a second full model copy |

### Air-gap mechanism

| Option | Where it wins | Where it loses |
|---|---|---|
| Rewrite every image in the overlay | Explicit in Git | Diverges from the connected manifests; easy to miss an image |
| Pull-through cache only | Simple | The cache itself must be reachable; not a disconnected install |
| **IDMS/ITMS + mirrored tag list** | Cluster-wide, source manifests unchanged, auditable image list | Requires cluster-admin bootstrap and a working mirror job |

## Decision

1. **Kustomize base, three overlays.** `deploy/kustomize/base` holds the workloads;
   `overlays/openshift`, `overlays/vanilla` and `overlays/airgap` hold the environment
   deltas. Shared behaviour (model download, release hooks) is expressed as components.
2. **Service names are load-bearing.** Services are named `vllm-gpu`, `tiersim-cpu`,
   `tiersim-cloud` and `gateway`, which is exactly what the policy backends reference. The
   validator parses `config/policy.yaml`, resolves every candidate URL to a Service and
   port in the rendered set, and fails if any alias points at something the cluster does
   not expose.
3. **One policy source, generated into a ConfigMap.** The base generates `slm-policy` from
   `config/policy.yaml` with `--load-restrictor LoadRestrictionsNone`. Argo CD v3 removed
   `spec.source.kustomize.buildOptions`, so the flag is set once for the repository in
   `argocd-cm` (`kustomize.buildOptions`) rather than per Application; the manifest
   comments carry the patch. The generated name
   carries a content hash, so a policy edit changes the gateway pod template and rolls the
   pod. The validator asserts the rendered ConfigMap is identical to the file, so a stale
   or hand-edited cluster copy fails the check.
4. **Gateway: one replica, `Recreate`, PVC-backed audit.** Scaling requires externalising
   quota and breaker state and a single-writer audit sink; until then a second replica is
   a correctness bug, not a capacity win. The release canary lives in the routing policy,
   where it is per-tenant and sticky, not in a Deployment rollout.
5. **vLLM: `Recreate`, one GPU, pinned runtime and revision.** The model revision from
   Phase 0 (`b37d1ccb…`) and the runtime tag (`v0.33.0`) are checked by the validator. The
   model cache and adapter weights are PVCs, not image layers. vLLM starts with
   `--enable-lora`; a PostSync hook loads the declared adapter set, and a second PostSync
   hook runs the Phase 4 golden-set gate against the served adapter. A gate failure leaves
   the Argo application failed and un-promoted.
6. **Restricted Pod Security, enforced twice.** The namespace carries the restricted Pod
   Security Standard labels, and Kyverno `ClusterPolicy` objects enforce non-root, no
   privilege escalation, `drop: ALL`, `RuntimeDefault` seccomp, resource requests/limits,
   readiness probes, pinned images and no host namespaces. vLLM is the single documented
   exception to `readOnlyRootFilesystem` because CUDA/Triton/inductor caches write outside
   `/tmp`.
7. **Vanilla Kubernetes pins UID 1001; OpenShift does not.** OpenShift's restricted SCC
   assigns an arbitrary non-root UID, and hardcoding one is rejected. Plain Kubernetes has
   no such assignment, so the vanilla overlay sets `runAsUser`/`fsGroup: 1001` with
   `OnRootMismatch`. The upstream vLLM image runs as root by default; `runAsNonRoot`
   without a UID would fail there.
8. **Default-deny networking, explicit call graph.** Only the gateway may reach the
   backends; only the gateway port is exposed; connected overlays add public HTTPS egress
   for vLLM's model download, and the air-gap overlay deletes that rule and sets
   `HF_HUB_OFFLINE=1`. DNS is allowed to `kube-system` and, on OpenShift, `openshift-dns`.
9. **GitOps as app-of-apps.** An Argo CD `AppProject` restricts sources and destinations,
   blacklists Secrets, and permits only the cluster-scoped kinds the project needs. The
   root Application reconciles `deploy/gitops/apps`; sync waves install admission policy
   (0) before the platform (10). The platform Application points at the OpenShift overlay;
   the policy generator's build flag is a repository-wide `argocd-cm` setting, because
   Argo CD v3 rejects the per-Application `kustomize.buildOptions` field.
10. **Air-gap is a mirror, not a fork.** `deploy/openshift/airgap` contains
    `ImageDigestMirrorSet`, `ImageTagMirrorSet`, the exact `images.txt` list and a mirror
    script. The application overlay stays identical to the connected one apart from offline
    environment variables and the deleted egress rule.

## Consequences

### Verified here

- Three overlays render with `kubectl kustomize` (v5.7.1): 32 objects for OpenShift,
  25 for vanilla, 31 for air-gap (the model-download policy is deleted).
- `python tools/validate_deploy.py` passes all three: image pins, model revision, security
  contexts, resources, probes, Service/policy wiring, ConfigMap equality, single-replica
  gateway, no Secrets, namespace placement, overlay-specific exposure.
- 19 deployment tests (synthetic negative cases plus rendered-overlay integration); the
  full suite is 106 tests.
- A kind v0.33.0 cluster applied the vanilla overlay (25 objects, gateway and both
  simulators running), evaluated the Kyverno policies in Enforce, and reconciled the
  policies Application with self-heal. That pass found a nested `subPath` mount that static
  review missed; it is fixed and covered by `check_mounts`.
- The release-gate image builds and runs as UID 1001 with the golden set and gate config
  at the paths the hook passes.

### Limits, stated rather than hidden

- **No OpenShift in this environment.** The vanilla path was applied to kind; OpenShift's
  SCC, Route, ServiceMonitor and release hooks are still static. kind's CNI does not
  enforce NetworkPolicy and has no ingress controller, so those paths remain declarative.
  Argo CD v3 removed the per-Application `kustomize.buildOptions` field; the build flag now
  lives in `argocd-cm` and an Application that carries the old field is rejected at decode.
- **Images are pinned by tag, not digest.** `vllm/vllm-openai:v0.33.0` is the Phase 0
  runtime tag; production should rewrite tags to digests at promotion. The validator
  accepts either.
- **Storage classes are not named.** PVCs rely on the cluster default. A real install
  patches the storage class and, for the model cache, sizes to the chosen weights.
- **The in-cluster release gate verifies, it does not auto-rollback.** A failed PostSync
  hook marks the sync failed and leaves the previous ReplicaSet/pods in place only when
  nothing was applied; rollback is a Git revert. The authoritative gate remains CI
  (`make eval-gate`) before the manifest revision is merged.
- **Gateway availability is one pod.** `Recreate` and a single replica mean a short gap on
  restart and no node-failure tolerance. This is a deliberate consequence of in-process
  state, recorded here so it is not mistaken for an oversight.
- **Hooks are part of the OpenShift overlay.** `kubectl apply -k` on that overlay creates
  the hook Jobs immediately; the Argo annotations are inert without Argo CD. The vanilla
  overlay omits them.

## Alternatives rejected

- **Helm.** The environment delta is the security and exposure surface. Patches make that
  diff reviewable; a chart with three values files hides it in conditionals.
- **A gateway canary via Argo Rollouts.** It would require the state externalisation that
  the single-replica decision deliberately defers. The existing per-tenant policy canary
  tests the thing that changes on a model release — the weights — without duplicating
  quota state.
- **Rewriting image names in the air-gap overlay.** IDMS/ITMS keeps one manifest set and
  makes the mirror mapping a cluster-level, auditable object.
- **Committing a placeholder Secret for local convenience.** The tenant Secret is created
  out of band (`make deploy-secret`), the AppProject blacklists Secrets, and the validator
  fails on any Secret in the rendered set.
