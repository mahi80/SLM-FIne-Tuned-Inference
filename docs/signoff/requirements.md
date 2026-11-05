# Requirement traceability

Source: the role description supplied with the repository request. Requirements are quoted
in shortened form; nothing is reworded to fit what was built.

Status vocabulary:

| Status | Meaning |
|---|---|
| **Measured** | Ran on the RTX 4080 host in this repository; artifact or test is committed |
| **Modelled** | Calibrated simulator, labelled `x-simulated: true`; no hardware of that class was available |
| **Static** | Manifests render and pass checks; the path was not run on the target substrate |
| **Partial** | Part of the requirement is evidenced, part is not — the gap column says which |
| **Not demonstrated** | No evidence in this repository |
| **N/A** | Personal history, not something a repository can evidence |

## What the role asks you to do

| ID | Requirement | Evidence | Status | Gap |
|---|---|---|---|---|
| W-01 | Design the serving architecture: model selection, framework (vLLM, TensorRT-LLM, Triton), runtime topology | [ADR-0001](../adr/ADR-0001-inference-runtime.md), [ADR-0002](../adr/ADR-0002-quantisation.md), [C4 L2/L3](../architecture/c4.md#l2--containers); `make smoke` | **Partial** | vLLM is measured end to end. TensorRT-LLM and Triton were assessed on paper only; there is no NVIDIA datacentre GPU and no Triton model repository here |
| W-02 | CPU vs GPU routing by latency, cost and throughput | [ADR-0004](../adr/ADR-0004-policy-router.md), [ADR-0005](../adr/ADR-0005-benchmarks-eval-cost.md), [ADR-0011](../adr/ADR-0011-latency-and-cost-admission.md); `make bench`, `make cost`; `tests/test_router.py` | **Measured** (GPU) / **Modelled** (CPU, cloud) | The routing engine is real and tested, and pools now declare the latency and cost admission is checked against, with a tenant `latency_budget_ms` veto. The CPU and cloud figures remain simulators, and the comparison is a declared target rather than a live p95 |
| W-03 | Multi-cloud, provider-agnostic, no managed-AI lock-in | [C4 L4](../architecture/c4.md#l4--deployment); `deploy/kustomize/overlays/vanilla`; `config/capacity.yaml`; `deploy/cloud/`; `src/gateway/backends.py`, `src/gateway/app.py`; [ADR-0009](../adr/ADR-0009-external-backends.md), [ADR-0010](../adr/ADR-0010-backend-credentials.md); `make deploy-render` | **Partial** | The workload is plain Kubernetes + OCI + OpenAI API, and a pool registry plus Karpenter/AKS/GKE node-pool snippets describe the capacity contract. The external-backend path (TLS, rotating/file and OIDC credentials, opt-in egress, health probes) exists and is tested against local mocks, but no EKS, AKS or GKE cluster, provider endpoint or IdP was provisioned; the cloud tier remains a modelled standby (R-18) |
| W-04 | On-premises serving on OpenShift and VMware | `deploy/kustomize/overlays/openshift`, `deploy/openshift/airgap`; `make deploy-validate` | **Static** | Manifests render (35 objects) and pass policy checks. No OpenShift cluster, no VMware substrate |
| W-05 | Per-tenant isolation, data residency, air-gapped sovereign deployment | `config/policy.yaml`, `config/tenants.example.yaml`, `src/router/engine.py`; `make smoke-policy`; air-gap overlay | **Partial** | Isolation is logical (tenant keys, quotas, alias/tier admission), not a separate namespace or network per tenant. Residency rules are enforced and audited. Air gap is manifest-level; no disconnected install was performed |
| W-06 | Release architecture: rollout, staged deployment, rollback, promotion gates | `src/eval/gate.py`, `config/policy.yaml` canary, `src/registry`, [release-process.md](release-process.md) | **Measured** | Promotion is a canary percent change plus adapter hot-load; rollback is a policy edit. There is no automatic rollback on gate failure in-cluster — the sync fails and pages a human |
| W-07 | Governance: behaviour monitoring, inference audit, guardrails | `src/router/audit.py`, `deploy/observability/grafana/dashboards/platform.json`, `src/eval/golden.jsonl` | **Partial** | Audit and latency/decision metrics are measured. "Guardrails" here means schema contract, output parsing and eval gates — not a runtime content-moderation model |
| W-08 | DevSecOps pipeline and self-service deployment | `.github/workflows/ci.yml`, `deploy/gitops`, `deploy/policies` (Kyverno), `Makefile` | **Partial** | GitOps, admission policy and CI exist. There is no self-service portal; a release is still a reviewed pull request |
| W-09 | Evaluate open-source models against closed models for a use case | `bench/results/golden-comparison.json`, `finetune/evaluate.py` | **Partial** | Qwen3-4B base vs two adapters is measured on a 200-ticket held-out set. No closed frontier model was called (cost, data-egress and reproducibility constraints); the "frontier quality" comparison remains an untested thesis |
| W-10 | Produce architecture sign-off documents and review designs before implementation | This pack, [ADR index](../adr) | **Measured** | Self-assessment only; no external reviewer signed it |

## Must have

| ID | Requirement | Evidence | Status | Gap |
|---|---|---|---|---|
| M-01 | Deployed LLMs to production in a real enterprise | — | **Not demonstrated** | This is a reference platform on a laptop. Claiming otherwise would fail the first reference check |
| M-02 | Hands-on vLLM, TensorRT-LLM or Triton in a production serving context | Pinned vLLM runtime, multi-LoRA hot load, AWQ, KV-cache and throughput measurements; `make smoke`, `make adapter-smoke` | **Partial** | Hands-on vLLM is demonstrated with measured numbers. "Production context" is not: single host, single replica, no fleet |
| M-03 | Design CPU cluster inference for Xeon / EPYC | [ADR-0005](../adr/ADR-0005-benchmarks-eval-cost.md), `src/tiersim/profiles.py`, `config/cost.yaml` | **Modelled** | No Xeon or EPYC host was available. The profile is calibrated arithmetic, explicitly labelled MODELLED |
| M-04 | Kubernetes at production scale (EKS, AKS, GKE, OpenShift) | `deploy/kustomize`, `deploy/gitops`, `deploy/policies`, `deploy/cloud`, `config/capacity.yaml`, `config/fleet.yaml`, `tools/validate_deploy.py`, `tools/fleet_status.py` | **Partial** | Manifests, sync waves, admission policy, node-pool contracts and a one-cluster fleet inventory are real artifacts. Scale, upgrade behaviour, multi-AZ failure, the provider snippets and a second cluster were not exercised (R-19) |
| M-05 | Multi-cloud, provider-agnostic AI architecture | Same as W-03 | **Partial** | Portability is demonstrated at the manifest and API level, not by running on three clouds |
| M-06 | Quantisation (GPTQ, AWQ, GGUF) and routing trade-offs | [ADR-0002](../adr/ADR-0002-quantisation.md), AWQ W4A16 measured, cost model | **Partial** | AWQ is measured and routed around. GPTQ and GGUF were analysed, not served; no quality-vs-quant matrix was run |
| M-07 | 10+ years in AI/software architecture | — | **N/A** | Personal history; a repository cannot evidence it |
| M-08 | Computer science background | — | **N/A** | Personal history |

## Good to have

| ID | Requirement | Evidence | Status | Gap |
|---|---|---|---|---|
| G-01 | OpenShift for on-premises model serving | `deploy/kustomize/overlays/openshift`, Route, ServiceMonitor, SCC-compatible UID handling, IDMS/ITMS | **Static** | No cluster |
| G-02 | NVIDIA Dynamo, SGLang or custom inference schedulers | — | **Not demonstrated** | vLLM's own scheduler is used as shipped. No custom batching or scheduler was written |
| G-03 | GPU FinOps — cost modelling for mixed CPU/GPU fleets | `src/cost`, `config/cost.yaml`, `bench/results/cost-model.json`; `make cost` | **Measured** (throughput) / **stated assumptions** (prices) | Capex, power and ops figures are declared inputs, not invoices. The model separates capacity cost from workload cost |
| G-04 | Sovereign AI or regulated-industry deployment | Sovereignty and region veto in policy; air-gap overlay; hash-chained audit | **Partial** | The controls exist and are tested. No regulator, auditor or certified environment was involved |
| G-05 | OpenVINO or ROCm for CPU-optimised inference | `src/tiersim` CPU profile | **Not demonstrated** | The CPU tier is simulated. No OpenVINO or ROCm build was executed |

## Summary

- **Measured end to end:** GPU serving with adapters, routing decisions, audit chain,
  gateway contract, observability, benchmarks, eval gate, cost model, signed releases,
  static deployment validation.
- **Modelled:** CPU tier, cloud tier, GPU reference profile, queueing behaviour on those
  tiers.
- **Static only:** everything OpenShift, and the air-gap mirror. The vanilla overlay,
  Kyverno admission and Argo CD reconciliation were observed on a kind cluster (A-17–A-21);
  OpenShift SCC, Route, ServiceMonitor and the release hooks were not.
- **Explicitly absent:** production multi-tenant operation, closed-model comparison,
  TensorRT-LLM/Triton/Dynamo/SGLang, OpenVINO/ROCm, real Xeon/EPYC measurements, automatic
  rollback, shared gateway state for HA.

The value of this table is the three right-hand columns. A candidate who marks M-01
"measured" on a laptop repo is not being reviewed for architecture judgement.
