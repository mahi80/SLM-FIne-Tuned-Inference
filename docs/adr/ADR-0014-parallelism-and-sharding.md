# ADR-0014 — One model per GPU: no tensor or pipeline parallelism at the reference sizes

- **Status:** Accepted
- **Date:** 2026-11-07
- **Deciders:** Platform architecture
- **Related:** ADR-0001 (runtime), ADR-0002 (quantisation), ADR-0008 (capacity pools)

## Context

A model that does not fit on one GPU has to be split. vLLM offers two ways, and they can
be combined:

- **Tensor parallelism (TP)** splits every weight matrix across N GPUs. Each layer runs on
  all N at once and they exchange partial results with an all-reduce **twice per layer,
  every token**. It cuts per-token latency and multiplies memory, but it needs a fast
  interconnect (NVLink, or at least PCIe on the same host); over a network it is slower
  than one GPU.
- **Pipeline parallelism (PP)** puts consecutive layers on different GPUs or nodes.
  Activations cross the boundary once per stage, so it tolerates slower links and can
  span nodes. It adds no compute speed for a single request; the pipeline bubbles are
  only filled when many requests are in flight.

Sharding is the result in both cases: each GPU holds a slice of the weights and the KV cache.

The fleet today: 1–8B models, AWQ INT4, 12 GB Ada cards on-prem, 24–80 GB cards in the
cloud pools (ADR-0008).

## Decision

**Every model in the reference fleet is served with `tensor_parallel_size=1` and
`pipeline_parallel_size=1`; capacity scales out by replicas, not by splitting a model.**

The arithmetic that makes this the right call:

| Model | Weights (AWQ INT4) | Fits one 12 GB card? | Fits one 24 GB card? |
|---|---:|---|---|
| Qwen3-4B (reference) | ~2.6 GB | yes, with ~6 GB of KV cache | yes |
| 8B class | ~5 GB | yes, but only ~3 GB of KV cache | yes |
| 14B class | ~9 GB | no useful KV headroom | yes |
| 32B class | ~19 GB | no | no useful headroom → TP=2 on 48 GB+ |
| 70B class | ~40 GB | no | TP=2 on 80 GB cards, or TP=4 on 24 GB with NVLink |

For a model that fits, a second GPU used as a **replica** doubles throughput and keeps
failure domains separate. The same GPU used for **TP=2** buys lower per-token latency, at
the price of an all-reduce on every layer and a pod that fails as a pair. At ≤ 8B the
per-token latency is already inside every tenant budget (ADR-0011), so replicas win.

## When the decision flips

| Trigger | Change |
|---|---|
| A model's weights + 8 k-token KV cache for `max-num-seqs` no longer fit on one card | `--tensor-parallel-size 2` (or 4) on a node with NVLink; one pod per node, `nvidia.com/gpu: N` |
| A model is larger than one node's total VRAM | add `--pipeline-parallel-size` across nodes (Ray), TP inside each node; never TP across the network |
| A tenant needs lower per-token latency than one GPU can give an 8B+ model | TP=2 for that alias only, in its own pool |

The manifests already prepare for TP: the vLLM pod mounts a memory-backed `/dev/shm`
(used for the NCCL all-reduce), and the capacity pools count GPUs per replica rather than
assuming one.

## Consequences

**Positive:** the simplest operating model. Every replica is the same, a failed GPU loses
one replica, KEDA scales by adding identical pods, and there is no NCCL tuning.

**Negative:** the fleet cannot serve a model larger than one card's VRAM without a new pool
definition. That ceiling is intended: it is about 14B on 24 GB cards and 32B on 48 GB.

**Revisit when:** any alias needs a model above 14B, or a contract asks for an 8B+ model
with a per-token latency below what one GPU sustains at the pool's p95.
