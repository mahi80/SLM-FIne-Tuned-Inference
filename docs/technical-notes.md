# Technical notes

The detail behind the [README](../README.md): evidence tables, contracts and measured results.
Read the README first if you only want to run the platform. The architecture decision records
in [adr/](adr/) hold the reasoning behind each choice; these notes collect the summary.

## Contents

- [What is real, and what is modelled](#what-is-real-and-what-is-modelled)
- [Kubernetes and OpenShift](#kubernetes-and-openshift)
- [Signed releases and the review pack](#signed-releases-and-the-review-pack)
- [Measured on this machine](#measured-on-this-machine)
- [Gateway contract](#gateway-contract)
- [Routing is a policy, not a table](#routing-is-a-policy-not-a-table)
- [Fine-tuning: two adapters, one contract](#fine-tuning-two-adapters-one-contract)
- [Benchmarks, release gates and cost](#benchmarks-release-gates-and-cost)
- [Latency budget and cost](#latency-budget-and-cost)
- [Repository map](#repository-map)

---

## What is real, and what is modelled

An architecture document that blurs this line is worthless in review, so the repo keeps
the distinction explicit:

| Layer | Status | Evidence |
|---|---|---|
| GPU tier (vLLM, AWQ W4A16, multi-LoRA capable) | **Measured** | `make smoke`, tables below |
| Fine-tuned adapters (QLoRA v1/v2, hot load / unload) | **Measured** | `make adapter-train`, `make adapter-smoke`, eval table below |
| Gateway (auth, quotas, streaming proxy, policy router, audit) | **Measured** | `make smoke-gateway` |
| Router (admission, canary, shadow, circuit breakers, reload) | **Measured** | `make smoke-policy`, outage drill below |
| Hash-chained audit + verification | **Measured** | `GET /v1/audit/verify` |
| Benchmarks (closed-loop, GPU + modelled tiers) | **Measured / MODELLED per run** | `make bench`, `bench/results/tier-comparison.json` |
| Release eval gate (24-ticket golden set, floors + drift) | **Measured** | `make eval-gate`, `src/eval/golden.jsonl` |
| Cost model (ownership TCO + modelled tiers) | **Measured throughput, stated assumptions** | `make cost`, `config/cost.yaml` |
| Observability (Prometheus + provisioned Grafana) | **Measured** | dashboard `slm-platform`, 7 panels |
| CPU tier (OpenVINO / llama.cpp on Xeon/EPYC) | **Modelled** — calibrated simulator, no Xeon on this laptop | `src/tiersim`, labelled `x-simulated: true` |
| Cloud burst tier (L4 profile) | **Modelled and wired** — a standby that residency usually excludes | `tiersim-cloud`, `x-simulated: true` |
| External backend path (TLS, keyed by reference, egress rule, health probes) | **Implemented and unit tested** — no live provider was called | `src/gateway/backends.py`, `tests/test_backends.py`, [A-26](signoff/acceptance.md) |
| OpenShift / vanilla / air-gap deployment | **Vanilla verified on kind; OpenShift and air gap static** — 25 objects applied, gateway and both simulators running | `deploy/kustomize`, `make deploy-validate`, [A-17](signoff/acceptance.md) |
| GitOps, release hooks and admission policy | **Admission and Argo verified on kind; hooks unrun** — Kyverno v1.19.2 denials and Argo v3.6.1 self-heal observed | `deploy/gitops`, `deploy/policies`, [A-20](signoff/acceptance.md), [A-21](signoff/acceptance.md) |
| Signed release manifest (Ed25519, canonical JSON, drift checks) | **Measured** | `make release`, `make release-check`, `release/slm-0.5.0.json` |
| Image supply (first-party build → GHCR → digest pin) | **Measured (hosted CI + registry)** — images run `41935648483` published three public packages; all four workload images render as `@sha256:`; no cluster pulled them yet | `.github/workflows/images.yml`, [A-22](signoff/acceptance.md) |
| C4 views, threat model, acceptance and sign-off pack | **Documented** — review artifacts, not runtime evidence | `docs/architecture/c4.md`, `docs/signoff/` |
| CI pipeline (tests + both validators) | **Verified** — GitHub Actions run `41875084383`, all steps green | `.github/workflows/ci.yml` |

Numbers from a simulated tier are labelled `MODELLED` everywhere they appear.

---

## Kubernetes and OpenShift

The compose stack is the developer loop. The cluster path keeps the same
`config/policy.yaml` and the same Service names, so a routing rule tested locally resolves
to a real Service after deployment — and the validator fails the build if it does not.

```bash
make deploy-images   # optional: build local copies; the cluster pulls the CI-published digests
make deploy-validate # render all three overlays and check the invariants (no cluster needed)
make deploy-render   # write deploy/rendered/{openshift,vanilla,airgap}.yaml
make deploy-secret   # create slm-tenants from the gitignored config/tenants.yaml
```

| Overlay | What it adds | Target |
|---|---|---|
| `openshift` | Route with edge TLS and a 300s timeout, ServiceMonitor, monitoring scrape policy, release hooks | Reference substrate |
| `vanilla` | nginx Ingress, explicit UID/fsGroup 1001, no hooks | EKS / AKS / GKE / kind |
| `airgap` | OpenShift shape plus offline Hugging Face env and no model-download egress | Disconnected site |

The manifests resolve first-party images from `ghcr.io/mahi80/*` and vLLM from Docker Hub,
each pinned by digest. The GHCR packages are public; for a private mirror, retag the same
digests into the registry your cluster pulls from (the air-gap overlay has an `oc image
mirror` list). The kustomization reads `config/policy.yaml` from outside its own tree, so
Argo's repo server needs the kustomize load-restrictor flag. Argo CD v3 removed the
per-Application `spec.source.kustomize.buildOptions` field and rejects an Application that
carries it, so set the flag once for the repository:

```bash
kubectl -n argocd patch configmap argocd-cm --type merge -p \
  '{"data":{"kustomize.buildOptions":"--load-restrictor LoadRestrictionsNone"}}'
kubectl -n argocd rollout restart deploy/argocd-repo-server
```

Then reconcile:

```bash
kubectl apply -f deploy/gitops/argocd/project.yaml
kubectl apply -f deploy/gitops/argocd/app-of-apps.yaml
```

Prerequisites are Argo CD, Kyverno, the NVIDIA GPU Operator, KEDA on the operator overlays
(the `ScaledObject` CRD must exist), a `ReadWriteMany` storage class for the model and
adapter claims, and either Prometheus Operator or OpenShift user-workload monitoring. Seed
the `slm-adapters` PVC from `make adapter-train` before the first sync; the loader hook
declares `triage-v1`/`triage-v2` and fails the sync if either is absent. The root app syncs admission policy (`slm-policies`, wave 0) before
the platform (`slm-platform`, wave 10); the platform's PostSync hooks then load the
adapters into vLLM and run the golden-set gate against the served adapter. A failed gate
leaves the application failed and un-promoted.

```text
$ python tools/validate_deploy.py
OK   openshift: 35 objects
OK   vanilla: 25 objects
OK   airgap: 34 objects
```

The validator is the review artifact. It re-renders the overlays and checks:

- every candidate URL in `config/policy.yaml` resolves to a Service and port in the
  rendered set (external candidates are exempt: they have no Service), and the policy
  ConfigMap is identical to the file on disk;
- the vLLM image and model revision are the Phase 0 pins, and multi-LoRA is enabled;
- restricted security contexts, resource requests/limits and readiness probes on every
  workload, with vLLM the single documented exception to a read-only root filesystem;
- no Secret material, no host namespaces, no `latest` tags, and a single `Recreate`
  gateway because quota, breaker and audit state are in-process;
- the overlay actually exposes what it claims: a Route or Ingress for the gateway, a
  ServiceMonitor and scrape policy on OpenShift, offline env in the air gap, and the five
  platform alerts on every overlay that installs the Prometheus Operator;
- every candidate names a pool declared in `config/capacity.yaml`, and its compute,
  deployment and backend agree with that pool;
- the GPU scaler agrees with `config/capacity.yaml`, keeps a warm floor of at least one
  replica, shares the model and adapter claims `ReadWriteMany`, preloads every adapter the
  loader hook declares, and is never pointed at the gateway;
- an external candidate and its pool opt in together: the gateway mounts the optional
  `slm-backend-keys` Secret, `SLM_BACKEND_KEYS` points inside that mount, and an egress rule
  opens TCP 443 to a public provider range only — never a default, private, loopback or
  link-local CIDR. The component without a candidate, and a candidate without the
  component, both fail.

**Two node pools.** The GPU tier is hard-pinned: it selects `nvidia.com/gpu.present=true`
and tolerates the `nvidia.com/gpu:NoSchedule` taint the GPU Operator applies. The gateway
and both simulators are different — they must run where a GPU is not being wasted, but a
single-node kind cluster has no separate pool. So `components/cpu-placement` adds a
*preferred* `slm.fabric/pool=cpu` affinity plus a toleration for a `slm.fabric/pool=cpu`
taint. Preference never blocks scheduling; the taint is what enforces separation once you
add the second pool:

```bash
# CPU pool (gateway, tiersim-cpu, tiersim-cloud)
kubectl label node <cpu-node> slm.fabric/pool=cpu
kubectl taint node <cpu-node> slm.fabric/pool=cpu:NoSchedule

# GPU pool: the Operator labels these; on plain Kubernetes label them by hand
kubectl label node <gpu-node> nvidia.com/gpu.present=true
```

The placement rules are asserted by the validator and by six tests, so an overlay that
loses its toleration fails review rather than silently filling the GPU nodes with simulators.

**Is the cluster healthy?** Static checks prove what will be created; they cannot see a
CrashLoop or a drained GPU node. `make cluster-health` reads the live cluster through the
same kubeconfig and reports PASS/WARN/FAIL for nodes, GPU capacity, Deployment availability,
endpoints, the policy ConfigMap and the gateway's own health payload:

```text
$ make cluster-health
PASS cluster     Kubernetes v1.33.1, 3 nodes ready
PASS gpu-capacity 1 allocatable nvidia.com/gpu on 1 node
PASS deployments gateway, tiersim-cloud, tiersim-cpu, vllm-gpu available
PASS endpoints   gateway, tiersim-cloud, tiersim-cpu, vllm-gpu have ready addresses
PASS policy      policy_version 3 matches ConfigMap slm-policy-6f2c9
PASS gateway     status ok, 4 alias(es), 0 breaker(s) open
```

`--allow-no-gpu` downgrades vLLM-tier findings to warnings (for kind and CPU-only test
clusters). `--json` emits the same result for CI. The gateway URL is reached through the
API-server service proxy, so no port-forward is needed; `--gateway-url` plus `--api-key`
enable the tenant-level `/v1/policy` and `/v1/audit/verify` checks against a real Route,
with the key taken from `SLM_API_KEY` so it stays out of shell history and process listings.

Five alerts ship in `PrometheusRule/slm-fabric`: `SlmGatewayUnavailable` (routing stopped),
`SlmGpuTierUnavailable` (router should be failing over), `SlmNoGpuCapacity` (no node
advertises a GPU), `SlmBreakerOpen` (a candidate has been failing for five minutes) and
`SlmPodRestartLoop`. The first and fourth are separated because they need different first
actions: one is a restart-and-inspect, the other is a backend problem the router is already
compensating for.

**Scaling the GPU tier.** Bounds and signals live in `config/capacity.yaml`; the
`vllm-autoscaling` component renders them as a KEDA `ScaledObject` on the OpenShift and
air-gap overlays. Replicas are not pinned in Git: the component removes `spec.replicas` from
`Deployment/vllm-gpu`, and the Argo Application ignores that field so `selfHeal` cannot undo
a scale event.

| Trigger | Query | Threshold | Why |
|---|---|---|---|
| queue depth | `sum(vllm:num_requests_waiting{namespace="slm-fabric"})` | 4 waiting | vLLM admits 16 sequences; a quarter of a batch queued means the next token is already late |
| KV cache | `avg(vllm:gpu_cache_usage_perc{namespace="slm-fabric"})` | 0.85 | above ~85% the engine preempts sequences, and queue depth only reacts after that |

Scale-up adds one pod per five minutes with no stabilisation window; scale-down waits fifteen
minutes and removes one pod per ten. Both are slow on purpose: a replica costs a whole GPU and
minutes of weight loading, so the scaler follows a trend, not a spike. The floor is one — the
warm floor that keeps `slm-triage` answering — and the ceiling is three.

Three things make a second replica useful, and each is enforced rather than hoped for:

- **RWX claims.** A `ReadWriteOnce` claim attaches to one node, so the second pod stays
  Pending. The component patches `slm-model-cache` and `slm-adapters` to `ReadWriteMany`, and
  the validator fails the render if either is otherwise. Access modes are immutable, so on an
  existing install both claims must be recreated before enabling the component.
- **Per-replica adapter preload.** The PostSync hot-load reaches exactly one pod. The
  component appends `--lora-modules` entries derived from `slm-adapter-set`, so a replica the
  scaler adds serves the fine-tuned aliases from its first request.
- **A node autoscaler.** KEDA sizes pods; it does not add nodes. The GPU pool needs Karpenter,
  Cluster Autoscaler or an OpenShift `MachineAutoscaler` watching the same
  `nvidia.com/gpu.present` label and taint.

The gateway is never a scale target: its quota counter, breaker state and audit-chain head
are in-process, so two replicas would mean two quotas and two forked audit chains. Load sheds
at the tier, not at the router, and the validator fails any HPA or ScaledObject aimed at
`gateway`.

On OpenShift the triggers read Thanos and authenticate with a bearer token held in the
`slm-keda-prometheus` Secret, created out of band and never committed:

```bash
oc create sa keda-scaler -n slm-fabric
oc adm policy add-cluster-role-to-user cluster-monitoring-view -z keda-scaler -n slm-fabric
oc create token keda-scaler -n slm-fabric --duration=8760h > /tmp/token
oc get configmap service-ca.crt -n openshift-config-managed \
  -o jsonpath='{.data.service-ca\.crt}' > /tmp/service-ca.crt
oc create secret generic slm-keda-prometheus -n slm-fabric \
  --from-file=token=/tmp/token \
  --from-file=ca.crt=/tmp/service-ca.crt
rm /tmp/token /tmp/service-ca.crt
```

Vanilla and kind get no scaler at all — kind has neither KEDA nor a second GPU node — so the
base Deployment keeps its single, explicitly pinned replica, and the validator rejects a
scaler in that overlay.

**Pools, not just tiers.** A `tier` used to say both what hardware a backend runs on and
where it runs, so a cloud GPU was indistinguishable from a cloud CPU and no tenant could ask
for "any GPU, on-prem only". A candidate now declares `compute: gpu|cpu` and
`deployment: onprem|cloud`, names a `pool`, and the client-facing `tier` is derived — `cloud`
when `deployment` is cloud, otherwise `compute`. Tenants gain `allowed_compute` and
`allowed_deployment` beside `allowed_tiers`, all enforced in admission before scoring.

Every candidate's pool must exist in `config/capacity.yaml`, which is now the registry of
places work may land:

| Pool | Compute | Deployment | Provider | Replicas | Autoscaler |
|---|---|---|---|---|---|
| `onprem-gpu` | gpu | onprem | onprem | 1–3 | KEDA + a node autoscaler |
| `onprem-cpu` | cpu | onprem | onprem | 1–1 | none |
| `cloud-burst` | gpu | cloud | simulated | 1–1 | external |

`modelled: true` marks a pool with no real hardware behind it; `onprem-cpu` and `cloud-burst`
are calibrated simulators today. `deploy/cloud/` holds the node-pool snippets that stand a
pool up — Karpenter `NodePool`s for EKS, `az aks nodepool add`, `gcloud container
node-pools create`, and a label/taint script for on-prem OpenShift workers. Each applies the
same `nvidia.com/gpu.present` label, `nvidia.com/gpu` taint and `slm.fabric/pool=cpu` CPU
contract the manifests already select, and the tests parse them so a typo cannot drift.

Capability is not the same as a working route. The snippets buy machines; they do not make a
cloud endpoint reachable. Stage 2 added the missing half — an external backend type with
TLS, credentials by reference, an opt-in egress rule and health probes feeding the breakers
— but no candidate in the shipped policy uses it, so `cloud-burst` stays
`provider: simulated` and every response it serves is labelled `x-simulated`. The validator
enforces the three-way agreement (pool exists, compute and deployment match, backend equals
the pool endpoint) so the registry cannot quietly describe capacity the router would never
reach.

**External backends.** A candidate that leaves the cluster declares *which* key to use, not
the key:

```yaml
- name: cloud-prod
  compute: gpu
  deployment: cloud
  pool: cloud-prod
  backend: https://inference.provider.example
  upstream_model: model-name
  access:
    key: external-prod          # an entry in Secret/slm-backend-keys
    header: authorization       # default; must be a token, never a header injection
    prefix: "Bearer "           # default
```

The matching pool sets `external: true` with an `https` endpoint and no local workload or
node pool. Three things then have to line up, and the validator fails the render if any is
missing:

1. `Secret/slm-backend-keys` holds `keys: {external-prod: …}` and is mounted *optionally* at
   `/app/backend-keys`; the value never enters Git, the policy ConfigMap, an audit record or
   a log line. Errors name the key, not the secret.
2. `components/cloud-egress` opens `allow-gateway-cloud-egress`: gateway pods only, an
   `ipBlock` for the provider's public range, TCP 443. Replace the TEST-NET-2 placeholder
   with the provider's published range. The air-gap overlay rejects the component outright.
3. The gateway verifies TLS against the system trust store, or `SLM_BACKEND_CA` when the
   provider's chain is private. A configured-but-missing bundle stops the process instead
   of silently trusting public roots.

At runtime an external candidate whose key is absent is *considered and vetoed* with
`backend-unconfigured` — the audit record says so, and the rest of the policy keeps serving.
Every `SLM_PROBE_INTERVAL_S` (30 s) the gateway probes `GET /health` on each configured
external candidate and records the result in that alias/candidate's breaker; a 4xx counts as
reachable (host and certificate answered), a 5xx or transport error counts as down. A dead
provider opens its breaker before a tenant request discovers it. All of this is proven by
unit tests, including a throwaway HTTPS server with a self-signed certificate; no live
provider was called (N-12, R-18). The rationale is in
[ADR-0009](adr/ADR-0009-external-backends.md).

**Air gap.** Mirror the images with `deploy/openshift/airgap/mirror.sh`, apply the
cluster-scoped `IDMS`/`ITMS` objects with `kubectl apply -k deploy/openshift/airgap`, point
`deploy/gitops/apps/platform.yaml` at `overlays/airgap`, and seed `slm-model-cache` from the
mirrored weights before first start. The application manifests do not change.

**Limits, stated.** The vanilla overlay ran on a kind v0.33.0 cluster: 25 objects applied,
gateway and both simulators answering, Kyverno v1.19.2 denying two synthetic pods, and Argo
CD v3.6.1 self-healing a drifted policy. That pass also found a real defect static review
missed (a `subPath` mount nested inside a read-only ConfigMap mount), now fixed and checked
by the validator. What kind does not cover: OpenShift SCC/Route/ServiceMonitor, the
release PostSync hooks, the air-gap mirror, NetworkPolicy enforcement (kindnet does not
enforce it) and an ingress controller (the Ingress object has no backing controller). The
GPU tier could not schedule, so the platform Application stops at its wave-3 health gate by
design. Every workload image is pinned by digest: the three first-party images come from
public GHCR packages built by CI, and the upstream vLLM digest is recorded in the signed
manifest. No cluster has pulled the GHCR images yet, so their registry-to-kubelet path is
still unproven. PVCs rely on the cluster default storage class, and the gateway is
deliberately one replica. The full rationale is in
[ADR-0006](adr/ADR-0006-deployment-gitops.md); the observed rows are A-17 to A-23 in
the [acceptance pack](signoff/acceptance.md).

---

## Signed releases and the review pack

A release here is a manifest, not a git tag. `make release` writes
`release/slm-0.5.0.json` — canonical JSON that pins the base model revision, the digest of
every adapter file, the image digests, the policy version and hash, the gate artifact and
its verdict, the benchmark and cost evidence, and the commit the manifest was built from. It
refuses to build if the release gate did not pass.

```bash
make release-key   # once per environment: generate an Ed25519 key pair (private key gitignored)
make release       # build, sign, verify
make release-check # signed manifest = rendered overlays = compose pins
```

```text
$ make release-check
note vanilla: adapter promotion and the in-cluster gate are operator-run; the overlay deliberately carries no PostSync hooks
OK   release slm-0.5.0 = signed manifest = 3 overlay(s)
```

The check re-renders all three overlays and fails on drift: a changed policy file, a golden
set edited after the gate ran, a re-trained adapter, a stale gate artifact, a compose tag
that no longer matches the manifest. Each of those is a silent-release bug that a tag would
not catch, and each has a deliberate test.

```text
$ make release-verify
OK   release/slm-0.5.0.json: signature verified
release  slm-0.5.0 gate=PASS exact=0.833
```

Signing is Ed25519 over the canonical bytes, verified offline with the committed public
key, which is the reason it was chosen over cosign for air-gapped sites ([ADR-0007](adr/ADR-0007-signed-releases.md)).
It proves the manifest bytes were not altered and records the gate that authorised them.
It does **not** prove the weights match the revision, nor who signed: that needs the key in
an HSM/KMS and an attestation that the published image was built from this tree, both named
as conditions in the acceptance record.

The review pack answers the questions a reviewer actually asks, in order:

| Document | Question it answers |
|---|---|
| [requirements.md](signoff/requirements.md) | What was asked, requirement by requirement, and which ones are not met |
| [c4.md](architecture/c4.md) | What the system is: L1–L4 views, runtime sequence, trust boundaries |
| [acceptance.md](signoff/acceptance.md) | What was verified (A-01…A-25), what was not (N-01…N-11), and the conditions (C-01…C-10) |
| [threat-model.md](signoff/threat-model.md) | What can go wrong, by asset, actor and entry point |
| [risks.md](signoff/risks.md) | Which risks are accepted, with anchors into the code |
| [operations.md](signoff/operations.md) | How it runs: SLOs, alerts, runbooks, backups |
| [release-process.md](signoff/release-process.md) | Who builds, signs, promotes and rolls back |
| [evidence.json](signoff/evidence.json) | Generated: test counts, signature status and artifact hashes |

The verdict is **accepted as a reference platform with conditions** — not a production
acceptance. CPU and cloud throughput are modelled, OpenShift/Kyverno/Argo behaviour is
statically validated rather than observed, and there is no multi-replica gateway. Those are
stated at the top of the record rather than buried.

---

## Measured on this machine

Host: RTX 4080 Laptop GPU, 12 GB VRAM · Docker 29.4 · vLLM `v0.33.0`

| Metric | Value |
|---|---|
| Model | Qwen3-4B-Instruct-2507, AWQ W4A16 via compressed-tensors |
| Weights revision | `b37d1cc` (pinned SHA) |
| Context / max sequences | 8,192 tokens · 16 |
| KV cache | 5.93 GiB → 43,136 tokens → 5.3× concurrent 8k requests |
| VRAM in use | 9,975 / 12,282 MiB |
| VRAM with both LoRA adapters | 10,563 / 12,282 MiB |
| Engine init | 30.5 s (model load, profiling, CUDA graph capture) |
| Time to first token | 234 ms first request · **20 ms** median warm · 53 ms at 12 clients |
| Decode throughput | **125.5 tok/s** single stream · **1,123 tok/s** aggregate at 12 clients |
| Structured output | valid strict JSON on the extraction prompt |

```text
$ make smoke
endpoint  http://localhost:8010
models    ['slm-small']
ttft      0.243 s
decode    68.7 tok/s over 35 tokens
latency   0.753 s total
json      {"customer": "Acme", "product": "X200 pump", "sentiment": "negative"}
PASS
```

The 35-token run is slower than the 90-token median because fixed per-request overhead
dominates short generations; decode throughput is the metric that matters for tiering.
5.9 GiB of KV cache is deliberate headroom for multiple LoRA adapters in Phase 2.

*Note: numbers are from a laptop RTX 4080 (12 GB). They demonstrate the tier is real and
calibrate the cost model — they are not a capacity plan for datacentre GPUs.*

### Tier comparison through the gateway

Same prompt, 90-token budget, median of three streaming requests:

| Tier | TTFT | Total | Throughput | Cost / 1M output tokens |
|---|---|---|---|---|
| GPU (measured) | 33 ms | **0.74 s** | ~122 tok/s | not metered locally |
| CPU (MODELLED) | 93 ms | **5.77 s** | ~16 tok/s | $8.80 |
| GPU reference profile (MODELLED) | — | — | ~500 tok/s aggregate | $0.33 |

Queueing is real, not decorative: 12 concurrent requests against 8 CPU slots produced a
median of 1.32 s and a maximum of 2.61 s — the second half genuinely waited for a slot.

The 7.8x latency gap at identical output length is the routing problem in one table. It is
also why CPU is not a "cheap GPU": per output token it is roughly 25x more expensive here,
and wins only against an already-idle GPU, a latency-tolerant deadline, or no GPU at all.

### Failover is proven, not asserted

With the GPU container stopped, `slm-small` stayed at **200**. The first requests failed over
at the transport level (`x-fallback: true`); after three failures the breaker opened and the
router selected the standby directly. Residency decided *which* standby:

| Tenant | Region / class | Served by | Why |
|---|---|---|---|
| acme | eu / confidential | CPU tier | `cloud-us: region-veto`; EU data stays on an owned tier |
| globex | us / internal | Cloud tier (MODELLED) | US data, cloud allowed, cheapest admissible standby |

The GPU was then restarted, adapters were hot-loaded again and the breaker returned to
closed on the next successful request. The breaker state and the full admission trace are in
[Routing is a policy, not a table](#routing-is-a-policy-not-a-table).

---

## Gateway contract

Every response carries its routing decision:

| Header | Meaning |
|---|---|
| `x-request-id` | Correlates the audit record, gateway metrics and upstream logs |
| `x-route-alias` | The alias the client asked for |
| `x-tier` | `gpu`, `cpu` or `cloud` |
| `x-route-reason` | `policy:<primary\|canary\|standby>:<candidate>[:fallback]` |
| `x-policy-version` | Version of the policy file that decided |
| `x-fallback` | `true` when the chosen candidate failed and a later attempt served |
| `x-canary` | `true` when the served candidate came from the canary slice |
| `x-model-version` | The model version that actually answered |
| `x-shadow` | Candidate that received a shadow copy, when one was sampled |
| `x-simulated` | Present and `true` only for modelled tiers |

Working today: bearer auth with constant-time key comparison, per-tenant sliding-window
RPM limits (429 with `Retry-After`), admission and scoring per request, deterministic
canary, shadow sampling, per-candidate circuit breakers, unbuffered streaming pass-through,
usage accounting from upstream, hash-chained audit records, live policy reload, decision
explain and audit verification.

Deliberately still absent: a shared quota counter for multi-replica gateways (the limiter is
in-memory and explicitly single-replica), and quality-aware scoring that uses eval results
rather than static cost and priority. Pretending otherwise would be the kind of gap that
fails an architecture review.

---

## Routing is a policy, not a table

`config/policy.yaml` is versioned data. An alias is what a client asks for; each candidate
is a place the request can run: tier, backend, region, sovereignty, context budget,
priority, cost and model version. The router **admits before it scores**, so a cheap tier
never wins a request it is not entitled to serve.

Admission, in order:

1. the tenant's alias allowlist;
2. the tenant's tier allowlist;
3. cloud permission (`allow_cloud`);
4. region match against `serves_regions`;
5. data class vs sovereignty — `confidential` and `restricted` never leave owned tiers;
6. context budget — estimated prompt + `max_tokens` must fit `max_context`;
7. breaker state — an open candidate is not a route.

Only admissible candidates are considered. The primary wins by default; otherwise the
canary slice is applied; otherwise the best standby by
`priority*100 + cost_per_1k_out*1000 + failure_rate*50`. Every excluded candidate carries a
reason (`region-veto`, `cloud-denied`, `sovereignty-veto`, `context-exceeded`,
`breaker-open`, `alias-denied`, …) into the explain endpoint and the audit record.

### Canary and shadow

`slm-triage` keeps v1 as primary and sends v2 to a deterministic 50% slice:
`blake2b("canary:slm-triage", tenant-or-request) mod 100`. Sticky per tenant, so a tenant
does not flip versions between requests; no shared counter is needed and a gateway restart
does not reshuffle the assignment. Measured buckets here: **acme 67 (v1)**, **globex 37
(v2)**.

A shadow copy (`slm-triage` → v2, 5%, `max_tokens` capped at 64) is sent for primary
traffic and discarded; it is audited as a separate `shadow` record and counted by
`gateway_shadow_requests_total`. The header `x-shadow` names the target.

```bash
# promote v2 to 100%, then roll back with a file edit and a reload
curl -s -X POST http://localhost:8020/v1/policy/reload \
  -H 'Authorization: Bearer dev-acme-key'
```

Reload is atomic: an invalid YAML file returns **422** and the previous policy keeps
serving. Measured rollback: setting the canary to 0% moved globex from
`policy:canary:v2` to `policy:primary:v1` on the next request, with no restart.

### Circuit breakers

Health is tracked per `alias:candidate`, not per backend, so one alias's failure does not
blind another. Three failures inside 60 s open the breaker for 20 s; one probe is admitted
in half-open; success closes it, failure reopens it. Connection errors, timeouts and 5xx
responses count as failures; 4xx does not. When the chosen candidate fails, the gateway
walks the ordered attempt list built from the same admission pass — never an inadmissible
candidate.

### Measured outage drill

With the GPU container stopped:

| Request | Tenant | Tier | `x-route-reason` | `x-fallback` |
|---|---|---|---|---|
| 1–3 | acme (eu) | cpu | `policy:primary:cpu:fallback` | `true` |
| 4 | acme (eu) | cpu | `policy:standby:cpu` | `false` |
| 1–4 | globex (us) | cloud | `policy:standby:cloud-us` | `false` |

The breaker is shared by alias, so acme's three failures opened `slm-small:gpu` for globex
too. The audit record for acme carries `vetoed: ["cloud-us:region-veto"]`; globex has no
such veto and the cloud candidate wins on score. This is the difference between a fallback
chain and a policy: the fallback for EU confidential traffic is the CPU tier, even though
the cloud tier is cheaper and faster.

Recovery is equally boring: start the GPU, hot-load the adapters, send one request. The
half-open probe succeeds and the breaker returns to closed.

### The audit trail

`logs/audit.jsonl` gets one record per inference and one per shadow request. Each record
commits to its predecessor:

```text
hash = sha256(canonical_json(record without hash))
```

so editing, removing or reordering a record breaks verification from that point on. The
gateway recovers the head on startup, drops a torn final line (crash between write and
newline), and refuses to start if anything earlier fails verification. `GET
/v1/audit/verify` recomputes the chain:

```json
{"ok": true, "records": 65, "last_seq": 65,
 "last_hash": "cb8e9565f625e4fee35272587afe0e4fc71aa853b7a023318128170bbd82d3dc"}
```

This is tamper **evidence**, not prevention: a writer with filesystem access can rewrite the
whole chain. Production must ship the head hash to a separate sink with object-lock or
append-only retention. Audit writes are fsync'd per record; a disk error is logged loudly
and does not fail the request, which is a deliberate availability-over-evidence trade-off
for this reference build, not a compliance posture.

### Policy endpoints

| Endpoint | Role | Purpose |
|---|---|---|
| `GET /v1/policy` | admin | Active policy, breakers and their state |
| `POST /v1/policy/explain` | tenant | Full admission trace for a hypothetical request |
| `POST /v1/policy/reload` | admin | Validate and swap the file; 422 keeps the old policy |
| `GET /v1/audit/verify` | admin | Recompute the hash chain |

```text
$ make smoke-policy
health    4 aliases, policy 2026-11-04.1
policy    4 aliases, breaker {'failures': 3, 'window_s': 60.0, 'open_s': 20.0}
admission acme/eu   chosen=gpu vetoed={'cloud-us': 'region-veto'}
admission globex    refused=alias-denied
canary    globex bucket=37 percent=50 decision=canary chosen=v2
audit     ok, 65 records, head cb8e9565f625e4f...
auth      non-admin policy read is 403
PASS
```

New metrics: `gateway_route_decisions_total`, `gateway_policy_refusals_total`,
`gateway_canary_requests_total`, `gateway_shadow_requests_total`,
`gateway_breaker_state` (0 closed, 1 half-open, 2 open), `gateway_policy_reloads_total`.

The full design rationale is in [ADR-0004](adr/ADR-0004-policy-router.md).

---

## Fine-tuning: two adapters, one contract

The narrow task is **support-ticket triage** for an industrial tenant. The model must emit
strict JSON against a fixed taxonomy and a severity rubric — exactly the kind of contract a
general model drifts on:

```json
{"category": "technical", "priority": "P2", "sentiment": "negative",
 "escalate": true, "product": "X200 pump", "summary": "Line stopped at the north plant"}
```

`finetune/prompt.py` holds the system prompt, schema and rubric that training, evaluation
and callers share. Violating the schema is a bug; guessing a different taxonomy is a bigger
one.

**Data is generated, not scraped.** `finetune/dataset.py` builds 25 clean scenarios and 18
adversarial ones (sarcasm, conflicting urgency, multi-issue, missing model number, typos,
mixed language, angry-but-trivial) with labels from the scenario definition. Splits:

| Split | Rows | Content |
|---|---|---|
| `train-v1` | 320 | clean scenarios, first two paraphrases |
| `train-v2` | 336 | 240 clean + 96 adversarial |
| `eval` | 200 | 80 clean + 80 adversarial paraphrases + 40 tickets from four families never trained on |

**Training:** QLoRA (4-bit NF4, bf16 compute) on the official BF16 base
(`Qwen/Qwen3-4B-Instruct-2507`, pinned SHA), LoRA r=16 / α=32 / dropout 0.05 on all linear
layers. 33.0 M trainable parameters (0.81%). One adapter takes 320–336 steps, 13–15 minutes
on the RTX 4080, with the GPU tier stopped.

**Serving:** the adapter runs on the **AWQ base** via multi-LoRA, not a merged checkpoint.
This was verified before training: a probe adapter loaded onto the compressed-tensors AWQ
model and served under its own name, which is why `--enable-lora` is safe here. See
[ADR-0003](adr/ADR-0003-adapters.md).

### Held-out results

200 tickets, temperature 0. "Exact" means category, priority, sentiment, escalate and
product all correct; strict JSON is `json.loads` of the raw answer with no trimming.

| Model | Strict JSON | Schema | Exact labels | Category | Priority | Sentiment | Escalate | Product | Clean | Hard | Held-out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| base `slm-small` | 100% | 99.0% | **19.5%** | 80.0% | 70.0% | 81.0% | 76.0% | 38.0% | 23.7% | 15.0% | 20.0% |
| adapter v1 | 100% | 99.0% | **73.5%** | 98.5% | 85.0% | 86.5% | 93.0% | 96.0% | 77.5% | 57.5% | 97.5% |
| adapter v2 | 100% | 100% | **79.0%** | 97.0% | 100% | 86.0% | 93.0% | 97.0% | 85.0% | 76.2% | 72.5% |

- The base model writes valid JSON but invents taxonomy (`"category": "security"`,
  `"feature request"`) and misses product extraction 62% of the time. Fine-tuning buys the
  contract, not just the syntax.
- v1 is clean-data-only: strong on easy tickets, **57.5%** on adversarial input, and it
  reads sarcasm as positive sentiment.
- v2 is trained on clean + adversarial data: **76.2%** on the hard slice, 100% schema, and
  it handles the sarcasm case correctly. Its held-out dip is concentrated in two paraphrases
  whose generated labels are genuinely arguable, not in structure.
- Training loss fell below `1e-5` within two epochs in both runs, i.e. it memorised the
  templates. Promotion is therefore decided by the held-out eval, never by loss.

```text
$ make adapter-smoke
gateway   http://localhost:8020
models    ['slm-small', 'slm-small-cpu', 'slm-triage', 'slm-triage-v2']
sarcasm         base=P2/positive v1=P2/positive v2=P2/negative
feature-request base=P4/positive v1=P4/positive v2=P4/positive
calm-outage     base=P2/neutral v1=P2/negative v2=P2/neutral
PASS
```

### Promotion and rollback without a redeploy

Adapters are loaded through vLLM's control API and addressed as model names:

```bash
curl -s -X POST http://localhost:8010/v1/load_lora_adapter \
  -H 'Content-Type: application/json' \
  -d '{"lora_name":"triage-v2","lora_path":"/adapters/triage-v2"}'

curl -s -X POST http://localhost:8010/v1/unload_lora_adapter \
  -H 'Content-Type: application/json' -d '{"lora_name":"triage-v2"}'
```

Rollback was exercised end to end: after unload, `slm-triage-v2` returns **404** while
`slm-triage` keeps serving; reload restores **200**. Both adapters together add ~590 MiB of
VRAM (9,975 → 10,563 of 12,282 MiB). Adapter weights are build artifacts, git-ignored and
reproduced by `make adapter-train`; `training.json` records data, base revision,
hyper-parameters and the loss curve.

---

## Benchmarks, release gates and cost

Phase 3 made every routing decision explainable. Phase 4 makes the three claims a reviewer
would otherwise have to take on faith measurable: **how fast the tier is under load**, **whether
a release is good enough to promote**, and **what the owned GPU costs per token**.

### Benchmark method

Closed-loop, fixed concurrency: each worker holds one request in flight, so `c=12` means
twelve simultaneous clients, not a batch size. Two warm-up requests per level, then 20
measured requests (8 for the CPU profile), streaming with usage. Percentiles are nearest-rank,
so a p95 is an observed request, not an interpolation. Every run records its concurrency and
its source: a response carrying `x-simulated: true` is labelled **MODELLED** from the
response, not from the config. Same prompt, 64 output tokens.

```text
$ make bench
| Target | Source | c | Req/s | Output tok/s | TTFT p50 | Total p50 | Errors |
|---|---|---:|---:|---:|---:|---:|---:|
| gpu-direct c=1 | measured | 1 | 1.96 | 125.5 | 20 ms | 509 ms | 0 |
| gpu-direct c=4 | measured | 4 | 7.58 | 484.8 | 27 ms | 524 ms | 0 |
| gpu-direct c=12 | measured | 12 | 17.55 | 1123.2 | 53 ms | 569 ms | 0 |
| adapter-v2 c=1 | measured | 1 | 1.75 | 101.6 | 28 ms | 570 ms | 0 |
| adapter-v2 c=12 | measured | 12 | 14.67 | 850.9 | 128 ms | 683 ms | 0 |
| cloud-sim c=12 | MODELLED | 12 | 12.32 | 788.4 | 30 ms | 810 ms | 0 |
| cpu-sim c=12 | MODELLED | 12 | 1.91 | 122.2 | 121 ms | 4185 ms | 0 |
```

Reading it: the GPU scales **9x** from one to twelve clients while p50 total latency moves
only 509 → 569 ms. The adapter costs 15–25% of aggregate throughput, which is the price of
the contract it enforces. The CPU profile saturates at ~122 tok/s — it is a deadline tier,
not a cheap GPU — and its 4.2 s total latency is the routing problem in one number. The
simulated cloud tier is modelled, not measured; it exists so residency rules can veto or
permit a real third candidate.

### The release gate

A benchmark says how fast a model is; it says nothing about whether its labels are right.
The gate scores an alias against a **hand-written 24-ticket golden set** — eight clean,
eight adversarial and eight held-out tickets, new wording, labels taken from the rubric in
`finetune/prompt.py`, not from a model. It then applies absolute floors and drift against a
recorded baseline. Both must pass.

| Model | Strict JSON | Schema | Exact | Clean | Hard | Held-out | Gate |
|---|---:|---:|---:|---:|---:|---:|---|
| base `slm-small` | 100% | 100% | **4.2%** | 0.0% | 0.0% | 12.5% | fails floors |
| adapter v1 | 100% | 91.7% | **75.0%** | 100% | 62.5% | 62.5% | fails schema floor |
| adapter v2 | 100% | 100% | **83.3%** | 87.5% | 100% | 62.5% | **PASS** |

```text
$ make eval-gate
gate slm-triage-v2: PASS
  ok   exact_labels     actual=0.833 limit=0.7
  ok   slice:held-out   actual=0.625 limit=0.5
  ok   drift:exact_labels actual=0.833 limit=0.783  baseline 83.3%
```

v2's four remaining disagreements are real label errors, not schema errors: a thank-you read
as P3, a refund chase where the word "escalate" pulled `escalate` true, a licence transfer
read as cancellation, and a two-site outage read as P2 instead of P1. They are recorded with
the answer the model gave, so a reviewer can disagree with the label rather than guess at it.
`make eval-baseline` records the current release; `make eval-gate` then fails on a drop of
more than 5 points in exact labels or 10 points in any field.

### What the GPU actually costs

The policy prices CPU and cloud per token because those tiers send a bill. The owned GPU has
`cost_per_1k_out: 0.0` because its cost is capacity that was bought. `src/cost` converts
ownership into the same unit: capex over 36 months, 180 W at PUE 1.4 and $0.18/kWh, 15% ops,
and the **measured aggregate throughput** from `bench/results/tier-comparison.json`.

| Tier | Source | $/1M output (capacity) | $/1M at 20k/day | $/request | $/month | p50 total / TTFT |
|---|---|---:|---:|---:|---:|---:|
| owned GPU, 35% util | measured | **$0.117** | **$2.243** | $0.000202 | **$121.11** | 509 / 20 ms |
| cloud L4 profile | MODELLED | $0.797 | $0.797 | $0.000072 | $43.02 | 785 / 25 ms |
| CPU Xeon profile | MODELLED | $15.80 | $15.80 | $0.001422 | $853.20 | 4141 / 116 ms |

The distinction between the two GPU columns is the point: at 35% utilisation and full
capacity the node is cheap per token, but a 20k-request/day workload uses only about 2% of
its capacity, so the fixed bill is spread over few tokens. Break-even against the cloud
profile is **56,069 requests/day** (2,839 against CPU), inside the node's ~1.08M
request/day full capacity. Below that volume, cloud is cheaper; above it, owning the GPU
wins — and the model states the assumptions rather than hiding them.

Full methodology and limits are in [ADR-0005](adr/ADR-0005-benchmarks-eval-cost.md).

---

## Latency budget and cost

Phase 3 admitted on who may be served; Stage 4 admits on how slow and how expensive that
service may be. Latency and cost were already measured — they simply never reached a
decision. Now each pool declares its SLO once, every candidate restates it, and a tenant may
refuse any route that cannot meet its deadline.

**Declared once, per pool.** `config/capacity.yaml` (version 3) carries an `slo` block:

| Pool | `expected_latency_ms` | `cost_per_1k_out` | `cost_source` |
|---|---:|---:|---|
| `onprem-gpu` | 900 | 0.0 | `measured` |
| `onprem-cpu` | 2500 | 0.0088 | `modelled` |
| `cloud-burst` | 1500 | 0.00033 | `modelled` |

`expected_latency_ms` is the p95 the pool is sized for. `cost_source` says whether hardware
of that class was actually benchmarked: the GPU numbers come from the committed benchmark
runs, the CPU and cloud numbers from the calibrated profiles in `src/tiersim/profiles.py`,
which is why a `modelled: true` pool is not allowed to claim `measured`.

**Restated per candidate, checked by the validator.** Each candidate in `config/policy.yaml`
declares the same `latency_ms` it inherits from its pool. The deploy gate compares the two:
latency must match exactly, `cost_per_1k_out` within 1e-9, and the error names the alias,
the candidate, the pool and both values. Drift is a failed render, not a routing surprise.

```yaml
# config/capacity.yaml           # config/policy.yaml
pools:                          aliases:
  - name: onprem-gpu              slm-small:
    slo:                            candidates:
      expected_latency_ms: 900        - name: gpu
      cost_per_1k_out: 0.0              pool: onprem-gpu
      cost_source: measured             latency_ms: 900
                                        cost_per_1k_out: 0.0
```

**A tenant budget vetoes, it does not rank.** `latency_budget_ms` in the tenant file
(`0` = no budget) adds one admission rule, `latency-budget-exceeded`, after
`context-exceeded` and before the breaker check. It fires only when the budget is positive,
the candidate declares a positive latency, and that latency is larger than the budget. A
candidate that declares nothing is never vetoed on latency, so a hand-written test policy
keeps working. The reason lands in `vetoed` on the audit record, and the audit record now
carries `latency_budget_ms` beside the decision fields:

```json
{"alias": "slm-small", "candidate": "cpu", "decision": "primary",
 "latency_budget_ms": 1000, "vetoed": ["cloud-us:region-veto",
 "cpu:latency-budget-exceeded"]}
```

```yaml
# config/tenants.example.yaml — globex admits GPU and cloud, refuses the CPU tier
- id: globex
  latency_budget_ms: 2000
```

Admission now reads, in order: alias allowlist, tier, compute, deployment, `allow_cloud`,
external backend readiness, region, sovereignty, context budget, **latency budget**, breaker
state. The position of the new rule is deliberate: when several would reject the same
candidate, the recorded reason is the one from the earlier rule, which makes a decision
reproducible from the policy file alone.

**What the veto is comparing.** A declared target against a declared budget — not the p95 of
the request in flight. The GPU's 900 ms is a target calibrated from the benchmark table
(p50 509 ms at one client, 569 ms at twelve), the CPU and cloud figures are modelled, and no
live latency feeds back into either. A pool that is currently slower than its own SLO is
caught by its breaker and by `SlmGpuTierUnavailable`, not by this veto. Cost is equally
declared: the GPU marginal cost is 0.0 because the node is owned, and ADR-0005 prices that
ownership per token separately.

```bash
# what a budget would do, without spending a request
curl -s -X POST http://localhost:8020/v1/policy/explain \
  -H 'Authorization: Bearer dev-acme-key' \
  -d '{"model": "slm-small", "prompt_tokens": 128, "max_tokens": 128}'
```

The rationale and the limits — modelled figures, no live p95, a stale release manifest until
it is re-signed — are in [ADR-0011](adr/ADR-0011-latency-and-cost-admission.md).

---

## Repository map

```
compose.yaml                     the whole platform: tiers, gateway, observability
Makefile                         operational entry points
config/policy.yaml               versioned routing policy: aliases, candidates, canary
config/bench.yaml                benchmark targets, concurrency levels and prompt
config/eval-gates.yaml           release floors and drift allowances
config/cost.yaml                 ownership assumptions: capex, power, utilisation, workload
config/release.yaml              release identity: model pin, images, adapters, gate, evidence
config/capacity.yaml             pool registry: compute/deployment, endpoints, bounds, triggers, SLO, node contract
config/tenants.example.yaml      tenant keys, quotas, region, data class, latency budget; tenants.yaml is gitignored
scripts/smoke.py                 GPU tier smoke test
scripts/smoke_gateway.py         end-to-end test through the gateway, all tiers
scripts/smoke_policy.py          admission, residency, canary stability, audit chain
scripts/smoke_adapters.py        adapter aliases and contract check through the gateway
scripts/load_adapters.py         runtime load of every adapter into vLLM
tests/                           router, eval, cost, deployment and release unit tests (stdlib unittest)
.github/workflows/ci.yml         tests, deployment validator and release chain on every push
docker/                          gateway, simulator, QLoRA training and eval images
src/gateway/                     OpenAI-compatible API, auth, quotas, proxy, policy use, audit
src/gateway/backends.py          external backend keys, TLS verification, upstream calls, probes
src/router/                      policy model, admission, canary, breakers, hash-chained audit
src/tiersim/                     calibrated CPU / cloud tier simulator
src/registry/                    signed release manifests: canonical JSON, Ed25519, drift checks
src/bench/                       closed-loop load generator and report rendering
src/eval/                        golden-set scoring, floors and drift gate
src/cost/                        ownership TCO, utilisation sweep and break-even
deploy/observability/grafana/    provisioned datasource and dashboard
deploy/kustomize/                base, openshift/vanilla/airgap overlays, shared components
deploy/kustomize/components/cloud-egress/  opt-in TCP 443 egress to one provider range
deploy/openshift/airgap/         IDMS/ITMS, mirrored image list and mirror script
deploy/policies/                 Kyverno ClusterPolicies enforced at admission
deploy/gitops/                   Argo CD project, app-of-apps and child Applications
deploy/cloud/                    EKS/AKS/GKE/on-prem node-pool contracts (applied by the cluster owner)
release/                         signed release manifest, detached signature and public key
tools/validate_deploy.py         static checks over the rendered overlays
tools/validate_release.py        release chain: manifest = overlays = compose, signature
tools/build_signoff.py           runs tests, verifies the signature, writes the evidence record
finetune/prompt.py               task contract: system prompt, schema, taxonomy
finetune/dataset.py              deterministic triage dataset generator
finetune/train_qlora.py          QLoRA trainer (4-bit NF4, assistant-only loss)
finetune/evaluate.py             strict JSON / schema / per-field evaluation
finetune/data/                   generated train-v1, train-v2 and eval splits
adapters/                        trained adapter artifacts (gitignored, reproducible)
bench/results/                   recorded benchmarks, eval gate and cost artifacts
docs/adr/                        architecture decision records
docs/architecture/c4.md          C4 context, container, component and deployment views
docs/signoff/                    requirements, acceptance, threat model, risks, operations, evidence
```
