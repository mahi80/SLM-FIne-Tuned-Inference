# Node pools

Capacity contracts for the machines the router may send work to. Nothing here is
rendered by Kustomize or synced by Argo CD: node pools belong to the cluster
owner, and only the label/taint contract is shared with the application.

| File | Provider | Creates |
| --- | --- | --- |
| `aws-karpenter-nodepools.yaml` | EKS | Karpenter `NodePool`s + one `EC2NodeClass` |
| `azure-aks-nodepools.sh` | AKS | two node pools with per-pool cluster autoscaler |
| `gcp-gke-nodepools.sh` | GKE | two node pools with per-pool autoscaling |
| `onprem-workers.sh` | OpenShift on-prem | labels and taints existing GPU workers |

Each snippet creates a GPU pool and a CPU pool. The names, sizes and bounds are
examples: the contract is the two labels and two taints, which the deployment
tree already expects.

## The contract

GPU nodes

* label `nvidia.com/gpu.present=true` — what `Deployment/vllm-gpu` selects.
* taint `nvidia.com/gpu=present:NoSchedule` — keeps CPU workloads and simulators
  off hardware that costs orders of magnitude more per hour.
* `nvidia.com/gpu` advertised in `status.allocatable` — added by the NVIDIA GPU
  Operator (driver + device plugin + node-feature-discovery). The snippets do
  not install it, and on AKS the driver is deliberately left to it.

CPU nodes

* label `slm.fabric/pool=cpu` — the simulators *prefer* this pool.
* taint `slm.fabric/pool=cpu:NoSchedule` — the simulators tolerate it.

`tools/validate_deploy.py` refuses a rendered overlay whose GPU Deployment does
not select and tolerate these values, so a pool that violates the contract
fails CI rather than degrading at runtime.

## From capacity to a route

These snippets buy machines. Making the router send traffic to them is a
separate, explicit step, because a pool without a route is dormant capacity and a
route without a pool is a 503:

1. Apply the snippet and confirm `kubectl get nodes -l nvidia.com/gpu.present=true`
   shows an allocatable GPU.
2. Add a pool to [`config/capacity.yaml`](../../config/capacity.yaml) with the
   same compute, deployment, endpoint and node-pool contract.
3. Add a candidate to [`config/policy.yaml`](../../config/policy.yaml) pointing
   at that pool and at the pool's endpoint.
4. `python tools/validate_deploy.py` — it fails if the candidate and the pool
   disagree about compute, deployment or backend.

For a cloud provider's managed endpoint the pool is *external*: it has no
Deployment, Service or node pool in this cluster. Steps 1–4 still describe the
provider's node pool, but the registry entry and the route look different:

1. Record the pool with `external: true`, an `https` endpoint, an `slo` block
   (`expected_latency_ms`, `cost_per_1k_out`, `cost_source`) and **no**
   `workload` or `node_pool` — the validator rejects either. Replica bounds are
   optional and normally omitted: the provider scales.
2. Give the candidate an `access` block that names a key, never the key itself:
   `access: {key: external-prod}`. The candidate also restates the pool's
   `latency_ms` and `cost_per_1k_out`; the validator fails a drift rather than
   admitting on a stale figure.
3. Create `Secret/slm-backend-keys` out of band in `slm-fabric` with
   `keys: {external-prod: …}`. It is mounted optionally at
   `/app/backend-keys`; a missing entry vetoes the candidate as
   `backend-unconfigured` instead of failing a request.
4. Add `components/cloud-egress` to the overlay and replace its TEST-NET-2
   placeholder with the provider's published range, TCP 443 only. The
   validator rejects a default, private, loopback or link-local CIDR, the
   component without an external candidate, and an external candidate without
   the component. The air-gap overlay forbids it.
5. Mount a CA bundle and set `SLM_BACKEND_CA` only if the provider's chain is
   not in the image's trust store. A configured bundle that is missing stops
   the gateway rather than silently trusting public roots.

Until you do this, `cloud-burst` in `config/capacity.yaml` is `provider:
simulated` and every response it serves is labelled `x-simulated`. The rationale
and limits are in [ADR-0009](../../docs/adr/ADR-0009-external-backends.md).

## Which endpoints actually work

The gateway is an OpenAI-compatible proxy, not a translation layer: it calls
`POST <backend>/v1/chat/completions` and `GET <backend>/health`, and it
authenticates with one header. That is the whole contract, so the honest
compatibility list is short.

| Endpoint | Path and auth | Works unchanged? |
|---|---|---|
| vLLM (or TGI, SGLang, llama.cpp server) on a cloud GPU, reachable over `https` | OpenAI paths; a bearer token from an `api-key`/`--api-key` flag or an auth proxy in front | **Yes** — `backend: https://…`, `access: {key: …}` |
| OpenAI-compatible SaaS (hosted vLLM/TGI endpoints, Together, Fireworks, DeepInfra, and OpenAI itself) | `Authorization: Bearer <api key>`, `/v1/chat/completions` | **Yes** — one key per provider |
| Azure OpenAI | `/openai/deployments/<deployment>/chat/completions?api-version=…`, `api-key:` header, model in the path | **No** — different path, header and query; needs a fronting proxy until an adapter exists |
| Amazon Bedrock | `bedrock-runtime` `Converse`/`InvokeModel`, SigV4 request signing, model id in the path | **No** — different API, different auth; needs a fronting proxy until an adapter exists |
| Google Vertex AI | `generateContent` under a project/location path, Google OAuth2 access token | **No** — different path and body; needs a fronting proxy until an adapter exists |

"No" means exactly that: an `access` block cannot make that provider work, because
the gateway would still call `/v1/chat/completions` with a bearer header. The
supported pattern today is a thin service (an in-cluster Deployment, a gateway
product, or the provider's own compatibility shim) that exposes the OpenAI shape
and holds the provider-specific auth; the platform then treats *that* service as
the external backend. An adapter inside the gateway — request/response mapping
plus SigV4 or Google OAuth — is not implemented, and no compatibility claim is
made for it.

## Credentials

`access.source` says where the credential behind `access.key` comes from:

* `static` (default) — the value in `Secret/slm-backend-keys`, read at startup; a
  rotation is a Secret update plus a restart.
* `file` — `key` names a file under `SLM_BACKEND_TOKEN_DIR` (default
  `/app/backend-tokens`), read on every request. Use it for a projected
  service-account token or anything else that rotates on disk; no restart and no
  reload is needed. The file name must be a plain name (`[A-Za-z0-9][A-Za-z0-9._-]*`).
  The base Deployment mounts only the keys Secret, so a `file` candidate also
  needs a projected-token volume (see `examples/policy-candidates.yaml`).
* `oidc` — `key` names a client secret in the same Secret, exchanged at
  `token_url` (client-credentials grant, optional `scope`) for a short-lived
  access token. The token is cached per gateway process until about a minute
  before it expires; a `401` clears it and retries that candidate once.

Copy-ready snippets for all three are in [`examples/`](examples/): a pool entry,
a policy fragment and an egress rule that also covers the IdP. Nothing in
`examples/` is applied by an overlay.

After mounting the Secret, check what this cluster can actually do before sending
tenant traffic:

```bash
PYTHONPATH=src python -m tools.check_backend \
  --policy config/policy.yaml --keys /app/backend-keys/keys.yaml
```

One line per external candidate — alias, candidate, host, key name, source,
configured state, probe result, latency — and exit 1 if any is unconfigured,
fails authentication or is down. It prints key names, never key values.
`tests/test_check_backend.py` drives the same code with a mocked transport.

No IdP and no provider was contacted for any of this: the token exchange, cache,
401 retry and harness are proven against a local mock. ADR-0010 states the
remaining limits (per-process cache, no refresh tokens, no private-key JWT).

## Autoscaling interaction

The pool bounds in `config/capacity.yaml` are pod bounds. The node autoscaler
decides whether a pending replica gets a machine:

* Karpenter, AKS cluster autoscaler and GKE autoscaling each grow the node pool
  up to the limits above; the pod scaler in `deploy/kustomize/components/`
  grows replicas up to the pool's `max`.
* When pods outgrow machines, replicas stay `Pending` and queue depth climbs.
  Alert on `kube_pod_status_unschedulable` if you add pools beyond the GPU limit.
* On-prem nodes are fixed. `MachineAutoscaler` can add workers only if the
  cluster was installed with MachineSets; otherwise the ceiling is the number of
  GPUs the platform team provisioned, and `max` in the registry must not exceed
  it.
