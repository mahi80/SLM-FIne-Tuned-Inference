# ADR-0001 — vLLM as the GPU serving runtime

- **Status:** Accepted
- **Date:** 2026-11-04
- **Deciders:** Platform architecture
- **Related:** ADR-0002 (quantisation), ADR-0006 (OpenShift reference substrate)

## Context

The platform must serve 1–8B open-weight models, several fine-tuned LoRA adapters per base
model, behind an OpenAI-compatible API, on Kubernetes and OpenShift. Today it runs on a
single 12 GB Ada laptop GPU; later it runs on multi-node GPU pools with 24–80 GB cards.

The runtime therefore has to satisfy non-negotiables before raw throughput is considered:

1. Multi-LoRA: load and unload tenant adapters without restarting the base model.
2. Portable artifact: the same container runs on-prem and in three clouds.
3. Wide quantisation support, because the tier policy selects quantisation per tier.
4. OpenAI-compatible API, so client code never learns the runtime's name.
5. Healthy upstream, because this is a reference architecture a customer will operate.

## Options considered

| Option | Strength | Why it did not win outright |
|---|---|---|
| **vLLM** | PagedAttention, continuous batching, first-class multi-LoRA hot-loading, broad quant support, Apache-2.0, dominant upstream | Not the fastest kernel for a single hot model; Python-side overhead under very high RPS |
| **TensorRT-LLM** | Best raw NVIDIA throughput/latency at fixed shape | Engines are built per model × GPU arch × parallelism; multi-adapter churn is painful; slower to onboard new models |
| **Triton Inference Server** | Excellent multi-model orchestration, ensembles, metrics | Usually runs vLLM as its backend anyway; adds an ops layer we do not need in Phase 1–3 |
| **SGLang** | Strong prefix caching and structured-output performance | Smaller adapter/quantisation ecosystem; higher risk for a customer-operated reference |
| **llama.cpp** | Best CPU and edge story, GGUF | Not built for high-concurrency GPU serving |
| **TGI** | Simple, HF-native | Narrower quantisation and adapter flexibility than vLLM |

## Decision

**vLLM is the GPU runtime of record**, deployed as a pinned container image behind a
runtime-agnostic interface. Each tier exposes the same OpenAI-compatible contract, so the
router never branches on runtime.

Two escape hatches are kept open by design:

- `InferenceBackend` is an interface; a TensorRT-LLM backend can be registered for a
  single high-volume model without touching routing or governance.
- Triton remains the answer if a tenant needs multi-model ensembles or non-LLM models
  (embeddings, rerankers, classifiers) in the same serving graph.

## Consequences

**Positive:** multi-LoRA hot-loading is the core of per-tenant fine-tuning isolation;
one image runs on-prem and in every cloud; quantisation choice is configuration, not a
rebuild; upstream velocity means new models land within days.

**Negative:** single-model peak throughput will trail a tuned TensorRT-LLM engine by a
measurable margin — accepted, because adapter churn and portability matter more than the
last 15% on a fleet where most models are small. The runtime is on the critical path, so
the image is pinned, SBOM'd and signed (Phase 4), never `latest`.

**Revisit when:** a model exceeds 50% of fleet GPU-seconds, or a customer contract requires
a sub-100 ms p95 at sustained load that vLLM cannot hold with headroom.
