# Operations

What "healthy" means, what pages someone, and what to do when it does not. Targets are
**proposed** from the measurements in the README; they are not a contracted SLA. Every
runbook names the signal, the action and the evidence to attach to the incident.

## Service levels

| SLI | Definition | Proposed target | Observed here |
|---|---|---|---|
| Availability | Gateway returns a non-5xx response to a valid request | 99.5% monthly, excluding announced single-replica maintenance | Not measured over time; single host |
| TTFT p95 (GPU) | Time to first streamed token, `alias=slm-small, tier=gpu` | < 250 ms | 22 ms at c=1, 53 ms at 12 clients |
| End-to-end p95 (GPU) | 90-output-token request | < 2 s | 0.51 s at c=1; 0.74 s median through the gateway |
| Error rate | 5xx / total, per tier | < 0.5% | 0 in the benchmark runs |
| Refusal rate | `gateway_policy_refusals_total` / total | < 0.5%, and every refusal is explained in audit | Measured 0 outside the deliberate drills |
| Audit completeness | Inference and shadow records present for every request | 100% | Best effort: a failed append is logged, not retried (R-14) |
| Gate freshness | Age of the deployed release's gate report | < 30 days | Recorded in the signed manifest |

The single gateway replica makes the availability target honest only if maintenance is
scheduled. A pod restart is brief downtime, not a rolling upgrade.

## Signals and alerts

Metrics come from the gateway (`gateway_*`), the simulators and vLLM. All alerts below are
PromQL against the provisioned Prometheus; the dashboard **SLM Serving Fabric** already plots
the first seven series.

| Alert | Expression | For | Meaning | Runbook |
|---|---|---|---|---|
| `GpuTierDown` | `up{job="vllm"} == 0` | 1m | GPU tier unreachable; traffic is on standbys | RB-01 |
| `BreakerOpen` | `max by (alias, candidate) (gateway_breaker_state) == 2` | 5m | A candidate is breaker-open | RB-02 |
| `Gateway5xxRate` | `sum(rate(gateway_requests_total{status=~"5.."}[5m])) / sum(rate(gateway_requests_total[5m])) > 0.01` | 10m | >1% server errors | RB-01, RB-02 |
| `GpuLatencyHigh` | `histogram_quantile(0.95, sum by (le) (rate(gateway_request_duration_seconds_bucket{tier="gpu"}[5m]))) > 2` | 10m | p95 over 2 s | RB-09 |
| `RefusalsSpiking` | `sum by (alias, reason) (rate(gateway_policy_refusals_total[5m])) > 0.01` | 10m | A policy is refusing traffic | RB-05 |
| `PolicyReloadFailed` | `increase(gateway_policy_reloads_total{result!="ok"}[10m]) > 0` | 1m | A reload was rejected (422 keeps the old policy) | RB-05 |
| `FallbackSpike` | `sum(rate(gateway_fallback_total[5m])) > 0.1` | 10m | Sustained fallback to a standby | RB-01 |
| `AuditChainBroken` | `GET /v1/audit/verify` returns `ok: false` | — | Tampering or corruption | RB-04 |
| `AuditWriteFailing` | *not exposed yet* — add `gateway_audit_write_failures_total` | — | Requests may be unrecorded | RB-04, C-04 |
| `CertExpiry` | Route/Ingress certificate < 14 days | — | Edge TLS expiry | RB-08 |

The audit check is not a metric today; it is a probe the operator or an external monitor
calls. That is deliberate honesty in the table above, not an omission to paper over.

## Runbooks

### RB-01 — GPU tier unavailable

**Signal:** `GpuTierDown`, `FallbackSpike`, or `x-tier` flipping to `cpu` in audit.
**Expected behaviour:** the first three requests fail over at transport level
(`x-fallback: true`); the breaker opens; subsequent requests select an admissible standby.
EU confidential tenants go to the CPU tier; US internal tenants may use the cloud standby.

1. Confirm the scope: `kubectl -n slm-fabric get pods -l app.kubernetes.io/name=vllm-gpu` (or
   `docker compose ps vllm-gpu`).
2. Check the cause: driver/host reboot, OOM-killed pod, GPU in use by another process. The
   container log is the first place; VRAM pressure appears as an engine init failure.
3. Do **not** disable tenant policy to force traffic to the cloud tier. Residency rules are
   the reason standbys exist; changing them during an incident is how a data-residency
   incident happens.
4. If the outage will exceed the SLO, announce it and record the affected window; the audit
   trail shows which tier served every request.
5. After recovery, confirm the breaker returns to closed (state `0` in `GET /v1/policy`) and
   adapters are loaded (`GET /v1/models` lists `triage-v1` and `triage-v2`).

**Evidence to attach:** audit records for the first failing and first recovered request, the
breaker state timeline, and the tier distribution during the outage.

### RB-02 — Breaker open longer than expected

**Signal:** `BreakerOpen` for 5 minutes after the upstream is healthy.

1. `GET /v1/policy` (admin key) → inspect `breakers` for the open key.
2. If the upstream responds to a direct `GET /health` but the breaker stays open, check the
   gateway's view: a DNS or port change in the Service will keep the breaker open while the
   pod looks healthy.
3. Half-open admits exactly one probe; if that probe fails, the breaker reopens. A failure
   caused by a single bad request (for example a context-length 4xx) should not count — 4xx
   does not open the breaker. If it did, treat it as a bug and capture the request id.
4. Only restart the gateway if the breaker registry is corrupt; a restart also resets quota
   windows and the audit process. Prefer waiting out `open_s` (20 s).

### RB-03 — Failed release gate

**Signal:** Argo CD sync failed; `release-gate` job in `Failed`; the gate report in the job
log lists the failing checks.

1. Do not raise the threshold to make the gate pass. Thresholds are in
   `config/eval-gates.yaml` and changing them is a reviewed decision, not an incident action.
2. Identify whether the regression is in the adapter or the harness: run `make eval-gate`
   from the gateway against the deployed alias and compare with the job's report.
3. Roll back the adapter: set `canary.percent: 0` for the alias (or load the previous
   adapter name) and reload the policy. Adapters are loaded by name, so v1 remains available
   while v2 is evaluated.
4. Re-run the gate before re-promoting. Attach both reports to the incident; the manifest for
   the failed release is evidence of what was attempted.

Automatic rollback is condition C-08; today this is a human step.

### RB-04 — Audit verification fails

**Signal:** `GET /v1/audit/verify` returns `ok: false` with a `first_bad`/detail field.

1. **Stop** and preserve the file. Copy `audit.jsonl` and the last known-good head hash
   before anything writes to it.
2. Determine the nature: a torn tail (crash between write and newline) is recovered on
   restart by truncating the last partial line. A mismatch mid-file is tampering or storage
   corruption.
3. Compare the reported sequence against the gateway log and the last external head hash, if
   one was shipped. Without an external anchor, the local chain cannot distinguish a
   malicious rewrite from corruption — that is R-01.
4. Restart only after preserving the copy. The gateway refuses to start on a broken chain and
   will not silently continue.
5. If the cause is disk exhaustion or permissions, fix storage first; requests served during
   that window may be missing from the log (R-14).

### RB-05 — Bad policy push

**Signal:** `PolicyReloadFailed`, `RefusalsSpiking`, or an unexpected tier in responses.

1. A rejected reload returns **422** and the previous policy keeps serving. Read the error;
   it names the failing field.
2. A reload that succeeded but is wrong is worse than a rejection. Use
   `POST /v1/policy/explain` with the affected tenant and alias to see the admission trace
   before and after the edit.
3. Revert the commit and reload. Policy is a file, so rollback is `git revert` plus reload —
   no image rebuild.
4. If the bad policy permitted cloud egress for a restricted class, treat it as a data
   incident: the audit records the vetoes and the tier that served each request.

### RB-06 — Adapter regression without a gate failure

**Signal:** tenant complaints, or a shifted distribution in shadow comparisons; the gate is
green.

1. Pull the audit records for the alias in the window; they name the model version and
   candidate that answered.
2. Compare the served adapter digest with the signed manifest:
   `python -m tools.validate_release` reports adapter drift when the weights are present.
3. Shadow traffic (`x-shadow`) exists for exactly this case: if v2 shadow output is worse and
   the gate missed it, the golden set has a coverage gap — open an eval-set issue, do not
   relax the gate.
4. Roll back with `canary.percent: 0`, then re-evaluate with additional representative
   tickets.

### RB-07 — Audit volume full

**Signal:** gateway error logs `audit append failed`; disk usage on the audit PVC high.

1. Increase the PVC or move the file; do not delete history. Truncation breaks the chain.
2. Requests continue to be served while writes fail (by design). Record the window so the
   missing records are explainable.
3. Retain according to the tenant's compliance requirement; the chain must be archived as a
   whole from genesis, not in arbitrary slices.

### RB-08 — Signing key or TLS material compromised

1. Signing key: stop releases, generate a new keypair with `make release-key --force`,
   publish the new public key, re-sign the current release, and mark the previous public key
   as compromised in the trust bundle. Manifests signed with the old key remain
   cryptographically valid — revocation is a trust-bundle decision.
2. Edge certificate: rotate via the Route/Ingress; verify streaming is not broken by the
   proxy's idle timeout (300 s is set for long generations).
3. Record the rotation in the sign-off pack; it is a review trigger for the threat model.

### RB-09 — Capacity or latency degradation

1. Check `gateway_inflight_requests` and per-tier throughput. A sustained in-flight plateau
   with flat output tokens/s means the upstream is saturated, not the gateway.
2. Compare with the benchmark baseline: `bench/results/tier-comparison.json` gives the
   measured curve (125 → 1,123 output tok/s from c=1 to c=12).
3. Verify no shadow traffic is consuming the GPU budget; shadow is capped at 5% and 64
   tokens, but a misconfiguration can raise it.
4. If the load exceeds the owned node, the cost model's break-even (~56k requests/day vs the
   cloud L4 profile) is the decision input — not an ad-hoc cloud enablement.

## Backup and restore

| Item | Backup | Restore | Tested |
|---|---|---|---|
| Routing policy | Git | Revert + reload | Yes, in drills |
| Tenant keys | Secret in the cluster's secret store (not Git) | Recreate with `make deploy-secret` | No |
| Audit trail | Volume snapshot + external head hash | Restore file; verify chain | No |
| Model weights | Re-download (connected) or mirror (air gap) | Seed the model PVC | Partially (connected) |
| Adapter weights | Retrain (`make adapter-train`) or artifact registry | Seed the adapter PVC | No |
| Signed release | Git | `make release-check` | Yes |

The two "No" rows matter: a restore drill is condition C-04 for audit and part of C-01 for
the cluster.

## Ownership

| Area | Owner | Escalation |
|---|---|---|
| Gateway, router, audit | Platform engineering | Architecture review for policy semantics |
| GPU tier and model quality | ML platform | Model engineer for adapter regressions |
| Cluster, GitOps, admission | Platform operations | Security for policy exceptions |
| Tenant access and residency | Service owner per tenant | Compliance / DPO |
