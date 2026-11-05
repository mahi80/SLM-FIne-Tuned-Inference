# ADR-0010 — Backend credentials rotate, and OIDC exchanges happen server-side

- **Status:** Accepted
- **Date:** 2026-11-05
- **Deciders:** Platform architecture
- **Related:** ADR-0009 (external backends), ADR-0004 (policy router), ADR-0006 (deployment)

## Context

ADR-0009 made an external backend reachable with one credential shape: a value in
`Secret/slm-backend-keys`, read once at startup and sent as `Authorization: Bearer …`.
That is enough for a provider that issues a durable API key, and it is wrong for two
deployments that a regulated tenant keeps asking for:

- **Provider credentials that rotate.** A key rotation means editing the Secret and
  restarting the gateway, because the keys file is read once. A restart drops in-process
  quota, breaker and audit state, and the gateway is deliberately a single replica, so
  the restart is an outage window. Any rotation cadence short enough to matter is
  therefore shorter than the one the platform can afford.
- **Workload identity.** A provider or internal endpoint that accepts a projected
  service-account token, or an IdP that issues short-lived tokens for a registered
  client, cannot be used at all: the credential is not a static string.

The naive fix — put the OIDC client secret and token endpoint in the policy — repeats the
mistake ADR-0009 rejected, and adds a second one: a token acquired per request turns the
IdP into a hard dependency of every inference call.

Nothing about this stage can be proven against a real IdP or provider: neither exists in
this repository, and no external endpoint is configured. The work is therefore shaped so
that the parts that can be wrong are unit-testable with a mocked transport.

## Decision

1. **`access.source` names the credential mechanism.** `static` is the Stage 2 behaviour
   and the default, so every existing policy keeps its meaning. `file` treats `key` as a
   file name in a token directory (`SLM_BACKEND_TOKEN_DIR`, default
   `/app/backend-tokens`). `oidc` treats `key` as a client secret in the keys file and
   exchanges it at `token_url` for an access token.
2. **Source-specific fields are rejected, not ignored.** `token_url`, `client_id` and
   `scope` are only valid with `source: oidc`; `oidc` requires the first two and rejects a
   scope that is present but blank. A file source restricts `key` to
   `^[A-Za-z0-9][A-Za-z0-9._-]*$` and forbids `.` and `..`, because the key becomes a
   path. Static and OIDC keep the non-empty-name rule.
3. **One URL rule for everything the gateway calls.** The token endpoint is checked by the
   same function as the backend endpoint: `https`, a host, no userinfo, no query, no
   fragment. A second hand-written check would drift.
4. **A file token is read on every request.** Rotation needs no restart and no reload: the
   next request sees the new file. A file that is missing or empty makes the candidate
   *unconfigured*, which is a veto, not an empty credential sent upstream.
5. **An OIDC token is cached per process.** The exchange is a `POST` with
   `grant_type=client_credentials`, `client_id`, the secret from the keys file and `scope`
   when declared. The response's `access_token` is cached until `now + expires_in - 60`
   seconds, with a five-second floor; `expires_in` defaults to 300 s when absent. The
   cache is keyed by `key`, lives only in memory and is never written to disk, an audit
   record or a metric label. A refresh on a 401 clears it.
6. **A 401 from a rotating or OIDC credential gets exactly one retry.** A token can expire
   between the probe and the request, and a projected token can be rotated between two
   requests. One refresh follows one 401; a second 401 is an authentication failure like
   any other. A static key is never retried, because the value cannot change in-process.
7. **Failures are named, never quoted.** A credential failure raises `CredentialError`
   whose message contains the key name and, at most, an HTTP status. No response body, no
   token, no secret and no header value enters an exception, a log line, a 503 detail or
   the probe output. The request path treats such a failure as a failed attempt: breaker
   failure, warning log with the key name, next candidate. The probe loop treats it as
   *down*.
8. **A harness answers "can this cluster reach it" without a tenant request.**
   `tools/check_backend.py` walks every external candidate, reports alias, candidate,
   host, key name, source, configured state, probe result and latency, and exits non-zero
   if any candidate is unconfigured, fails authentication or is down. It accepts an
   injected `httpx.AsyncClient` so tests drive it with `httpx.MockTransport`.
9. **The registry does not change.** Pools, candidates and the egress component keep their
   Stage 2 shape: a candidate still needs a pool, a key name and the opt-in egress rule.
   Stage 3 changes how the credential behind the key is obtained, not where the route is
   allowed to go.

## Consequences

**Positive:** a provider key rotation is a file write or a Secret update rather than a
restart, and a projected-token or OIDC deployment becomes describable in policy without a
credential entering Git, the ConfigMap, the signed policy hash or an audit record. The
token exchange happens once per process per key instead of once per request, so the IdP is
not on the request path in steady state. Every failure mode is unit-tested with a mocked
transport, and the harness makes an unconfigured or unauthenticated candidate visible
before a tenant finds it as a 503.

**Negative / limits, stated rather than hidden:**

- **No IdP and no provider was contacted.** There is no live endpoint, no registered
  client and no real token in this repository. The OIDC path is proven against
  `httpx.MockTransport` and a local test server only; the first real deployment is the
  first real test (N-12, R-18).
- **The token cache is per process and per key.** Two gateway replicas hold two tokens, a
  refresh in one is not seen by the other, and a revoked token is used until it expires or
  a 401 clears it. Nothing here implements single-flight either: a burst after expiry can
  issue several exchanges at once. All of that is acceptable because the gateway is a
  single replica (ADR-0006) and tokens are short-lived; it would not be acceptable if the
  gateway were ever scaled out.
- **No refresh tokens, no JWT assertion, no mTLS.** Only the client-credentials grant is
  implemented: no user delegation, no private-key JWT (the usual way to avoid holding a
  secret), no workload identity federation. The `access` shape can grow these, but it does
  not have them.
- **A file token is trusted as written.** The gateway does not check its expiry, its
  audience or its signature; it strips whitespace and sends it. Rotation is assumed to be
  atomic (a projected volume symlink swap); a file caught half-written would be sent and
  fail upstream, which surfaces as a 401 and one retry.
- **The base manifest mounts no token directory.** The gateway Deployment mounts
  `Secret/slm-backend-keys` and nothing else, so a `file` candidate needs an operator to
  add a projected-token volume (and `SLM_BACKEND_TOKEN_DIR` if the path differs). Without
  it the candidate is simply unconfigured, which the harness reports before traffic does.
- **Egress still has to allow the IdP.** The shipped `cloud-egress` component opens one
  CIDR on TCP 443. An OIDC deployment must add the token endpoint's range, which
  `deploy/cloud/examples/egress-policy.yaml` shows and the validator does not yet check.
- **The harness is a manual step.** It is not wired into CI (no cluster, no credentials
  there) and it is not a substitute for the probe loop; it is what an operator runs after
  mounting a Secret, before sending traffic.

**Revisit when:** a provider requires private-key JWT or workload-identity federation, the
gateway needs more than one replica, a token must be shared across processes, or an IdP
range has to be validated the way the provider range is.
