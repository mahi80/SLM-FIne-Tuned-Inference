# ADR-0009 — External backends authenticate by reference, and fail closed

- **Status:** Accepted
- **Date:** 2026-11-05
- **Deciders:** Platform architecture
- **Related:** ADR-0004 (policy router), ADR-0006 (deployment), ADR-0008 (capacity pools)

## Context

ADR-0008 made a cloud route describable, but not reachable. Three separate controls blocked
it, and each was deliberate until now:

- The gateway called every backend with a plain request and no credential. An in-cluster
  backend was trusted because the only pods that could reach it were ours.
- Base `NetworkPolicy` denies all egress and allows only the in-cluster backends. A backend
  at `https://…` would time out with no explanation beyond a 503.
- The deploy validator required every policy backend to render as a Service. A managed
  provider never will, so an honest external candidate could not pass CI.

The naive fix — add a URL and a token to `policy.yaml` — puts a credential in Git, in the
ConfigMap the gateway mounts, in the signed policy hash and in every policy diff. It also
makes a typo a runtime outage instead of a validation failure.

## Decision

1. **Key by reference, never by value.** A candidate may carry
   `access: {key: <name>, header: authorization, prefix: "Bearer "}`. `key` names an entry
   in a mounted `keys.yaml`; the value lives only in `Secret/slm-backend-keys`, created out
   of band. The policy, audit record, `/v1/policy` output and logs can name the key, never
   its value. `src/gateway/backends.py` is the only code that reads the file, and its errors
   name the key, not the secret.
2. **External means TLS or nothing.** A candidate with `access` must use `https://`, must
   name a host, and must not carry userinfo, a query or a fragment. The gateway verifies
   against the system trust store or `SLM_BACKEND_CA`; a configured CA bundle that is
   missing is a startup failure, not a silent downgrade to public roots.
3. **Unconfigured is a veto, not a crash.** Admission gains
   `backend-unconfigured`: an external candidate whose key is absent is considered and
   rejected like any other veto, with the reason in the audit record. An in-cluster-only
   install has no keys file and keeps working; a reload keeps the same readiness view.
4. **Health probes feed the same breakers as requests.** A loop probes each configured
   external candidate every `SLM_PROBE_INTERVAL_S` (default 30 s) against `/health`, and
   records success or failure under the alias and candidate key. A 4xx counts as reachable
   (the host and certificate answered); a 5xx or transport error counts as down. A dead
   cloud route therefore opens its breaker before a tenant request finds it.
5. **Egress is opt-in and narrow.** The `cloud-egress` component adds
   `allow-gateway-cloud-egress`: gateway pods only, an `ipBlock`, TCP 443. The validator
   rejects a rule that names no peer, opens any other port, or uses a default, private,
   loopback or link-local CIDR. It also rejects the component when no candidate is
   external, and rejects external candidates when the component is absent. The air-gap
   overlay forbids it outright.
6. **The registry agrees in both directions.** A pool declares `external: true` with an
   `https` endpoint and no local workload or node pool; its candidates must carry `access`.
   An in-cluster pool's candidates must not. The validator fails either mismatch, and the
   Service-rendering check skips only candidates that opt into external access.

## Consequences

**Positive:** adding a provider is now a data change (a pool, a candidate, a Secret) plus one
component, with CI rejecting every half-finished combination. Credentials never enter the
repository or the signed policy. A missing key degrades to a documented veto instead of a
500, and a dead provider opens a breaker before it burns a request.

**Negative / limits, stated rather than hidden:**

- The shipped policy has no external candidate; the feature is proven by unit tests, a
  local TLS server and static validation, not against a live provider (N-12, R-18).
- Bearer keys are static. Rotation is a Secret update plus a pod restart, because the keys
  file is read once at startup. Short-lived credentials or workload identity are a later
  slice; the `access` shape is designed to grow an alternative mode.
- `NetworkPolicy` cannot express a DNS name. A provider whose range is unstable needs a
  maintained CIDR list, or a private endpoint, which is an in-cluster route and not covered
  by this rule.
- Probes cost one request per external candidate per interval, and a probe failure counts
  toward the same failure budget as tenant traffic.
- There is still no per-tenant egress accounting, and no cap on what an external route may
  cost; that is the next stage's admission input.

**Revisit when:** a tenant requires workload identity instead of a bearer key, a provider
publishes only hostnames, or a latency/cost budget must gate the cloud route (Stage 4).
