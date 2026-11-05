# Risk register

Severity is likelihood × impact, judged for a regulated enterprise tenant — not for a demo.
Each entry says what exists today, what is left after mitigation, and which condition in
[acceptance.md](acceptance.md#conditions) closes it.

| ID | Risk | Likelihood | Impact | Severity | Closes with |
|---|---|---|---|---|---|
| [R-01](#r-01--audit-chain-is-evidence-not-prevention) | Audit chain is tamper-evident, not tamper-proof | Medium | High | **High** | C-04 |
| [R-02](#r-02--image-digests-depend-on-registry-retention) | A registry can lose or withhold a pinned digest | Low | Medium | **Medium** | C-02 (closed) |
| [R-03](#r-03--upstream-artifact-compromise) | Pinned upstream artifact is compromised | Low | High | **High** | C-02 |
| [R-04](#r-04--static-bearer-keys) | Static bearer keys, no rotation or OIDC | Medium | High | **High** | C-03 |
| [R-05](#r-05--no-in-cluster-mtls) | No mTLS between gateway and tiers | Low | Medium | Medium | C-01 |
| [R-06](#r-06--prompt-content-is-not-inspected) | No DLP or runtime guardrail on prompt content | Medium | High | **High** | C-07 |
| [R-07](#r-07--tenant-policy-misconfiguration) | A tenant policy edit can permit cloud egress | Medium | High | **High** | C-01, C-03 |
| [R-08](#r-08--gateway-is-a-single-writer) | Gateway is a single writer and single point of failure | Medium | Medium | Medium | C-05 |
| [R-09](#r-09--no-live-openshift-validation) | OpenShift-only paths unvalidated on a real cluster | Medium | High | **High** | C-01 |
| [R-10](#r-10--modelled-cpu-and-cloud-numbers) | CPU and cloud numbers are modelled | High | Medium | **High** | C-06 |
| [R-11](#r-11--no-automatic-rollback-on-gate-failure) | Failed in-cluster gate does not auto-roll back | Medium | Medium | Medium | C-08 |
| [R-12](#r-12--evaluation-set-is-small-and-synthetic) | Evaluation set is small and synthetic | High | High | **High** | C-07 |
| [R-13](#r-13--adapter-artifacts-are-not-committed) | Adapter weights are not in the repository | Medium | Medium | Medium | — (by design) |
| [R-14](#r-14--audit-failure-does-not-fail-the-request) | Audit write failure does not fail the request | Low | Medium | Medium | C-04 |
| [R-15](#r-15--single-runtime) | Only vLLM is evidenced | Medium | Medium | Medium | — |
| [R-16](#r-16--no-dependency-or-image-scanning) | No dependency or image scanning in CI | Medium | High | **High** | C-09 |

---

### R-01 — Audit chain is evidence, not prevention

**What exists.** One JSONL file, `sha256` over canonical record including the previous hash,
fsync per record, torn-tail recovery, verification endpoint. Any edit, deletion or reorder
breaks verification from that point.

**Residual.** A process with write access to the volume can rewrite the chain and recompute
every hash. There is no external anchor.

**Closure.** Ship the head hash per interval to an object-lock bucket or append-only syslog
and verify against it (C-04). The gateway already exposes the head at `/v1/audit/verify`.

### R-02 — Image digests depend on registry retention

**What exists.** All four workload images are pinned by digest in the Kustomize base and in
the signed manifest: upstream `vllm/vllm-openai:v0.33.0@sha256:d825baf9…`, and the three
first-party images `ghcr.io/mahi80/slm-{gateway,tiersim,eval}:0.5.0@sha256:…`, published
from this repository by the images workflow. `validate_deploy` rejects a tag-only or
malformed digest, and `validate_release` compares the rendered digest with the signed one.
The admission policy still rejects `latest`.

**Residual.** A digest proves which bytes are requested, not who produced them. If the
registry withdraws the manifest, pulls fail loudly rather than silently changing; serving
different bytes under the same digest would require breaking SHA-256. The public GHCR
packages and the upstream registry remain trusted third parties (see R-03).

**Closure.** C-02 is closed by A-22. Adding image signatures or attestations and a scanning
proxy remains C-09.

### R-03 — Upstream artifact compromise

**What exists.** Model revision and runtime tag are pinned; every image is pinned by digest
too, and the air-gap mirror has an auditable image list.

**Residual.** Pinning proves immutability, not provenance. A compromised upstream repository
serving different bytes for a pinned revision is not detected locally.

**Closure.** Verify the model snapshot hash against an independently obtained manifest and
mirror images through a scanning proxy (C-09).

### R-04 — Static bearer keys

**What exists.** Per-tenant bearer keys in `config/tenants.yaml` (gitignored), constant-time
comparison, per-tenant quotas and allowlists.

**Residual.** Keys do not expire, cannot be scoped to a workload, and are shared as plain
strings. A leaked key is valid until the file changes and the gateway restarts or the secret
is updated.

**Closure.** OIDC / workload identity with short-lived tokens, plus a key-rotation runbook
(C-03). The tenant resolution point is a single function (`current_tenant`), so this is a
contained change.

### R-05 — No in-cluster mTLS

**What exists.** Default-deny NetworkPolicy with an explicit call graph; only the gateway can
reach the model tiers.

**Residual.** NetworkPolicy is identity by IP/namespace, not cryptographic identity. Anything
that can spoof a pod in the allowed namespace can call vLLM directly, bypassing auth, quota,
policy and audit.

**Closure.** Service mesh or SPIFFE-based mTLS between gateway and tiers (C-01). Note that
direct access to `vllm-gpu:8000` from inside the namespace is already possible today; the
audit trail only covers requests through the gateway.

### R-06 — Prompt content is not inspected

**What exists.** The gateway counts tokens and routes; it does not classify or redact prompt
content. The audit record deliberately stores metadata only — no prompt or completion text —
which is good for privacy and limits content-level forensics.

**Residual.** Secrets pasted into a prompt are forwarded to the selected tier and are not
detected. There is no runtime moderation model. Residency rules protect *where* data goes,
not *whether* the data should have been sent.

**Closure.** Decide the guardrail architecture with the tenant: DLP pre-filter, prompt
redaction, or contractual prohibition plus monitoring. This is condition C-07 and is a
product decision, not a code gap to hand-wave.

### R-07 — Tenant policy misconfiguration

**What exists.** Admission checks `allow_cloud`, region, data class and sovereignty on every
request; every veto is recorded in the audit record.

**Residual.** The policy is data edited by an admin. Setting `allow_cloud: true` and
`data_classes: [internal, confidential]` for an EU tenant would route confidential EU traffic
to the US cloud standby, and the router would be working as configured. Nothing currently
blocks an unsafe combination in `policy.yaml` itself.

**Closure.** Add policy linting at reload (reject `cloud` candidates in an alias serving
`confidential` unless the data class is explicitly permitted), and require two-person review
for policy commits (C-01, C-03). The audit trail makes the misconfiguration visible after
the fact, which is not the same as preventing it.

### R-08 — Gateway is a single writer

**What exists.** `replicas: 1` with `Recreate`; documented as a correctness constraint.
Quotas, breakers and the audit chain assume one process.

**Residual.** A node drain or crash means inferred downtime until reschedule. There is no
horizontal scale-out for availability, and the audit chain has no fencing against a second
writer.

**Closure.** Move quota counters, breaker state and audit sequencing to a shared store
(Redis/Postgres with advisory locking), then scale with a `RollingUpdate` (C-05). Until then,
the single replica is the honest design.

### R-09 — No live OpenShift validation

**What exists.** Three overlays render and pass 60+ static assertions; 43 deployment tests
cover the failure modes that matter; Kyverno policies and Argo CD applications are committed.
A kind v0.33.0 cluster then applied the vanilla overlay, admitted and rejected pods through
Kyverno v1.19.2, reconciled two Applications with Argo CD v3.6.1, and found one real defect
(the gateway tenants Secret mount) that static review had missed.

**Residual.** No OpenShift instance evaluated SCC assignment, Route TLS, ServiceMonitor
discovery, release hooks or the air-gap mirror path. kind's CNI does not enforce
NetworkPolicy and has no ingress controller, so those paths are still unobserved. The new
`make cluster-health` checker is unit tested and was exercised only through its no-cluster
failure path; a healthy run against two tainted node pools is part of C-01.

**Closure.** Execute C-01 on a real cluster and attach the outputs to this pack. Until then,
every OpenShift-specific claim stays marked *static*.

### R-10 — Modelled CPU and cloud numbers

**What exists.** A calibrated simulator with a declared profile, honest queueing (bounded
slots), and `x-simulated: true` on every response. Benchmarks label their source.

**Residual.** CPU latency, throughput and cost are arithmetic, not measurement. The
router's cost trade-off depends on those numbers. A Xeon Platinum or EPYC part with different
memory bandwidth could move the CPU result by a factor, not a percent.

**Closure.** Run the benchmark matrix on a real CPU node and replace the profile (C-06). The
`source` field and the `x-simulated` header make the substitution auditable.

### R-11 — No automatic rollback on gate failure

**What exists.** The gate runs as a PostSync hook; failure marks the sync failed and keeps
the previous adapter loaded (weights are loaded before evaluation, but a regression is not
automatically unloaded). Canary percent is a manual policy edit.

**Residual.** A human must notice the failure, decide, and edit policy or load the previous
adapter. Time to recovery depends on alerting (see [operations.md](operations.md)).

**Closure.** Add an automated promotion controller: on gate failure, reload the previous
policy version and unload the new adapter, then re-run the gate (C-08). The audit record
already carries the version that answered, so the controller has the state it needs.

### R-12 — Evaluation set is small and synthetic

**What exists.** 24 golden tickets (8 clean, 8 hard, 8 held-out) and a 200-ticket held-out
comparison; labels are hand-written from scenario definitions.

**Residual.** 24 tickets give coarse resolution: one ticket moves exact-label accuracy by
4.2 points. The data is generated, so it cannot capture real customer phrasing, adversarial
users, or distribution shift. A model can pass the gate and still fail in production.

**Closure.** Replace or supplement with tenant-representative, consented data, and report
confidence intervals; compare against a closed model on the same set (C-07).

### R-13 — Adapter artifacts are not committed

**What exists.** Adapter weights are gitignored; `make adapter-train` reproduces them; the
signed manifest pins their tree digest, so a different training run produces a manifest that
does not match the release.

**Residual.** Rebuilding the release requires the GPU, the dataset generator and the exact
training configuration. QLoRA training is not bit-reproducible across hardware, so "rebuild"
means "retrain and re-gate", not "verify bytes".

**Closure.** Store adapters in an artifact registry with retention and access control, and
treat the registry digest as the release input. The manifest already has the field; the
storage layer is missing.

### R-14 — Audit failure does not fail the request

**What exists.** `write_audit` logs an exception and lets the request succeed — an explicit
availability-over-evidence trade-off.

**Residual.** During a disk or permission incident, requests are served without an audit
record. There is no metric alerting on this today, only a log line.

**Closure.** Add `gateway_audit_write_failures_total` and page on it; decide with the tenant
whether to fail closed (C-04).

### R-15 — Single runtime

**What exists.** vLLM is measured; TensorRT-LLM, Triton, Dynamo and SGLang are discussed in
ADR-0001 but not exercised.

**Residual.** Runtime-specific performance claims, and the routing logic that assumes
OpenAI-compatible backends, are unproven for other runtimes. The gateway would work with any
OpenAI-compatible server, but no second runtime was served.

**Closure.** Serve the same model on a second runtime behind a candidate and compare on the
same golden set. This is the natural next phase, not a defect in this one.

### R-16 — No dependency or image scanning

**What exists.** Unit tests, static manifest validation, admission policies and a CI workflow
that runs them.

**Residual.** No SCA, container scan or API fuzzing. A vulnerable base image or Python
dependency would not be caught by the current gates. This is a CI gap, not an untested
claim.

**Closure.** Add `pip-audit`/`trivy` to CI with a severity threshold and an exception list
(C-09).

## Accepted risks summary

Five risks are accepted as-is for a reference build: R-13 (artifacts by design), R-15
(single runtime is the scope), R-05 (no mTLS until there is a cluster), R-08 (single writer is
documented as a constraint) and R-14 (availability trade-off is deliberate). The remaining
eleven require a closing action before regulated production use.
