# ADR-0003 — QLoRA adapters served on the AWQ base, one alias per adapter

- **Status:** Accepted
- **Date:** 2026-11-04
- **Deciders:** Platform architecture
- **Related:** ADR-0002 (quantisation), ADR-0001 (runtime)

## Context

The platform's premise is that a small model fine-tuned on one narrow task
beats a general model on that task. Phase 2 has to prove that claim with real
adapters, not a diagram.

Constraints:

- The GPU tier has **12 GB VRAM**, already holding the AWQ 4B weights and a
  5.9 GiB KV cache. Two adapter checkpoints and a training run must fit
  alongside it.
- Base weights are the **pinned AWQ artifact**. Re-quantising per fine-tune
  would multiply supply-chain risk and invalidate the release manifest.
- A model release must be promotable and reversible **without a redeploy**, and
  both adapters must be servable at the same time so a canary can compare them.
- Training on 12 GB must not require a second copy of the 4B model in BF16.

## Options considered

| Option | Where it wins | Where it loses |
|---|---|---|
| Full fine-tune | Maximum task fit | 8 GB weights + optimiser states do not fit; produces a new base artifact and loses the shared KV cache |
| Merge adapter into BF16, re-quantise to AWQ | One artifact, no LoRA overhead at inference | Every experiment needs a re-quantise + eval cycle; rollback means shipping weights; provenance churn |
| Separate vLLM per adapter | Total isolation | Duplicate base weights per adapter — impossible on 12 GB |
| **QLoRA adapter served on the AWQ base via multi-LoRA** | One base in VRAM, adapters are megabytes, hot load/unload | Depends on runtime support for LoRA on quantised layers; adapter quality is gated by eval |

The fourth option was the only one that fit the hardware, but it carried an open
question: does vLLM support LoRA on a `compressed-tensors` AWQ model? That was
resolved empirically before committing to the design.

## Decision

1. **Train with QLoRA** on the official BF16 checkpoint
   (`Qwen/Qwen3-4B-Instruct-2507`, revision `cdbee75f`, pinned in
   `finetune/train_qlora.py`): 4-bit NF4 double-quantised base, bf16 compute,
   LoRA r=16 / alpha=32 / dropout 0.05 on all linear layers. 33.0 M trainable
   parameters (0.81%).
2. **Serve the adapter against the AWQ base**, not a merged checkpoint:
   `--enable-lora --max-lora-rank 16 --max-loras 4`, LoRA delta applied in fp16
   on top of the quantised matmul. Training quantisation (NF4) and serving
   quantisation (AWQ) differ; the adapter is a low-rank correction, and the
   eval gate — not the training loss — decides whether the combination holds.
3. **One alias per adapter.** `slm-triage` and `slm-triage-v2` are separate
   model names behind the same GPU tier. Promotion is a routing change; rollback
   is a routing change back.
4. **Load adapters at runtime** through `/v1/load_lora_adapter` and
   `/v1/unload_lora_adapter` (requires `VLLM_ALLOW_RUNTIME_LORA_UPDATING=1`).
   The serving container starts base-only, so a node can be booted without
   adapter artifacts present.
5. **Adapters are build artifacts, not source.** They are git-ignored and
   reproduced with `make adapter-train`; `training.json` records data, base
   revision, hyper-parameters and the loss curve.

## Evidence (measured on the RTX 4080)

Runtime support was tested before training: a zero-effect probe adapter with
PEFT-style `q_proj`/`o_proj` tensors loaded onto the AWQ base with vLLM 0.33.0
and served under its own model name. vLLM's LoRA wrappers read
`weight_packed`/`qweight` and apply the delta through the layer's quantisation
method, which is why compressed-tensors and GPTQ/AWQ layers work.

Held-out set: 200 tickets (80 clean paraphrases, 80 adversarial paraphrases, 40
tickets from four scenario families never seen in training).

| Model | Strict JSON | Schema | All labels exact | Category | Priority | Sentiment | Escalate | Product | Clean | Hard | Held-out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| base `slm-small` | 100% | 99.0% | **19.5%** | 80.0% | 70.0% | 81.0% | 76.0% | 38.0% | 23.7% | 15.0% | 20.0% |
| adapter v1 | 100% | 99.0% | **73.5%** | 98.5% | 85.0% | 86.5% | 93.0% | 96.0% | 77.5% | 57.5% | 97.5% |
| adapter v2 | 100% | 100% | **79.0%** | 97.0% | 100% | 86.0% | 93.0% | 97.0% | 85.0% | 76.2% | 72.5% |

Reading:

- The base model already emits JSON, but invents taxonomy (`"category":
  "security"`, `"feature request"`) and gets product extraction wrong 62% of the
  time. Fine-tuning is about the contract, not the syntax.
- v1, trained on clean tickets only, reaches 57.5% on adversarial input and
  misreads sarcasm as positive sentiment.
- v2, trained on clean plus adversarial tickets, reaches 76.2% on the hard
  slice and 100% schema. Its held-out dip (72.5%) is concentrated in two
  paraphrases where the generated label is genuinely arguable (a neutral
  "cancel the late order" and "Hello and bonjour ... the line is stopped"),
  not in missing structure.
- Training loss fell below 1e-5 within two epochs in both runs, i.e. it
  memorised the templates. That is exactly why promotion is decided by the
  held-out eval and not by loss.

Serving both adapters costs ~590 MiB of VRAM on top of the base tier
(9,975 → 10,563 MiB of 12,282), leaving room inside the 16-sequence KV budget.

## Consequences

**Positive:** the GPU holds one base and several adapters; a canary can send
one tenant to `slm-triage-v2` while another stays on `slm-triage`; rollback is
`unload_lora_adapter` (verified: alias returns 404, reload restores 200); no
weights are rebuilt to change behaviour.

**Negative:** adapters cannot fall back to the CPU simulator, which has no LoRA
support. Adapter aliases therefore declare no fallback and fail fast rather than
lying to the caller. Runtime-loaded adapters disappear on restart; `make up`
re-runs `scripts/load_adapters.py`. A production release should bake adapter
references into the release manifest and preload them at startup.

**Risk recorded:** NF4 training on BF16 weights and AWQ serving weights are not
the same numerics. The eval set is the guardrail, and it is small: 200 tickets,
generated. Phase 4 replaces it with a reviewable golden set and adds a
canary/rollback gate on top.

**Revisit when:** adapter count exceeds `--max-loras`, an adapter needs to
change base behaviour rather than style/format, or a tenant's eval shows adapter
quality below its floor — then merge and re-quantise per tenant release.
