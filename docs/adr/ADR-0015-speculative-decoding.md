# ADR-0015 — Speculative decoding: evaluated, off by default, n-gram first when enabled

- **Status:** Accepted
- **Date:** 2026-11-07
- **Deciders:** Platform architecture
- **Related:** ADR-0001 (runtime), ADR-0005 (benchmarks), ADR-0011 (latency admission)

## Context

Decode is memory-bound. Each output token is one forward pass that reads every weight and
the whole KV cache to produce a single token, so the GPU's compute sits mostly idle.
Speculative decoding uses that idle compute. A cheap **proposer** guesses the next *k*
tokens, the target model **verifies all k in one forward pass**, and every token up to the
first wrong guess is accepted. The output distribution is unchanged, because the target
model still decides every token. What changes is how many tokens each pass yields.

The gain depends on two numbers: the **acceptance rate** and the proposer's **cost**.
Proposers vLLM supports:

| Proposer | Cost | Works well when |
|---|---|---|
| **n-gram / prompt lookup** | ~free, no extra weights | output repeats spans of the input: RAG answers, summaries, extraction, code edits |
| **draft model** (e.g. Qwen3-0.6B for Qwen3-4B) | a second model in VRAM + its own KV cache | general chat, when a small sibling with the same tokenizer exists |
| **EAGLE / MTP heads** | small trained head | a published head exists for the exact base model |

## Decision

**Speculative decoding is off in the reference deployment.** It is the first decode-latency
lever to pull, in this order: n-gram, then EAGLE/MTP, then a draft model.

Why it is off today:

1. **Batching eats the gain.** The reference GPU runs `max-num-seqs 16`. Under load,
   continuous batching already fills the idle compute that speculation would use, and
   rejected guesses waste slots. Speculation pays at low concurrency, and the KEDA pool is
   sized for the busy case.
2. **VRAM.** A draft model on a 12 GB card takes space from the KV cache, and the KV cache
   is what sets concurrency (ADR-0002). n-gram costs nothing, so it is the only proposer
   that fits the laptop reference.
3. **Multi-LoRA.** Every request on the reference runs through a tenant adapter. A draft
   model trained on the base distribution accepts fewer tokens on adapter outputs. Check
   speculation per adapter against the release gate before any claim.

## How to enable and measure (n-gram)

Add to the vLLM args in `compose.yaml` or the Deployment:

```
--speculative-config '{"method": "ngram", "num_speculative_tokens": 4, "prompt_lookup_max": 4}'
```

Then compare `make bench` before and after on the same prompt set. The decode-phase
columns (`TPOT p50`, `ITL p95/p99`) show the effect. TTFT should not move, because
speculation does not touch prefill. Ship it only if all of these hold:

- TPOT p50 improves by at least 15% at c=1 **and** total throughput does not fall at
  c=12;
- `make eval-gate` passes unchanged (output equivalence is guaranteed in theory; the gate
  catches kernel or adapter interaction bugs);
- vLLM's `spec_decode` acceptance metric is above ~0.5 on production-like prompts.

## Consequences

**Positive:** no added complexity in the default path. When a latency-sensitive,
low-concurrency alias appears, such as an interactive RAG assistant, enabling speculation
is one flag change plus a benchmark.

**Negative:** single-stream decode latency is left on the table for tenants who send one
request at a time.

**Revisit when:** an alias runs mostly at c ≤ 2, or a pool moves to cards with enough
headroom (24 GB+) for a draft model next to an 8k KV cache.
