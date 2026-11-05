# ADR-0011 — Latency and cost are declared admission inputs, not comments

- **Status:** Accepted
- **Date:** 2026-11-05
- **Deciders:** Platform architecture
- **Related:** ADR-0004 (policy router), ADR-0005 (benchmarks, eval and cost),
  ADR-0008 (capacity pools), ADR-0009 (external backends)

## Context

Every tier already knew how fast it was and what it cost — in prose. The GPU benchmark table
recorded a 509–569 ms p50 total latency, the CPU and cloud profiles were calibrated
simulators with a price per million tokens, and `src/cost` turned GPU ownership into the same
unit. None of that reached admission. A tenant could only say *which* tiers it allowed, never
*how slow* a route was permitted to be, and a candidate could declare any `cost_per_1k_out`
it liked with nothing checking it against the pool it named.

That gap produced two failures a reviewer would catch and a router would not:

- **Latency was a preference, not a constraint.** A CPU standby at 2.5 s was a perfectly good
  fallback for a caller whose deadline was one second. The router had no way to know, so it
  scored it and sent the request.
- **Cost was a free-text field.** The figure that drives scoring and the audit's
  `est_cost_usd` lived only in `config/policy.yaml`. Change it in one place and the two
  silently disagreed; the registry that describes the pool said nothing about what a token
  of its output costs.

Adding a per-candidate latency and cost block to the policy would have fixed the first
symptom and recreated the second: the same number declared N times, drifting N ways.

## Decision

1. **The pool declares the SLO, once.** `config/capacity.yaml` (now version 3) gains a `slo`
   block per pool: `expected_latency_ms` (positive integer, the p95 the pool is sized for),
   `cost_per_1k_out` (non-negative, USD per thousand output tokens) and `cost_source`
   (`measured` or `modelled`). The registry is already the one place a route may point, so it
   is the right place for the number a route is judged on.
2. **The candidate restates it, and the validator makes the copies identical.** A candidate
   may declare `latency_ms`; the deploy validator requires it to equal the pool's
   `expected_latency_ms` exactly, and requires `cost_per_1k_out` to match within 1e-9. A
   mismatch fails the render with both values and both names in the message. The policy also
   gains the pool's cost source through the registry check: a `modelled: true` pool may not
   claim `measured`.
3. **Latency is a tenant budget, not a global target.** A tenant may declare
   `latency_budget_ms` (0 = no budget). Admission gains one veto,
   `latency-budget-exceeded`, evaluated after `context-exceeded` and before the breaker
   check, so a deterministic reason is recorded when several rules would reject the same
   candidate. It fires only when the budget is positive, the candidate declares a positive
   latency, and that latency exceeds the budget. A candidate that declares nothing is never
   vetoed on latency, which keeps small test policies and older policy files admissible.
4. **The decision is auditable.** The audit record carries `latency_budget_ms` beside the
   existing decision fields, and every veto keeps its reason in `vetoed`, so a refusal can be
   explained from the log after the fact. `/v1/policy/explain` shows the same trace without
   spending a request.

Shipped values:

| Pool | `expected_latency_ms` | `cost_per_1k_out` | `cost_source` |
|---|---:|---:|---|
| `onprem-gpu` | 900 | 0.0 | `measured` |
| `onprem-cpu` | 2500 | 0.0088 | `modelled` |
| `cloud-burst` | 1500 | 0.00033 | `modelled` |

**What these numbers are, honestly.** The 900 ms GPU figure is a target calibrated from the
committed GPU benchmark runs — p50 total 509 ms at one client, 569 ms at twelve, p95 under a
second — not a live p95 read from production. The CPU and cloud figures come from the
calibrated profiles in `src/tiersim/profiles.py`; no Xeon and no L4 was benchmarked for this
repository, which is exactly what `cost_source: modelled` says. The veto therefore compares
a declared target against a declared budget. It does not measure the request it is about to
send, and it cannot catch a pool that is currently slower than its own SLO — that is what the
breakers, the alerts and the benchmark harness are for. GPU marginal cost is 0.0 because the
node is owned; ADR-0005 prices the ownership per token separately rather than pretending the
tokens are free.

## Consequences

- **A budget now refuses instead of degrading.** A tenant with a 1000 ms budget gets the GPU
  or a 503 with `latency-budget-exceeded` per candidate, never a 2.5 s answer it did not ask
  for. Latency-tolerant work keeps the CPU tier by leaving the budget at 0 or raising it.
- **One number, one owner.** Latency and cost changed in `config/capacity.yaml` now fail CI in
  `config/policy.yaml` until both agree, so the registry, the policy and the scoring all
  describe the same pool. The cost field also stops being an unchecked constant.
- **The registry grew a contract.** Adding a pool means declaring an SLO, and a modelled pool
  cannot claim measured numbers. That is one more thing to fill in, deliberately: a pool with
  no latency target cannot be admitted against a budget.
- **Backward compatible, at the cost of a silent default.** `latency_ms` defaults to 0 and
  `latency_budget_ms` defaults to 0, so existing policies and tenant files load unchanged.
  The price is that an undeclared latency is invisible to admission: a candidate with no
  number passes every budget. The validator closes that hole for the shipped policy by
  requiring the field, but a hand-written policy can still omit it.
- **The signed release is stale until it is rebuilt.** The policy's version and digest are
  pinned in the release manifest, so bumping the policy makes the committed release fail its
  drift check on purpose. The re-sign is a separate, reviewed step (`make release`), not a
  side effect of editing a routing rule.
- **Not done here.** No per-request cost meter for external providers, no live p95 feedback
  into the target, and no quality-aware scoring. Cost still enters the ranking only through
  the static `cost_per_1k_out` term, and the audit records an estimate, not an invoice.
