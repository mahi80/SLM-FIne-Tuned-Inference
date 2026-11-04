# ADR-0005 — Benchmark closed loops, gate on a hand-written golden set, cost ownership explicitly

- **Status:** Accepted
- **Date:** 2026-11-04
- **Deciders:** Platform architecture
- **Related:** ADR-0002 (quantisation), ADR-0003 (adapters), ADR-0004 (policy router)

## Context

Phase 3 made routing explainable, but three numbers were still being asserted rather than
measured:

- throughput under more than one concurrent client, which is the only regime a serving
  platform is bought for;
- whether a model release is good enough to promote, on data the model was not tuned on;
- what the owned GPU costs per token once there is no invoice to point at.

The generated eval set from Phase 2 is 200 tickets, but its wording repeats the training
templates' vocabulary. A model that memorised the templates can look good on it. The RTX
4080 has a measured single-stream decode rate but no aggregate figure. The policy carries
`cost_per_1k_out: 0.0` for the GPU because ownership is not a per-token price.

## Options considered

### Benchmark method

| Option | Where it wins | Where it loses |
|---|---|---|
| Single-stream median | Simple, comparable to vendor charts | Ignores batching, the reason to buy a GPU server; queueing invisible |
| Fixed-concurrency closed loop | Matches how clients actually hold connections; exposes queueing and throughput saturation | Not a Poisson arrival model; coordinated omission if a client stalls |
| Open-loop Poisson | Models real arrival processes | Needs a load driver and a steady-state harness; overkill before Phase 5 |
| **Fixed-concurrency closed loop, per-level, with percentiles** | One number per concurrency level, no hidden batching assumption | Still a laptop, still short runs |

### Eval data

| Option | Where it wins | Where it loses |
|---|---|---|
| Generated eval only | 200 rows, exact labels, reproducible | Vocabulary overlaps training; memorisation can pass |
| LLM-as-judge on new tickets | Scales to thousands | Non-deterministic, needs a judge model and its own validation |
| **Hand-written golden set + generated set** | Reviewable labels, new wording, deterministic checks | Small: 24 tickets is a regression detector, not a benchmark |

### Cost basis

| Option | Where it wins | Where it loses |
|---|---|---|
| Use list price for the GPU | Simple | The GPU is owned; list price is not the bill |
| Ignore GPU cost | Honest about marginal cost | Cannot answer "is the GPU worth buying?" |
| **Amortised ownership + measured throughput** | Same unit as the modelled tiers; break-even is computable | Sensitive to capex, utilisation and the measured throughput |

## Decision

1. **Benchmark in closed loops at concurrency 1, 4 and 12**, 20 requests per level (8 for
   the CPU tier), two warm-up requests, streaming with usage. Report request rate, aggregate
   output tokens/s, TTFT and total latency p50/p95/p99, and errors. A run without its
   concurrency is not a result.
2. **Label the source from the response, not the config.** `x-simulated: true` marks a run
   MODELLED; a measured run without it is reported as measured. The same driver measures the
   GPU, the models and the gateway aliases.
3. **Gate releases on a 24-ticket hand-written golden set** (`src/eval/golden.jsonl`):
   eight clean, eight adversarial and eight held-out tickets with new wording and labels
   taken from the rubric in `finetune/prompt.py`. The gate combines absolute floors
   (strict JSON, schema, exact labels, per-slice exact) with drift against a recorded
   baseline. A release must clear both.
4. **Cost ownership explicitly.** Convert capex, power and operations into an hourly cost,
   divide by the measured aggregate throughput at a stated utilisation, and report dollars
   per million output tokens. Compare that with the modelled CPU and cloud profiles and
   compute break-even volume.
5. **Assumptions live in config, results in artifacts.** `config/bench.yaml`,
   `config/eval-gates.yaml` and `config/cost.yaml` hold the inputs; `bench/results/*.json`
   hold what was measured, including the golden-set hash.

## Evidence (measured on the RTX 4080 host)

Same prompt, 64 output tokens, streaming, two warm-ups per level:

| Target | Source | c | Output tok/s | TTFT p50 | Total p50 | Errors |
|---|---|---:|---:|---:|---:|---:|
| GPU | measured | 1 | 125.5 | 20 ms | 509 ms | 0 |
| GPU | measured | 4 | 484.8 | 27 ms | 524 ms | 0 |
| GPU | measured | 12 | **1,123.2** | 53 ms | 569 ms | 0 |
| adapter v2 | measured | 1 | 101.6 | 28 ms | 570 ms | 0 |
| adapter v2 | measured | 4 | 389.2 | 49 ms | 590 ms | 0 |
| adapter v2 | measured | 12 | 850.9 | 128 ms | 683 ms | 0 |
| cloud L4 profile | MODELLED | 12 | 788.4 | 30 ms | 810 ms | 0 |
| CPU Xeon profile | MODELLED | 12 | 122.2 | 121 ms | 4,185 ms | 0 |

The GPU scales 9x from one to twelve clients while p50 total latency moves 509 → 569 ms.
The adapter costs roughly 15–25% of aggregate throughput. The CPU profile saturates near
122 tok/s, which is why it is a deadline tier rather than a cheap GPU.

Golden set, 24 tickets, temperature 0, through the gateway:

| Model | Strict JSON | Schema | Exact | Clean | Hard | Held-out | Gate |
|---|---:|---:|---:|---:|---:|---:|---|
| base `slm-small` | 100% | 100% | **4.2%** | 0.0% | 0.0% | 12.5% | fails floors |
| adapter v1 | 100% | 91.7% | **75.0%** | 100% | 62.5% | 62.5% | fails schema floor |
| adapter v2 | 100% | 100% | **83.3%** | 87.5% | 100% | 62.5% | **PASS** |

v2's four remaining disagreements are genuine: a thank-you classified P3 instead of P4, a
refund chase where the word "escalate" pulled `escalate` true despite P3, a licence transfer
read as cancellation, and a two-site outage read as P2 instead of P1. All four are label
errors, not schema errors, which is the distinction the gate is meant to surface.

Cost at 700 input + 90 output tokens, 20,000 requests/day:

| Tier | Source | $/1M output (capacity) | $/1M at workload | $/month |
|---|---|---:|---:|---:|
| owned GPU, 35% utilisation | measured | **$0.117** | **$2.243** | **$121.11** |
| cloud L4 profile | MODELLED | $0.797 | $0.797 | $43.02 |
| CPU Xeon profile | MODELLED | $15.80 | $15.80 | $853.20 |

Owned GPU assumptions: $2,600 capex over 36 months, 180 W at PUE 1.4, $0.18/kWh, 15% ops
overhead. At 20k requests/day the fixed bill is spread over 54M output tokens/month, so
owned is more expensive than cloud. Break-even against the L4 profile is **56,069
requests/day**; against the CPU profile, **2,839**. The node's full capacity at the measured
1,123 tok/s is about 1.08M requests/day, so the break-even volume is feasible even at 35%
utilisation.

## Consequences

**Positive:** throughput claims now carry their concurrency and source; the release gate is
deterministic and separates schema failures from label errors; the fixed-versus-marginal
cost question is answered in the same unit the router uses.

**Negative:** the benchmark is closed-loop on a laptop, not an arrival-process simulation on
datacentre hardware. The golden set is 24 tickets, so one label flip moves the exact rate by
4.2 points; the drift allowance is 5 points for that reason. Cost per token depends on
utilisation and capex assumptions that are local, not universal.

**Risk recorded:** the GPU throughput is aggregate over a 64-token generation and includes
prefill, but the cost model reuses it for a 90-token workload without modelling the different
prefill/decode ratio. The error is small at this prompt length; Phase 5 should split prefill
and decode if the workload mix changes materially.

**Revisit when:** arrival distributions matter (open-loop harness), the golden set is
expanded past ~200 tickets or needs multiple annotators, or a datacentre GPU replaces the
laptop and the capacity assumptions no longer hold.
