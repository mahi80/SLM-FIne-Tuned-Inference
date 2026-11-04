# Acceptance checklist and conditions

This is the reviewer's sheet. Each row is a claim, the command that tests it, and what a
passing run looked like in this environment. Rows marked *not verified* are not failures of
the design; they are limits of the environment the design was built in.

## Verified in this environment

Host: RTX 4080 Laptop 12 GiB, Docker 29.4, vLLM v0.33.0, Windows 11 + Git Bash,
Python 3.12, OpenSSL 3.5, `kubectl` for rendering only.

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
| A-11 | Router, eval, cost, deployment and release logic are unit tested | `make test` | 104 tests, 0 failures, stdlib `unittest` | Measured |
| A-12 | Three overlays render and pass static checks | `make deploy-validate` | openshift 32, vanilla 25, airgap 31 objects; no errors | Static |
| A-13 | A signed release matches the deployment tree | `make release-check` | manifest signature verified; policy, images, revision, adapter set and gate all agree | Measured |
| A-14 | Secrets are not in Git; workloads are non-root with limits and probes | `make deploy-validate`, `tests/test_deploy.py` | no `Secret` objects rendered; 17 deployment tests pass | Static |
| A-15 | Air gap removes internet egress and forces offline mode | `make deploy-validate` | model-download NetworkPolicy absent; `HF_HUB_OFFLINE=1` present | Static |
| A-16 | CI runs the tests and both validators on a hosted runner | GitHub Actions run `41875084383` on `31b4a7c` | unit tests, `validate_deploy`, `validate_release` and `registry verify` all passed | Measured (hosted), no cluster |

Reproducible artifacts for each row are under [`bench/results/`](../../bench/results) and
the test suite; [evidence.json](evidence.json) records their hashes and the commit.

## Not verified in this environment

| # | Gap | Why | Consequence | Risk |
|---|---|---|---|---|
| N-01 | No OpenShift, EKS, AKS or GKE cluster | None available | Route, SCC, ServiceMonitor, Kyverno enforcement and Argo reconciliation are unexercised | [R-09](risks.md#r-09--no-live-cluster-validation) |
| N-02 | Kyverno policies never evaluated by an admission controller | No cluster | A policy could be syntactically valid and semantically wrong | R-09 |
| N-03 | Argo CD sync waves and PostSync hooks | No cluster | Ordering and hook failure behaviour are read, not observed | R-11 |
| N-04 | Air-gapped installation | No disconnected site | Mirror list and IDMS/ITMS are unproven end to end | R-09 |
| N-05 | Xeon / EPYC CPU tier | No server hardware | CPU latency, throughput and cost are MODELLED | [R-10](risks.md#r-10--modelled-cpu-and-cloud-numbers) |
| N-06 | TensorRT-LLM, Triton, Dynamo, SGLang | No datacentre GPU | Only vLLM was served | [R-15](risks.md#r-15--single-runtime) |
| N-07 | Closed-model comparison | Cost, data-egress and reproducibility | The thesis "most of frontier quality" is untested | [R-12](risks.md#r-12--evaluation-set-is-small-and-synthetic) |
| N-08 | Production load and HA | One host, one GPU, one gateway replica | Quotas, breakers and audit assume one writer | [R-08](risks.md#r-08--gateway-is-a-single-writer) |
| N-09 | Security testing beyond unit tests | No pentest, fuzzing or dependency scanning in CI yet | Unknown classes of input/API defects | [R-05](risks.md#r-05--no-in-cluster-mtls), [R-16](risks.md#r-16--no-dependency-or-image-scanning) |
| N-10 | Audit sink durability under storage failure | Single node | A failed audit write does not fail the request | [R-01](risks.md#r-01--audit-chain-is-evidence-not-prevention) |

## Conditions

Production use for a regulated tenant requires these to be closed and evidenced. Each is a
risk accepted for the reference build, not an oversight.

| # | Condition | Closes | Evidence required |
|---|---|---|---|
| C-01 | Install on a real OpenShift cluster: Kyverno in Enforce, Argo CD synced, Route TLS verified, ServiceMonitor scraping | R-09, N-01–N-04 | `oc get route`, admission denial tests, Argo sync history, Grafana panels populated |
| C-02 | Pin images by digest, not tag, in manifests and the release manifest | R-02 | Rendered images contain `@sha256:`; digest matches the signed manifest |
| C-03 | Replace static bearer keys with OIDC / short-lived credentials; rotate the release key | R-04 | Tenant auth failure and rotation rehearsal recorded |
| C-04 | Ship the audit head hash to a WORM or append-only sink outside the cluster | R-01 | Object-lock or equivalent retention policy; restore test |
| C-05 | Move quota, breaker and audit state to a shared store, or accept single-replica and document the maintenance window | R-08 | Replica-2 test proving no quota bypass and no chain fork |
| C-06 | Benchmark on a real Xeon/EPYC node and replace the simulator | R-10 | Measured CPU artifact with `source: measured` |
| C-07 | Compare against at least one closed model on tenant-representative data, within the tenant's egress policy | R-12 | Evaluation report under the same golden contract |
| C-08 | Automatic rollback on failed in-cluster gate (or a documented manual SLA) | R-11 | Drill: break the adapter, observe rollback, verify the audit record |
| C-09 | Add dependency/image scanning and an API fuzz pass to CI | R-16 | CI run with no high-severity findings |

## Sign-off record

| Field | Value |
|---|---|
| Review type | Self-assessment against the role requirements |
| Date | 2026-11-04 |
| Platform release | latest manifest under [`release/`](../../release) |
| Evidence snapshot | [evidence.json](evidence.json) |
| Decision | **Accepted as reference architecture, conditions C-01 to C-09 open** |
| Reviewer | Platform architecture (author) |
| Independent reviewer | *not yet assigned* |

An independent review should start with [requirements.md](requirements.md) and spot-check
three rows from *Verified* and three from *Not verified*.
