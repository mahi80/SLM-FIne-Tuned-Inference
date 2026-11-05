# Acceptance checklist and conditions

This is the reviewer's sheet. Each row is a claim, the command that tests it, and what a
passing run looked like in this environment. Rows marked *not verified* are not failures of
the design; they are limits of the environment the design was built in.

## Verified in this environment

Host: RTX 4080 Laptop 12 GiB, Docker 29.4, vLLM v0.33.0, Windows 11 + Git Bash,
Python 3.12, OpenSSL 3.5, `kubectl` v1.34.1, kind v0.33.0 on Docker Desktop.

| # | Claim | Command | Observed | Class |
|---|---|---|---|---|
| A-01 | GPU tier serves the pinned AWQ model with valid JSON | `make smoke` | TTFT 0.24 s cold, decode 68.7 tok/s short run, strict JSON | Measured |
| A-02 | All three tiers answer through one OpenAI-compatible contract | `make smoke-gateway` | `x-tier` = gpu / cpu / cloud; `x-simulated` only on modelled tiers | Measured / MODELLED |
| A-03 | Admission runs before scoring and vetoes are explicit | `make smoke-policy` | EU tenant routed to CPU with `cloud-us: region-veto`; US tenant permitted cloud | Measured |
| A-04 | Canary assignment is deterministic and sticky per tenant | `make smoke-policy` | acme bucket 67 → v1, globex bucket 37 → v2, identical across restarts | Measured |
| A-05 | Audit chain detects tampering and recovers a torn tail | `make test`, `make smoke-policy` | chain verified over 65+ records; torn 14-byte tail dropped on start | Measured |
| A-06 | GPU outage fails over without dropping requests | manual drill in README | first 3 requests `x-fallback: true`, then breaker opens and standby is selected | Measured |
| A-07 | Fine-tuned adapter meets the release gate | `make eval-gate` | v2: schema 100%, exact 83.3%, held-out 62.5%, 13 checks pass | Measured |
| A-08 | Tier throughput and latency are measured, not estimated | `make bench` | GPU 125.5 / 484.8 / 1,123.2 output tok/s at c=1/4/12 | Measured |
| A-09 | CPU and cloud tiers are calibrated simulators | `make bench` | CPU saturates ~122 tok/s; every response `x-simulated: true` | MODELLED |
| A-10 | Ownership cost is separated from per-token price | `make cost` | $0.117/1M at capacity, $2.24/1M at 20k req/day, break-even ≈ 56k/day | Measured throughput, stated prices |
| A-11 | Router, eval, cost, deployment and release logic are unit tested | `make test` | 362 tests, 0 failures, stdlib `unittest` | Measured |
| A-12 | Three overlays render and pass static checks | `make deploy-validate` | openshift 35, vanilla 25, airgap 34 objects; no errors | Static |
| A-13 | A signed release matches the deployment tree | `make release-check` | manifest signature verified; policy, image names, tags and digests, revision, adapter set and gate all agree | Measured |
| A-14 | Secrets are not in Git; workloads are non-root with limits and probes | `make deploy-validate`, `tests/test_deploy.py` | no `Secret` objects rendered; 82 deployment tests pass | Static |
| A-15 | Air gap removes internet egress and forces offline mode | `make deploy-validate` | model-download NetworkPolicy absent; `HF_HUB_OFFLINE=1` present | Static |
| A-16 | CI runs the tests and both validators on a hosted runner | GitHub Actions run `41955890300` on `b1bd4a0` | unit tests, `validate_deploy`, `validate_release`, `registry verify` and `fleet_status --check` all passed | Measured (hosted), no cluster |
| A-17 | The vanilla overlay runs on a real API server and kubelet | `kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/kustomize/overlays/vanilla \| kubectl apply -f -`; kind v0.33.0, Kubernetes 1.34 | 25 objects applied; gateway and both tier simulators 1/1; GPU tier left unschedulable by a node taint; the first apply exposed a nested `subPath` mount on the tenants Secret, now fixed and covered by `check_mounts` | Measured on kind |
| A-18 | Live routing follows residency and cloud permission through the cluster Service | `curl` to `svc/gateway` with tenant keys (port-forward) | acme (EU, confidential) → `x-tier: cpu`, `x-route-reason: policy:primary:cpu:fallback`, `x-fallback: true`; globex (US, internal) → `x-tier: cloud`, `policy:standby:cloud-us`; audit records `vetoed: ["gpu:breaker-open"]` | Measured on kind |
| A-19 | The audit chain verifies against the PVC-backed file | `GET /v1/audit/verify` as admin | `{"ok": true, "records": 7, "last_seq": 7, ...}`; the file's last record hash equals the in-memory head | Measured on kind |
| A-20 | Kyverno Enforce rejects non-compliant pods and admits a compliant one | `kubectl apply` of four synthetic pods | missing readiness probe → `slm-require-probes`; `nginx:latest` → `slm-require-pinned-images`; PSS-compliant pod with probe admitted | Measured on kind (Kyverno v1.19.2) |
| A-21 | Argo CD reconciles Applications and self-heals drift | temp Application at the vanilla overlay; patch and delete a managed ClusterPolicy | `slm-policies` Synced/Healthy/Succeeded over 6 policies; a drifted field restored in ~6 s and a deleted policy recreated in <3 s; `slm-platform` applied waves 0–2 and waits on the unschedulable GPU tier | Measured on kind (Argo CD v3.6.1) |
| A-22 | Every workload image is pinned by digest and the signed release records the same digests | `kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/kustomize/overlays/openshift \| grep image:`; images workflow run `41952187982`; `make release-check` | all four images render as `name:tag@sha256:…`; CI published the three first-party images to public GHCR packages and the manifest pins the same digests (gateway `ebd974fe…`, tiersim `ef8494fd…`, eval `680f4896…`) plus upstream vLLM `d825baf9…`; anonymous registry lookups returned those digests; a tag-only or mismatched image fails `check_images`/`check_rendered` | Measured (hosted CI + registry); no cluster pulled the GHCR images |
| A-23 | CPU/GPU placement, platform alerts and the live health checker are enforced | `make deploy-validate`, `tests/test_deploy.py`, `tests/test_cluster_health.py`; `make cluster-health` without a kubeconfig | every overlay pins vLLM to GPU nodes and gives CPU workloads a preferred CPU pool plus tolerations; the five platform alerts are required on operator overlays; the suite passes; the health checker reports `FAIL cluster` and exits 1 when no API server answers — no cluster was available to observe a healthy run | Static + local failure path |
| A-24 | GPU scaling policy is declared once and enforced on the operator overlays | `make deploy-validate`, `tests/test_deploy.py`; `config/capacity.yaml` | the KEDA `ScaledObject` matches capacity bounds 1–3 and both trigger thresholds; `spec.replicas` is removed from `Deployment/vllm-gpu` and ignored by Argo; both claims render `ReadWriteMany`; every declared adapter is preloaded per replica; a scaler aimed at the gateway fails validation — no KEDA cluster was available to observe a scale event | Static |
| A-25 | Capacity is a registry, and the router cannot route to undeclared capacity | `make deploy-validate`, `tests/test_deploy.py`, `tests/test_router.py`; `config/capacity.yaml` | three pools declare compute, deployment, endpoint, replica bounds and the node-pool label/taint; every policy candidate resolves to a pool whose compute, deployment and backend match; tenant `allowed_compute`/`allowed_deployment` veto before scoring; the AWS, Azure, GCP and on-prem snippets parse and carry the same label and taint — no cloud pool was created | Static |
| A-26 | An external backend fails closed on key, TLS and egress | `tests/test_backends.py`, `tests/test_router.py`, `tests/test_deploy.py`, `make deploy-validate` | an external candidate needs `access` naming a key from the optional `slm-backend-keys` mount; a missing key is vetoed as `backend-unconfigured`; HTTPS is mandatory and userinfo/query are rejected; header injection is rejected; probes against a throwaway TLS server treat 2xx/4xx as reachable and 5xx, a bad certificate or a refused connection as down; the egress rule rejects `0.0.0.0/0`, private CIDRs and non-443 ports, and the validator rejects the component without a candidate and a candidate without the component; no live provider was called (N-12) | Static + local TLS |
| A-27 | Backend credentials can rotate without a restart, and OIDC exchanges stay server-side | `tests/test_backends.py`, `tests/test_router.py`, `tests/test_check_backend.py`, `make deploy-validate` | `access.source` is static, file or oidc; source-specific fields are rejected on the wrong source; a file token is read per request, so a rotated projected token is used immediately; an OIDC client secret is exchanged at an https token endpoint, cached per process until just before expiry, and refreshed once after a 401; `CredentialError` names the key and a status only, never a value; `tools/check_backend.py` reports configured/probe state and exits 1 without printing a secret; the three copy-ready examples pass the same validator as shipped config — no IdP or provider was contacted (N-13) | Static + mocked transport |
| A-28 | Latency and cost are declared once per pool and admitted on | `make deploy-validate`, `tests/test_router.py`, `tests/test_deploy.py`, `tests/test_tenants.py`; `config/capacity.yaml` | every pool declares `slo.expected_latency_ms`, `cost_per_1k_out` and `cost_source`; every candidate repeats the latency exactly and the cost within 1e-9, and a `modelled: true` pool cannot claim `measured`; a tenant `latency_budget_ms` vetoes a candidate whose declared latency exceeds it, with reason `latency-budget-exceeded`, and is recorded in the audit; zero budget or undeclared latency admits as before — the comparison is a declared target, not a live p95 (N-05) | Static + unit |
| A-29 | Every pool is hosted by exactly one declared cluster | `make fleet-check`, `tests/test_fleet.py`, `config/fleet.yaml` | the inventory names one primary cluster and hosts all three pools; duplicate names or contexts, a missing or second primary, an unhosted or double-hosted pool, a policy candidate whose pool is unhosted, and a non-https gateway URL all fail; `--live` is read-only and isolates per-cluster failures — no second cluster exists (N-13) | Static + injected runner |
| A-30 | Cluster bring-up is a plan a reviewer can read, and every provider call is reachable under `--dry-run` without a credential | `tests/test_cloud_bringup.py`; `bash deploy/cloud/{aws-eks,azure-aks,gcp-gke}-cluster.sh --dry-run`; `bash deploy/cloud/bootstrap-addons.sh --dry-run` | 13 tests: all four scripts run under `set -euo pipefail` with no provider CLI installed; each prints its cluster call, the delegated node-pool call and the add-on installs; the AWS controller policy grants `iam:PassRole` only to `KarpenterNodeRole-*` for `ec2.amazonaws.com` and contains no `*` action; AKS enables the OIDC issuer and workload identity and takes a revocable kubeconfig (no `--admin`), GKE binds a workload pool and enables shielded nodes; four charts are pinned with `--version` and waited on; the driver flag is true on AKS and false on EKS/GKE; no credential shape (access-key id, private key, password, client secret) appears anywhere in `deploy/cloud/` — no account was contacted and no cluster exists (N-14, R-17) | Static (dry-run); no account |

Reproducible artifacts for each row are under [`bench/results/`](../../bench/results) and
the test suite; [evidence.json](evidence.json) records their hashes and the commit.

## Not verified in this environment

| # | Gap | Why | Consequence | Risk |
|---|---|---|---|---|
| N-01 | No OpenShift, EKS, AKS or GKE cluster; kind covers vanilla only | None available | Route, SCC and ServiceMonitor are unexercised; managed control planes untested | [R-09](risks.md#r-09--no-live-openshift-validation) |
| N-02 | The Kyverno policies still use the deprecated `kyverno.io/v1` `ClusterPolicy` kind | v1.19 warns on every apply; the CEL `policies.kyverno.io` kinds are the forward path | A future Kyverno release removes the v1 kinds and admission breaks until migrated | R-09 |
| N-03 | Argo CD PostSync hooks and wave 4 (`gateway`) | kind has no GPU capacity, so `vllm-gpu` never becomes healthy and the platform Application stops at wave 3 | Hook ordering, the adapter loader and the in-cluster gate are read, not observed | R-11 |
| N-04 | Air-gapped installation | No disconnected site | Mirror list and IDMS/ITMS are unproven end to end | R-09 |
| N-05 | Xeon / EPYC CPU tier | No server hardware | CPU latency, throughput and cost are MODELLED | [R-10](risks.md#r-10--modelled-cpu-and-cloud-numbers) |
| N-06 | TensorRT-LLM, Triton, Dynamo, SGLang | No datacentre GPU | Only vLLM was served | [R-15](risks.md#r-15--single-runtime) |
| N-07 | Closed-model comparison | Cost, data-egress and reproducibility | The thesis "most of frontier quality" is untested | [R-12](risks.md#r-12--evaluation-set-is-small-and-synthetic) |
| N-08 | Production load and HA | One host, one GPU, one gateway replica | Quotas, breakers and audit assume one writer | [R-08](risks.md#r-08--gateway-is-a-single-writer) |
| N-09 | Security testing beyond unit tests | No pentest, fuzzing or dependency scanning in CI yet | Unknown classes of input/API defects | [R-05](risks.md#r-05--no-in-cluster-mtls), [R-16](risks.md#r-16--no-dependency-or-image-scanning) |
| N-10 | Audit sink durability under storage failure | Single node | A failed audit write does not fail the request | [R-01](risks.md#r-01--audit-chain-is-evidence-not-prevention) |
| N-11 | NetworkPolicy and Ingress enforcement | kind's kindnet does not enforce NetworkPolicy and ships no ingress controller | default-deny, the model-download egress rule and the Ingress path are declarative only on kind | [R-09](risks.md#r-09--no-live-openshift-validation) |
| N-12 | No live external (cloud) inference provider | None configured; no key or endpoint was available | Provider-side auth, DNS, TLS rotation, egress enforcement and remote health are unobserved; the path is proven only against a local TLS server | [R-18](risks.md#r-18--external-backend-path-is-unproven-against-a-provider) |
| N-13 | No live IdP and no second cluster | No registered OIDC client and no second kube context | The client-credentials exchange, token cache, 401 retry and fleet live view are proven against mocked transports and an injected runner; a token endpoint's real behaviour and a standby cluster's capacity are unobserved | R-18, [R-19](risks.md#r-19--fleet-inventory-has-no-second-cluster) |
| N-14 | No cloud account, credential or GPU quota | No AWS, Azure or GCP account was available, and quota cannot be requested for a reference build | Cluster creation, IAM propagation, Karpenter AMI resolution, managed autoscaler behaviour and GPU scheduling are unobserved; `--dry-run` proves the command shape, not that a provider accepts it | [R-17](risks.md#r-17--cloud-pools-are-declared-but-not-reachable) |

## Conditions

Production use for a regulated tenant requires these to be closed and evidenced. Each is a
risk accepted for the reference build, not an oversight.

| # | Condition | Closes | Evidence required |
|---|---|---|---|
| C-01 | Install on a real OpenShift cluster: Kyverno in Enforce, Argo CD synced, Route TLS verified, ServiceMonitor scraping | R-09, N-01–N-04 | `oc get route`, admission denial tests, Argo sync history, Grafana panels populated, `make cluster-health` all PASS with both node pools |
| C-02 | ~~Pin images by digest, not tag, in manifests and the release manifest~~ **Closed** | R-02 | A-22: rendered images carry `@sha256:`; the signed manifest records the same digests; the validator rejects tag-only references |
| C-03 | Replace static bearer keys with OIDC / short-lived credentials; rotate the release key | R-04 | Tenant auth failure and rotation rehearsal recorded |
| C-04 | Ship the audit head hash to a WORM or append-only sink outside the cluster | R-01 | Object-lock or equivalent retention policy; restore test |
| C-05 | Move quota, breaker and audit state to a shared store, or accept single-replica and document the maintenance window | R-08 | Replica-2 test proving no quota bypass and no chain fork |
| C-06 | Benchmark on a real Xeon/EPYC node and replace the simulator | R-10 | Measured CPU artifact with `source: measured` |
| C-07 | Compare against at least one closed model on tenant-representative data, within the tenant's egress policy | R-12 | Evaluation report under the same golden contract |
| C-08 | Automatic rollback on failed in-cluster gate (or a documented manual SLA) | R-11 | Drill: break the adapter, observe rollback, verify the audit record |
| C-09 | Add dependency/image scanning and an API fuzz pass to CI | R-16 | CI run with no high-severity findings |
| C-10 | Migrate the Kyverno policies from `kyverno.io/v1` `ClusterPolicy` to the CEL `policies.kyverno.io` kinds | R-09, N-02 | Policies render as `ValidatingPolicy`; the same four admission tests deny and admit identically |

## Sign-off record

| Field | Value |
|---|---|
| Review type | Self-assessment against the role requirements |
| Date | 2026-11-05 |
| Reviewed tree | `b1bd4a0` — 362 tests green, CI run `41955890300`; release `slm-0.5.0` signature verified. `evidence.json` names `a3f265d`, identical to `b1bd4a0` except for the evidence file itself |
| Scope | requirements, acceptance, risks, operations, release process, evidence, ADRs 0001–0013, README and technical notes |
| Cross-checks | evidence hashes match the working tree; A-22 digests equal the signed manifest; R-16, R-17 and R-18 residuals remain open |
| Platform release | latest manifest under [`release/`](../../release) |
| Evidence snapshot | [evidence.json](evidence.json) |
| Decision | **Accepted as reference architecture, C-02 closed; C-01 and C-03 to C-10 open.** A-30 evidences a reviewed bring-up plan, not a provisioned cluster (N-14) |
| Reviewer | Platform architecture (author) |
| Independent reviewer | *not yet assigned* |

An independent review should start with [requirements.md](requirements.md) and spot-check
three rows from *Verified* and three from *Not verified*.
