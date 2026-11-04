# Threat model

Scope: the inference platform in this repository, from the tenant application to the model
tier, including the release path that changes what runs. Out of scope: the tenant
application, the Kubernetes control plane, the model author, and managed cloud services.

Boundaries are drawn in [C4 — trust boundaries](../architecture/c4.md#trust-boundaries); this
document names what crosses them and what would happen if it did.

## Assets

| Asset | Why it matters | Where it lives |
|---|---|---|
| Tenant prompts and completions | Customer data; may contain confidential or restricted content | In transit only; not written to the audit log |
| Tenant API keys | Authenticate every request and define quota and allowlists | `config/tenants.yaml` → `Secret/slm-tenants` |
| Routing policy | Decides which tier may see which data | `config/policy.yaml` → content-hashed ConfigMap |
| Audit trail | Proves what ran and why; supports disputes and compliance | `logs/audit.jsonl` / audit PVC |
| Adapter weights | The fine-tuned behaviour; a swapped adapter is a silent product change | `adapters/triage-*` → adapter PVC |
| Signed release manifest | The authoritative statement of what is deployed | `release/*.json` + `.sig` |
| Signing key | Authority to publish a release | Local file today; HSM/KMS in production |

## Actors

| Actor | Capability assumed | Trust |
|---|---|---|
| Tenant application | Holds one bearer key; can send arbitrary prompts | Untrusted input, authenticated identity |
| Tenant end user | Controls prompt content through the application | Untrusted |
| Platform operator | Deploys, reloads policy, can read audit | Trusted, but not trusted to forge evidence |
| Model engineer | Produces adapters and gate reports | Trusted for quality, not for release authority |
| Release manager | Holds the signing key | High trust; split from model engineering |
| Auditor | Read-only verification | Trusted reviewer |
| Network attacker | Can reach the edge only | Untrusted |
| Supply-chain attacker | Controls an upstream image, model or dependency | Untrusted |

## Entry points

1. `POST /v1/chat/completions`, `POST /v1/embeddings` (if configured), `GET /v1/models` — tenant key.
2. `POST /v1/policy/explain` — tenant key; reveals the admission trace for that tenant.
3. `POST /v1/policy/reload`, `GET /v1/policy`, `GET /v1/audit/verify` — admin key.
4. `/metrics` and `/health` — unauthenticated by design; network-scoped.
5. Argo CD sync and the PostSync jobs — cluster-internal.
6. Image and model pulls at first start — network egress, connected overlay only.

## STRIDE analysis

| Category | Threat | Path | Current control | Residual |
|---|---|---|---|---|
| **S**poofing | Stolen bearer key impersonates a tenant | B1 → gateway | Constant-time comparison; per-tenant quotas and allowlists limit blast radius | No expiry or binding to a workload — [R-04](risks.md#r-04--static-bearer-keys) |
| **S**poofing | Rogue pod calls vLLM directly, bypassing gateway | In-namespace | NetworkPolicy allows only `gateway` to `vllm-gpu:8000` | No cryptographic identity — [R-05](risks.md#r-05--no-in-cluster-mtls) |
| **S**poofing | Replaced image under the same tag | Registry → node | Manifests pin digests and the signed manifest records them; admission rejects `latest` | Registry or mirror compromise is still trusted — [R-03](risks.md#r-03--upstream-artifact-compromise) |
| **T**ampering | Edit, delete or reorder audit records | Audit volume | sha256 chain with `prev` links; startup verification refuses a broken chain; `/v1/audit/verify` | Writer can rewrite the whole chain — [R-01](risks.md#r-01--audit-chain-is-evidence-not-prevention) |
| **T**ampering | Change policy without review | Git / ConfigMap | Policy is generated from `config/policy.yaml`; hash rolls the gateway; validator asserts byte equality | Admin can edit policy in-cluster or merge a bad PR — [R-07](risks.md#r-07--tenant-policy-misconfiguration) |
| **T**ampering | Swap adapter weights after signing | Adapter PVC | Manifest pins the adapter tree digest; `make release-check` re-hashes local artifacts | No runtime digest check at load time, and artifacts are outside the cluster's provenance — [R-13](risks.md#r-13--adapter-artifacts-are-not-committed) |
| **R**epudiation | Deny that a request was served by a model version | Dispute | Audit record carries tenant, alias, candidate, model version, policy version, bucket, vetoes, latency, tokens | Chain can be rewritten by a privileged writer; no external notarisation — R-01 |
| **I**nformation disclosure | Confidential data routed to cloud | Gateway → cloud | Admission: `allow_cloud`, `serves_regions`, sovereignty veto for `confidential`/`restricted`; vetoes audited | A misconfigured policy can permit it — R-07 |
| **I**nformation disclosure | Prompt secrets forwarded to a tier | B3/B4 | Audited routing decision; no DLP | Content is not inspected — [R-06](risks.md#r-06--prompt-content-is-not-inspected) |
| **I**nformation disclosure | Cross-tenant data in adapters or cache | GPU | Single-tenant platform deployment; adapters are global, not per tenant | Multi-tenant shared adapter use would need per-tenant adapter isolation; out of scope today |
| **I**nformation disclosure | Audit log leaks prompt content | Audit volume | Records contain metadata and token counts only, never message text | Deliberate: limits forensic content review |
| **D**enial of service | Request flood exhausts GPU or gateway | B1 | Per-tenant sliding-window RPM (429 + `Retry-After`); bounded upstream concurrency | In-memory limiter resets on restart and does not coordinate across replicas — [R-08](risks.md#r-08--gateway-is-a-single-writer) |
| **D**enial of service | Oversized prompt exhausts context | B1 → admission | `max_context` admission per candidate; context estimated from characters | Estimation is approximate (chars/4); vLLM still rejects at the boundary |
| **D**enial of service | Hung upstream holds requests | Gateway → tier | Circuit breaker per `alias:candidate`; timeouts; attempt list fallback | No request dead-line cancellation end to end |
| **E**levation of privilege | Tenant calls admin endpoints | B1 → `/v1/policy/*` | `admin` flag in tenant config, checked by a dependency | Static key compromise yields admin if the key is an admin key — R-04 |
| **E**levation of privilege | Pod escapes or writes host paths | Admission | Non-root, `RuntimeDefault` seccomp, drop `ALL`, read-only rootfs (vLLM excepted), no host namespaces or `hostPath`; Kyverno Enforce | vLLM runs with a writable root filesystem and a CUDA device; that is the widest exception in the system |
| **E**levation of privilege | Unpinned or privileged image admitted | Admission | Six Kyverno ClusterPolicies in Enforce; denials observed on kind for probes and `:latest` | Policies still use the deprecated `kyverno.io/v1` kind — [R-09](risks.md#r-09--no-live-openshift-validation), C-10 |

## AI-specific threats

These do not fit STRIDE cleanly and are the ones reviewers of LLM systems ask about.

| Threat | What the platform does | What it does not do |
|---|---|---|
| **Prompt injection** | The model has no tools and no retrieval; its output is parsed as a fixed JSON schema. A successful injection changes a classification, not the system's authority | There is no runtime injection classifier; a poisoned ticket can still produce a wrong field |
| **Output exfiltration** | Completions are returned to the caller; no other sink exists | No scanning for PII or secrets in completions |
| **Model behaviour drift** | Golden-set gate with floors and drift against a baseline; canary and shadow traffic; audit records the model version | The gate runs at release time, not continuously; a gradual drift within bounds is not alerted |
| **Training-data leakage** | Synthetic dataset generated from scenario templates; no customer records | Fine-tuning can still memorise templates; sampling and differential privacy are not applied |
| **Adapter supply chain** | Manifest pins adapter tree digests; adapter loader only promotes declared names | No signature on adapter weights themselves; a compromised PVC before loading is undetected |
| **Model extraction** | Rate limits and per-tenant quotas; audit trail of volume | No defences against a patient tenant extracting behaviour through queries |
| **Unsafe content** | Schema-constrained output for triage; no free-form tool use | No content moderation layer; the platform is not a guardrail product |

## Attack trees worth stating explicitly

**Goal: get confidential EU data to the cloud tier.**
Requires (a) a policy where the tenant has `allow_cloud: true`, (b) a cloud candidate whose
`serves_regions` includes the tenant's region, and (c) the data class not being
`confidential`/`restricted`. All three are data in `config/policy.yaml`; all three appear in
the audit record as the decision reason. The control is review and linting (C-01/C-03), not
runtime enforcement of intent.

**Goal: make a request look like it was served by v1 when it was served by v2.**
Requires rewriting the audit chain from the affected record onward and recomputing every
subsequent hash, or compromising the gateway process. The former leaves no local trace; only
an externally anchored head hash would reveal it (C-04).

**Goal: run code in the model pod.**
Requires a malicious image (pinned by digest and admission; provenance remains R-03), a
supply-chain compromise (R-03), or an exploit in vLLM's HTTP surface — which is the largest
unscanned attack surface in the design and the reason C-09 exists.

## Out of scope

- Kubernetes control-plane compromise, etcd access, or node-level root.
- The tenant application's own authentication and authorization.
- Threats to the base model's training pipeline at the upstream vendor.
- Physical access to the GPU host.
- Denial of service at the network layer (volumetric); no WAF or DDoS posture is claimed.

## Review triggers

Re-run this threat model when: a second runtime or gateway replica is added, cloud egress is
enabled for a new data class, adapter weights move to a registry, OIDC replaces static keys,
or the audit head is shipped externally. Each of those changes moves a boundary, not just a
configuration value.
