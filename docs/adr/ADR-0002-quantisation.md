# ADR-0002 — AWQ INT4 on GPU, GGUF on CPU, provenance tracked per release

- **Status:** Accepted
- **Date:** 2026-11-04
- **Deciders:** Platform architecture
- **Related:** ADR-0001 (runtime), ADR-0005 (CPU tier runtime)

## Context

Quantisation is not a global setting in this platform — it is a routing dimension. The
same logical model is served at different precisions on different tiers, and the router
must know the quality and cost delta between them.

Constraints that decide the format:

- The reference GPU is **Ada (sm_89)**. It has no FP8 tensor cores; FP8 weights fall back
  to dequantise-on-the-fly and lose to INT4 on both memory and latency.
- 12 GB VRAM must hold weights **plus** KV cache **plus**, from Phase 2, several LoRA
  adapters. A 4B model in BF16 (~8 GB) leaves no room.
- The CPU tier runs on Xeon/EPYC without GPUs, where GGUF and OpenVINO are the mature
  formats; a GPU-native format is useless there.
- Quantised weights are supply-chain artifacts. A community quant can be replaced or
  deleted by its uploader, and a silent re-upload changes model behaviour under a fixed
  name.

## Options considered

| Format | Where it wins | Where it loses |
|---|---|---|
| **AWQ 4-bit** | Accuracy retention at 4 bits, mature `awq_marlin` kernels in vLLM, small footprint | Needs calibration data; quality still degrades on multi-step reasoning |
| **GPTQ 4-bit** | Broad checkpoint availability | More sensitive to calibration; slower kernels in some shapes |
| **FP8** | Hopper/Blackwell throughput, near-lossless | Not accelerated on Ada — wrong tool for this fleet |
| **GGUF Q4_K_M / Q5_K_M** | CPU and edge, llama.cpp ecosystem | Not a GPU serving path for vLLM |
| **BF16** | Fidelity | Does not fit the VRAM budget with adapters |

## Decision

1. **GPU tier:** AWQ W4A16 weights, packaged as compressed-tensors so vLLM selects the
   marlin kernel automatically, for models ≤ 4B on 12 GB cards. BF16/FP8 are reconsidered
   per node class once ≥ 24 GB GPUs are in the pool.
   *Phase 0 note:* the chosen artifact is `compressed-tensors`-packaged, not a bare AWQ
   checkpoint, so an explicit `--quantization awq_marlin` is rejected at startup. The
   runtime is left to detect the quantization method from the model config — forcing a
   kernel name is a worse contract than trusting a pinned artifact's own metadata.
2. **CPU tier:** GGUF Q4_K_M (llama.cpp) or INT4 (OpenVINO), selected per CPU vendor in
   ADR-0005.
3. **Provenance:** every release manifest records base model, quantiser, source URL,
   revision SHA and a checksum of the weight shards.
4. **Production rule:** weights are quantised **in-house** from official checkpoints.
   Third-party quants are permitted only for reference builds and local development, and
   every such artifact is flagged in its release manifest.

## Consequences

**Positive:** a 4B AWQ build occupies ~2.5 GB, leaving room for 8k context and multiple
adapters; one model family can be served at two precisions on two tiers for routing
experiments; the supply chain is recorded rather than assumed.

**Negative:** Phase 0 pins a community AWQ artifact because Qwen publishes no official
AWQ build of the 2507 Instruct 4B. That is a **recorded risk**, not a hidden one: it is
acceptable for a reference build, unacceptable for a regulated deployment, and closed in
Phase 4 by quantising locally and passing an eval gate.

**Measured, not assumed:** every quantisation change must clear the eval suite in Phase 4.
Task accuracy on extraction/classification is expected to hold; multi-step reasoning is
where 4-bit loss shows up, which is exactly why the router keeps an 8B BF16 tier in reserve.

**Revisit when:** a ≥ 24 GB node class enters the fleet (FP8 becomes attractive), or a
tenant's eval shows AWQ below its quality floor (escalate to a larger model rather than a
larger precision — precision is the cheaper lever only up to a point).
