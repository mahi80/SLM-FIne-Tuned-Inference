# SLM Serving Fabric

Private, provider-agnostic serving for fine-tuned small language models — a runnable
reference platform, not a slide deck.

**Status:** Phase 1 of 6 — GPU tier, gateway, simulated CPU tier and observability are live.

---

## Thesis

A 1–8B model, fine-tuned on a narrow enterprise task and served with the right runtime
per compute tier, delivers most of the quality of a frontier model at a fraction of the
cost — provided the platform can decide, per request, **which model, which tier, and
which quantisation** still satisfies the SLO inside the customer's perimeter.

Most "private LLM" architectures stop at a single vLLM deployment behind an API key.
That is the easy 20%. The remaining 80% is:

- choosing between **GPU and CPU** on latency, cost and concurrency — not on vibes;
- keeping **tenants, data residency and air-gapped sites** from leaking into each other;
- shipping a **model release** through gates, canary, and rollback without a redeploy;
- proving **why** every request went where it went, to an auditor.

This repo builds that platform, and documents the decisions as reviewable ADRs.

---

## What is real, and what is modelled

An architecture document that blurs this line is worthless in review, so the repo keeps
the distinction explicit:

| Layer | Status | Evidence |
|---|---|---|
| GPU tier (vLLM, AWQ W4A16, multi-LoRA capable) | **Measured** | `make smoke`, tables below |
| Gateway (auth, quotas, streaming proxy, audit) | **Measured** | `make smoke-gateway` |
| Observability (Prometheus + provisioned Grafana) | **Measured** | dashboard `slm-platform`, 7 panels |
| CPU tier (OpenVINO / llama.cpp on Xeon/EPYC) | **Modelled** — calibrated simulator, no Xeon on this laptop | `src/tiersim`, labelled `x-simulated: true` |
| Cloud burst tiers (EKS / AKS / GKE) | Profile defined, not wired | `src/tiersim/profiles.py` |
| OpenShift / air-gapped deployment | **Static validation** — manifests render and pass policy checks; no cluster available | `deploy/openshift` |
| Router policy engine, governance, release pipeline | Planned (Phases 3–5) | — |

Numbers from a simulated tier are labelled `MODELLED` everywhere they appear.

---

## Architecture

```mermaid
flowchart LR
  C[Client / app] -->|OpenAI API| GW[Gateway<br/>auth · quota · guardrails · audit]
  GW --> RT{Router<br/>policy engine}
  RT -->|interactive, long decode| GPU[GPU tier<br/>vLLM · multi-LoRA]
  RT -->|short, bulk, low QPS| CPU[CPU tier<br/>OpenVINO / llama.cpp]
  RT -->|overflow, if permitted| CL[Cloud burst<br/>EKS · AKS · GKE]
  RT -.->|residency / sovereignty veto| RT
  REG[(Signed model<br/>release manifests)] --> GPU
  REG --> CPU
  GPU --> OBS[(Audit log · metrics · evals)]
  CPU --> OBS
  CL --> OBS
```

The platform contract is deliberately portable: Kubernetes + Gateway API + OCI registry +
S3-compatible object storage + OIDC. **OpenShift is the reference substrate**; EKS, AKS and
GKE differ only in node pools, storage class, KMS and DNS.

---

## Quickstart

Requirements: Docker Desktop (WSL2 backend), an NVIDIA GPU with a working Windows driver,
Python 3.10+, ~12 GB free VRAM and ~15 GB disk.

```bash
make gpu-check     # NVIDIA GPU visible inside a container
make up            # build, pull, download weights, wait until every service is healthy
make smoke         # GPU tier directly
make smoke-gateway # both tiers through the gateway
make logs          # follow all services
make down          # stop (model cache and dashboards are kept)
```

| Service | URL | Notes |
|---|---|---|
| Gateway (OpenAI API) | http://localhost:8020 | `Authorization: Bearer dev-acme-key` |
| GPU tier | http://localhost:8010 | direct; bypasses the gateway |
| CPU tier (simulated) | http://localhost:8011 | direct; responses carry `x-simulated: true` |
| Prometheus | http://localhost:9090 | scrapes gateway, simulator and vLLM |
| Grafana | http://localhost:3001 | `admin` / `admin`, dashboard **SLM Serving Fabric** |

Clients send the same OpenAI request to the gateway and pick a tier with the model alias:

```bash
curl -s http://localhost:8020/v1/chat/completions \
  -H 'Authorization: Bearer dev-acme-key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"slm-small","messages":[{"role":"user","content":"hello"}],"max_tokens":32}'
```

`slm-small` resolves to the GPU tier, `slm-small-cpu` to the simulated CPU tier. Clients
address an alias, never a weights path — that indirection is what lets a release swap
weights or an adapter without touching application code.

Host ports are configurable in `.env`; 8000 is deliberately avoided because it is commonly
already taken on developer machines.

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
| Engine init | 30.5 s (model load, profiling, CUDA graph capture) |
| Time to first token | 234 ms first request · **20 ms** median warm |
| Decode throughput | **128.5 tok/s** median, 5 runs of 80–96 tokens |
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

With the GPU container stopped, `slm-small` returned **200 from the CPU tier** with
`x-tier: cpu` and `x-fallback: true`. The GPU was then restarted and served normally.

---

## Gateway contract

Every response carries its routing decision:

| Header | Meaning |
|---|---|
| `x-request-id` | Correlates the audit line, gateway metrics and upstream logs |
| `x-route-alias` | The route that actually served the request |
| `x-tier` | `gpu`, `cpu` (later also `cloud`) |
| `x-route-reason` | Why this route was chosen — static in Phase 1, policy-driven in Phase 3 |
| `x-policy-version` | Version of the table or policy that decided |
| `x-fallback` | `true` when the primary route failed and the fallback served |
| `x-simulated` | Present and `true` only for modelled tiers |

Working today: bearer auth with constant-time key comparison, per-tenant sliding-window
RPM limits (429 with `Retry-After`), unbuffered streaming pass-through, usage accounting
taken from upstream, one structured audit line per request, and one fallback attempt on
connection failure.

Deliberately absent until Phase 3: quality-aware routing, shadow traffic, circuit breakers,
and a shared quota counter for multi-replica gateways. The in-memory limiter is explicitly
single-replica; pretending otherwise would be the kind of gap that fails an architecture
review.

---

## Repository map

```
compose.yaml                     the whole platform: tiers, gateway, observability
Makefile                         operational entry points
config/routes.yaml               alias → backend table (Phase 3 replaces the lookup)
config/tenants.example.yaml      tenant keys and quotas; tenants.yaml is gitignored
scripts/smoke.py                 GPU tier smoke test
scripts/smoke_gateway.py         end-to-end test through the gateway, both tiers
docker/                          gateway and simulator images
src/gateway/                     OpenAI-compatible API, auth, quotas, proxy, audit
src/router/                      policy engine: admissibility, tier scoring, breakers
src/tiersim/                     calibrated CPU / cloud tier simulator
src/registry/                    signed model release manifests
src/eval/                        golden sets, deterministic checks, drift detection
deploy/observability/grafana/    provisioned datasource and dashboard
deploy/openshift/                SCC, Route, ImageStream, GPU Operator, IDMS/ITMS
deploy/kustomize/                overlays: openshift, vanilla k8s, airgap
deploy/policies/                 Kyverno / Gatekeeper admission policy
deploy/gitops/                   Argo CD app-of-apps, Rollouts analysis
finetune/                        QLoRA scripts producing adapter v1 / v2
bench/                           load generator and recorded results
docs/adr/                        architecture decision records
docs/signoff/                    architecture review template and completed review
```

---

## Decisions taken in Phase 0
**Pinned everything.** The runtime is pinned to `vllm/vllm-openai:v0.33.0` and the weights
to a commit SHA, not `main`. A serving node is a reproducible artifact; `latest` means the
model can change under a running release without anyone noticing.

**AWQ INT4, not FP8.** The target GPU is Ada (sm_89), which has no FP8 tensor cores. FP8
would fall back to dequantise-on-the-fly and lose to INT4 on both latency and memory.

**Third-party quantisation is a recorded risk.** Qwen does not publish an official AWQ
build of the 2507 Instruct 4B, so Phase 0 pins a community AWQ artifact by revision. This
is acceptable for a reference build and unacceptable for regulated production — Phase 4
re-quantises in-house from official weights and records provenance per release. This is
ADR-0002 once the ADR series lands.

**Headroom is reserved on purpose.** 8k context and 16 concurrent sequences leave VRAM
for multiple LoRA adapters in Phase 2, rather than maxing out a single-model deployment.

**One tier per container, one alias per model.** Scaling, quotas and failure domains follow
the tier; routing and release management follow the alias.

### Phase 1

**The simulator is a timing model, not a fake model.** `src/tiersim` returns canned text but
reproduces prefill, per-token decode and queueing from a documented profile, and tags every
response `x-simulated: true`. Its numbers are labelled MODELLED. Its job is to let the
router, dashboards and cost model be built before CPU hardware exists.

**Routing is a table, but the contract is already final.** Phase 3 swaps the lookup for
policy evaluation; clients already see `x-route-reason`, `x-policy-version` and `x-fallback`,
so the client contract will not change when the policy engine arrives.

**Fallback is connection-level only.** A refused connection fails over to the next route; a
500 does not, because silently downgrading a tier after an application error hides bugs.
Quality-aware failover is a Phase 3 policy decision.

**No buffering in the proxy.** Responses are relayed chunk by chunk. The injected
`stream_options.include_usage` is stripped unless the client asked for it, so usage can be
metered without changing the caller's stream shape.

**Tenant secrets never enter git.** `config/tenants.yaml` is generated from an example file
and gitignored; the gateway mounts the config directory read-only.

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Scaffold, GPU passthrough, vLLM serving, smoke test | **Done** |
| 1 | Gateway, CPU tier simulator, Prometheus + Grafana | **Done** |
| 2 | QLoRA adapters v1/v2, multi-LoRA hot loading | Next |
| 3 | Router policy engine, canary, audit trail | |
| 4 | Benchmarks, eval harness, cost model | |
| 5 | OpenShift / air-gap manifests, GitOps, admission policy | |
| 6 | ADRs, C4 diagrams, architecture sign-off pack | |

---

## Weights and licences

Base model: [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)
(Apache-2.0). AWQ build: `cyankiwi/Qwen3-4B-Instruct-2507-AWQ-4bit`, pinned by revision.
Runtime: vLLM (Apache-2.0). No model weights are vendored into this repository.
