# ADR-0004 — Admission-first policy router, deterministic canary, hash-chained audit

- **Status:** Accepted
- **Date:** 2026-11-04
- **Deciders:** Platform architecture
- **Related:** ADR-0003 (adapters), ADR-0002 (quantisation), ADR-0001 (runtime)

## Context

Phases 0–2 proved the compute tiers, the OpenAI-compatible gateway and two QLoRA
adapters. Routing was a static alias table with one connection-level fallback. The
job the platform promises — place each request on the right tier, inside the
customer's legal perimeter, and prove why — cannot be expressed as a table.

Constraints:

- **Residency and data class are hard constraints.** A cheaper or faster backend
  that cannot legally serve a tenant must not receive traffic, including during an
  outage. A "best effort" fallback chain violates the contract.
- **The GPU is a single point of failure.** CPU and cloud standbys must take over
  without a redeploy, and a recovering backend must not be stampeded.
- **A release must be comparable and reversible.** Two adapter versions are
  already served side by side; the router needs a canary that is stable per
  tenant, not a per-request coin flip.
- **Audit is a deliverable, not a log line.** Reviewers ask why a request went to
  a tier, and whether the record can be altered after the fact.
- **No extra infrastructure for the reference build.** A service mesh, Redis and
  a policy sidecar would each be defensible in production, but they would hide the
  routing logic behind a dependency the reader cannot run on a laptop.

## Options considered

### Routing

| Option | Where it wins | Where it loses |
|---|---|---|
| Per-tenant static routes | Trivial to reason about | No failover, no canary, no residency model; every change is a config deploy |
| Weighted random canary with a shared counter | Exact traffic split | Needs Redis or a control plane; per-request flipping; split-brain across replicas |
| Envoy / service mesh header routing | Mature traffic control, outlier detection | Router has no tenant data class, region or context budget; policy lives in two places |
| OPA / policy sidecar | Reusable policy language | Extra hop and process on the request path; policy input still assembled by the gateway |
| **In-process admission + scoring, policy as data** | One place to reason and explain; sub-millisecond; no new service | Policy language is Python plus YAML; breakers are per replica |

### Audit

| Option | Where it wins | Where it loses |
|---|---|---|
| Application log | Zero cost | No integrity; rotation loses order; deletion is silent |
| Database table | Queryable, transactional | Adds infrastructure; privileged writers can still rewrite rows |
| External transparency log | Strong tamper resistance | Network dependency on the request path; overkill for a reference build |
| **Hash-chained JSONL, head hash exportable** | Verifiable offline, one file, no dependency | Single writer; tamper *evidence*, not prevention |

### Breakers

Envoy's outlier detection is per upstream host. The unit that matters here is
`alias:candidate`: the same GPU serves the base model and two adapter versions,
and a broken adapter should not remove the GPU for the base alias. Per-replica
in-process breakers were chosen; sharing state is a Phase 5 concern when the
gateway scales beyond one replica.

## Decision

1. **Policy is versioned data** in `config/policy.yaml`. An alias names a
   primary and a set of candidates. A candidate carries tier, backend, upstream
   model, region, `serves_regions`, `sovereign`, priority, `max_context`, cost and
   model version. Unknown keys are rejected, so a typo cannot silently disable a
   guardrail.
2. **Admission before scoring.** The router evaluates, in order: tenant alias
   allowlist, tenant tier allowlist, cloud permission, region, data class vs
   sovereignty, context budget, breaker state. A failed candidate is not a
   fallback. `confidential` and `restricted` tenants never reach a non-sovereign
   candidate.
3. **One primary, optional canary, scored standbys.** Standbys are scored by
   `priority*100 + cost_per_1k_out*1000 + failure_rate*50`. The primary is used
   whenever admissible; this keeps routine routing stable and makes the standby
   path attributable to an outage.
4. **Canary assignment is deterministic and stateless.**
   `blake2b("canary:<alias>", tenant-or-request-id) mod 100 < percent`. Sticky
   per tenant by default. The same input yields the same bucket on every replica
   and across restarts. Raising the percentage promotes; setting it to 0 rolls
   back. Shadow sampling uses an independent bucket on `request_id`.
5. **Circuit breakers are per `alias:candidate`.** Three failures in 60 s open
   the breaker for 20 s; half-open admits exactly one probe; success closes,
   failure reopens. Connection errors, timeouts and 5xx count; 4xx does not.
6. **Transport fallback walks the decision's attempt list**, built from the same
   admission pass. The fallback can never be a candidate the policy vetoed.
7. **The audit log is a hash chain.** One JSONL record per inference and one per
   shadow request; `hash = sha256(canonical_json(record without hash))`; `prev`
   links to the predecessor. Recovery verifies the chain at startup, drops only a
   torn final line, and fails closed on any earlier corruption. Verification is
   an endpoint.
8. **Reload is atomic.** The file is parsed and validated into immutable
   objects, then swapped with its breaker configuration; breaker *state* survives
   because health is orthogonal to intent. An invalid file returns 422 and the
   active policy keeps serving.

## Evidence (measured on the RTX 4080)

**Outage drill.** With `vllm-gpu` stopped:

| Request | Tenant | Result | Header |
|---|---|---|---|
| 1–3 | acme, eu / confidential | CPU tier | `policy:primary:cpu:fallback`, `x-fallback: true` |
| 4 | acme, eu / confidential | CPU tier | `policy:standby:cpu`, `x-fallback: false` |
| 1–4 | globex, us / internal | Cloud tier (MODELLED) | `policy:standby:cloud-us` |

The acme audit record carries `vetoed: ["cloud-us:region-veto"]` even though the
cloud tier is cheaper; globex has no veto and the cloud candidate wins on score.
The breaker is keyed by alias, so acme's three failures opened the GPU for
globex too — shared health, separate admission.

**Canary.** Measured buckets: acme 67 (v1), globex 37 (v2). Headers:
`x-route-reason: policy:primary:v1` / `x-model-version: triage-v1` for acme;
`policy:canary:v2` / `x-canary: true` / `x-model-version: triage-v2` for globex.

**Reload and rollback.** An invalid YAML append returned **422** while the old
policy continued serving. Setting `percent: 50` to `percent: 0` and reloading
moved globex to `policy:primary:v1` on the next request. Restoring the file and
reloading returned the canary; the file was byte-identical afterwards.

**Shadow.** A forced request id (`shadow-13`, bucket < 5) produced
`x-shadow: v2`, a `shadow` audit record with status 200, and
`gateway_shadow_requests_total{alias="slm-triage",candidate="v2",status="200"} 1`.

**Audit.** `GET /v1/audit/verify` → `{"ok": true, "records": 65, ...}`. The unit
suite (24 tests) covers threshold/cooldown/half-open behaviour, residency and
sovereignty vetoes, alias denial, context exhaustion, canary stability, tamper
detection, torn-tail recovery and atomic reload.

## Consequences

**Positive:** residency and data class are enforced before any score, so a cheap
cloud tier cannot win traffic it must not serve. Canary and rollback are file
edits plus a reload. Breakers isolate one alias's failure from another on the same
hardware. Every decision is explainable through the same code path that made it,
and every request has a verifiable audit record.

**Negative / limits, stated rather than hidden:**

- Breakers and rate limits are **in-memory per gateway replica**. Multi-replica
  deployments need shared health and quota state (Phase 5).
- A tenant-sticky canary is not a request-level percentage. With two tenants the
  split is 0/50/100%, not 50% of requests. That is intentional for version
  stability but must not be reported as a traffic percentage to a release gate.
- The hash chain is tamper **evidence**. The head hash must be shipped to an
  append-only sink (syslog, object-lock bucket) for tamper resistance.
- Audit writes are fsync'd per record but **fail open** with a loud log: a full
  disk does not take inference down. A regulated deployment should fail closed
  or spool to a durable local queue.
- Shadow traffic costs real capacity (5% of `slm-triage` primary traffic, capped
  at 64 tokens). It is a sampling tool, not a free comparison.
- Policy reload reads a local file. Kubernetes/OpenShift distribution (ConfigMap
  projection, checksum annotation, or a control-plane push) is Phase 5.

**Revisit when:** the gateway scales past one replica (shared breakers and quota),
quality-aware scoring replaces static priority/cost (Phase 4), or a tenant needs
guaranteed capacity rather than best-effort standbys — then admission grows a
reservation model and the scorer becomes capacity-aware.
