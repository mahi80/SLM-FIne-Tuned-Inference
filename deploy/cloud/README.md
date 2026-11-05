# Node pools and cluster bring-up

Capacity contracts for the machines the router may send work to, plus the scripts
that build a managed cluster the contracts can land on. Nothing here is rendered
by Kustomize or synced by Argo CD: the cluster belongs to its owner, and only the
label/taint contract is shared with the application.

## Capacity contracts

| File | Provider | Creates |
| --- | --- | --- |
| `aws-karpenter-nodepools.yaml` | EKS | Karpenter `NodePool`s + one `EC2NodeClass` |
| `azure-aks-nodepools.sh` | AKS | two node pools with per-pool cluster autoscaler |
| `gcp-gke-nodepools.sh` | GKE | two node pools with per-pool autoscaling |
| `onprem-workers.sh` | OpenShift on-prem | labels and taints existing GPU workers |

Each snippet creates a GPU pool and a CPU pool. The names, sizes and bounds are
examples: the contract is the two labels and two taints, which the deployment
tree already expects.

## Bring-up scripts

| File | Provider | Creates |
| --- | --- | --- |
| `aws-eks-cluster.sh` | EKS | control plane, one system node group, Karpenter (IAM policy, controller role, node role, instance profile, Helm release), then the NodePools |
| `azure-aks-cluster.sh` | AKS | resource group, cluster with workload identity and Calico, then the two pools |
| `gcp-gke-cluster.sh` | GKE | regional cluster with Workload Identity, then the two pools |
| `bootstrap-addons.sh` | any | NVIDIA GPU Operator, KEDA, Argo CD, Kyverno — pinned and waited on |
| `aws-karpenter-controller-policy.json` | EKS | the controller policy the first script attaches; `iam:PassRole` is scoped to the node role and its EC2 trust |

Each script prints every command it would run under `--dry-run`, which needs only
`bash` — no provider CLI, no credential, no account. Without the flag it refuses
to start until the provider CLI, `helm` and `kubectl` are on the path, then runs
the same commands for real. It never prints a credential; what it prints is the
account, subscription or project it is about to spend money in.

```bash
# read the plan first — this is the review artifact
deploy/cloud/aws-eks-cluster.sh --dry-run
deploy/cloud/azure-aks-cluster.sh --dry-run
deploy/cloud/gcp-gke-cluster.sh --dry-run
deploy/cloud/bootstrap-addons.sh --dry-run

# then, with credentials and quota in place
CLUSTER=slm-fabric REGION=eu-west-1 deploy/cloud/aws-eks-cluster.sh
```

Defaults are overridable per run:

| Script | Variables |
| --- | --- |
| `aws-eks-cluster.sh` | `CLUSTER`, `REGION` (`AWS_REGION`), `K8S_VERSION`, `SYSTEM_NODE_TYPE`, `SYSTEM_MIN`, `SYSTEM_MAX`, `KARPENTER_VERSION` |
| `azure-aks-cluster.sh` | `RESOURCE_GROUP`, `CLUSTER`, `LOCATION`, `K8S_VERSION`, `SYSTEM_VM_SIZE` |
| `gcp-gke-cluster.sh` | `PROJECT`, `CLUSTER`, `LOCATION`, `K8S_VERSION`, `SYSTEM_MACHINE_TYPE`, `SYSTEM_MIN`, `SYSTEM_MAX` |
| `bootstrap-addons.sh` | `GPU_OPERATOR_VERSION`, `KEDA_VERSION`, `ARGOCD_VERSION`, `KYVERNO_VERSION`; `--driver=true\|false`, `--skip-keda`, `--skip-gitops` |

The driver flag is not cosmetic. AKS creates GPU pools with `--gpu-driver none`, so
the operator must install the driver (`--driver=true`, what the AKS script passes).
EKS accelerated AL2023 and GKE with `gpu-driver-version=latest` already carry a
driver, so the operator installs only the device plugin and NFD
(`--driver=false`). Two drivers on one node is a known way to break CUDA.

What the scripts deliberately leave to you: the tenant Secret (`make deploy-secret`),
the Argo CD repository registration and the platform Application, the
`ReadWriteMany` storage class, and the entry in `config/fleet.yaml`. Those are
account- and policy-specific, so a script cannot guess them.

The credential, IAM role and quota each provider needs is listed in
[Taking it to real infrastructure](../../README.md#taking-it-to-real-infrastructure).
No script here has been run against a real account, and no cluster was created: the
plans are statically checked (`tests/test_cloud_bringup.py`) and reviewed, not
tested infrastructure (risk R-17). Quota, IAM propagation and instance availability
are only discovered on the first real run.

## The contract

GPU nodes

* label `nvidia.com/gpu.present=true` — what `Deployment/vllm-gpu` selects.
* taint `nvidia.com/gpu=present:NoSchedule` — keeps CPU workloads and simulators
  off hardware that costs orders of magnitude more per hour.
* `nvidia.com/gpu` advertised in `status.allocatable` — added by the NVIDIA GPU
  Operator (driver + device plugin + node-feature-discovery). The capacity snippets
  do not install it; `bootstrap-addons.sh` does, and on AKS it owns the driver too.

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
