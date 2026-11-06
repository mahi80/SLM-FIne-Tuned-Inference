# LLM serving checklist: where each concept lives in this repo

Twenty-five concepts anyone who claims to deploy LLMs should be able to explain, each
mapped to the code, config or decision record that implements or deliberately rejects it.
"Rejected" here means a written decision with numbers behind it. Nothing in this list is
left unaddressed.

| # | Concept | Status | Where |
|---:|---|---|---|
| 1 | Prefill vs decode | Measured | `src/bench/runner.py`: TTFT ≈ prefill, TPOT/ITL = decode; `prefill_tokens_per_s_est`. Simulator models both rates (`src/tiersim/profiles.py`) |
| 2 | KV cache | Configured + scaled on | `--gpu-memory-utilization 0.85`; KEDA trigger on `vllm:gpu_cache_usage_perc` (`config/capacity.yaml`); budget below |
| 3 | Time to first token | Measured | `bench` TTFT p50/p95/p99; gateway `gateway_ttft_seconds` histogram |
| 4 | Inter-token latency | Measured | `bench` ITL p50/p95/p99 (pooled chunk gaps) and TPOT per request |
| 5 | Latency vs throughput | Measured | `bench` runs c = 1, 4, 12: req/s and tok/s against TTFT/total latency |
| 6 | Static vs continuous batching | Configured | vLLM continuous batching, `--max-num-seqs 16`; explained below |
| 7 | PagedAttention | Inherited from runtime | vLLM (ADR-0001); explained below |
| 8 | Quantization (INT8, INT4, FP8) | Decided | ADR-0002: AWQ W4A16 on GPU, GGUF on CPU, FP8 rejected for this fleet |
| 9 | GPTQ and AWQ | Decided | ADR-0002 options table |
| 10 | Speculative decoding | Decided off, with enable path | ADR-0015 |
| 11 | Prefix caching | Enabled + measured | `--enable-prefix-caching` (enforced by `tools/validate_deploy.py`); `make bench-prefix` |
| 12 | Context length vs memory | Configured | `--max-model-len 8192`; budget below |
| 13 | VRAM budgeting | Measured | ADR-0002; `scripts/doctor.py` (≥ 10 GB); measured 9,975 / 12,282 MiB (`docs/technical-notes.md`) |
| 14 | Tensor parallelism | Decided (TP=1) | ADR-0014 |
| 15 | Pipeline parallelism | Decided (PP=1) | ADR-0014 |
| 16 | Multi-GPU sharding | Decided, with triggers | ADR-0014: size table and when to shard |
| 17 | Serving engines | Decided | ADR-0001: vLLM vs TensorRT-LLM vs TGI vs SGLang vs Triton vs llama.cpp |
| 18 | Streaming responses | Implemented | gateway SSE relay with usage injection (`src/gateway/app.py`) |
| 19 | Rate limits and queueing | Implemented | per-tenant RPM → 429 + `Retry-After` (`src/gateway/auth.py`); engine queue depth in KEDA |
| 20 | Autoscaling and cold starts | Implemented | KEDA ScaledObject, `minReplicaCount: 1` because a cold start costs model load time (`config/capacity.yaml`) |
| 21 | Model routing | Implemented | policy router: admission, canary, latency budgets (`src/router`, ADR-0004, ADR-0011) |
| 22 | Semantic caching | Implemented, opt-in | `src/gateway/semantic_cache.py`; tenant `semantic_cache: true` |
| 23 | Provider fallbacks | Implemented | ordered candidates + circuit breakers (`src/router/breakers.py`), external backends (ADR-0009) |
| 24 | Cost per million tokens | Implemented | `src/cost/model.py`, `make cost` |
| 25 | Cost per request | Implemented | `tier_cost_per_request`; `est_cost_usd` on every audit record |

## The reference GPU's memory budget (items 2, 12, 13)

Qwen3-4B has 36 layers, 8 KV heads and a head dimension of 128. With an FP16 KV cache,
each token costs:

```
2 (K and V) × 36 layers × 8 heads × 128 dims × 2 bytes = 147,456 B ≈ 144 KiB per token
```

| Line | Size | Note |
|---|---:|---|
| Card | 12.0 GiB | RTX 4080 Laptop |
| vLLM's share (`--gpu-memory-utilization 0.85`) | ~10.2 GiB | the rest is left for the driver and display |
| AWQ INT4 weights | ~2.6 GiB | the embeddings stay at 16 bit |
| Activations, CUDA graphs, 4 LoRA slots at rank 16 | ~1.5 GiB | estimate; vLLM profiles this at startup |
| **KV cache** | **~6 GiB ≈ 42k tokens** | vLLM logs the exact figure as `GPU KV cache size` at boot |

What that buys:

- **5 full-length requests** (8,192 tokens each) fit at once;
- or **16 requests** (`--max-num-seqs`) averaging about 2.6k tokens of prompt plus output;
- doubling `--max-model-len` to 16k does not cost memory up front. It only lets a single
  request claim twice as much KV, so the number of full-length requests that fit falls to 2.

When the KV cache fills, vLLM **preempts** running sequences and recomputes them later.
This is why KEDA scales on cache usage at 85% (`config/capacity.yaml`) instead of waiting
for queue depth, which only rises after preemption has started.

## Static vs continuous batching (item 6)

**Static batching** collects N requests, runs them together and returns when the
*longest* finishes. A 10-token answer waits for a 500-token one, and a new request waits
for the whole batch.

**Continuous batching** (vLLM, TGI, TensorRT-LLM "in-flight batching") schedules at the
token step. Every iteration, finished sequences leave the batch and queued ones join, and
new prompts are prefilled in chunks alongside running decodes. GPU occupancy stays high
without anyone waiting for a straggler. `--max-num-seqs` caps how many sequences share a
step, and that is the knob behind the latency-vs-throughput curve `make bench` draws.

## PagedAttention (item 7)

A naive server reserves `max-model-len` tokens of KV memory per request up front. Most of
that reservation is never used, and the leftover gaps fragment memory. PagedAttention
stores the KV cache in fixed-size **blocks** (16 tokens by default) addressed through a
per-sequence block table, the way an OS pages virtual memory. Memory is claimed one block
at a time as tokens are generated, waste is limited to the last partial block, and
sequences can **share** blocks. Prefix caching (item 11) is built on that sharing: blocks
are hashed from the first token onward, so identical prefixes such as a system prompt or a
few-shot block map to the same physical blocks. This is also why the prefix probe in
`make bench-prefix` puts its cache-busting nonce at the very start.

## How to read a benchmark run

```
make bench         # TTFT, TPOT, ITL p95/p99, tok/s at c = 1, 4, 12
make bench-prefix  # TTFT with a shared system prompt vs one that cannot be reused
make cost          # $/1M output tokens per tier at the stated utilisation
```

- TTFT rising with concurrency while TPOT stays flat → queueing ahead of prefill; add
  replicas.
- TPOT rising with concurrency → the decode batch is saturating memory bandwidth; this is
  the throughput ceiling for this card.
- ITL p99 far above p50 → stalls, usually a long prefill landing in the batch or KV
  preemption; check `vllm:gpu_cache_usage_perc`.
