# ADR-0013 — Bring-up is a printed script, not Terraform

- **Status:** Accepted
- **Date:** 2026-11-05
- **Deciders:** Platform architecture
- **Related:** ADR-0008 (capacity pools), ADR-0006 (deployment), ADR-0012 (fleet inventory)

## Context

Through the first six phases the repository could describe a cluster but not create one.
`deploy/cloud/` held node-pool contracts — Karpenter `NodePool`s, `az aks nodepool add`,
`gcloud container node-pools create`, an on-prem label/taint script — that assumed a working
control plane, an installed GPU operator, KEDA on the operator overlays, Argo CD for the sync
waves and Kyverno for the admission policies. Every one of those was listed as a prerequisite
in the README, and none of them was produced by anything in the repository.

That gap has a cost in review. A team reading the repository could not answer "what exactly
happens on the first real run?" without reading four provider consoles, and the answer
differed per provider in ways the node-pool contracts hid: AKS creates GPU pools without a
driver, EKS accelerated images carry one, GKE installs one at pool creation, and the
consequence — whether the GPU Operator must install the driver or only the device plugin — is
a property of the cluster, not of the pool.

The obvious alternatives were Terraform (or OpenTofu), Crossplane, or an operator per
provider. Each brings state to hold and a second toolchain to install, and state in a
reference repository is either committed (a drift and secret hazard) or held remotely (an
account the reader does not have). The value being added here is the *reasoning* — identity
federation, autoscaler ownership, the driver split, pinned versions — not a module registry.

## Decision

1. **One script per provider, one for the add-ons.** `aws-eks-cluster.sh`,
   `azure-aks-cluster.sh` and `gcp-gke-cluster.sh` create the control plane, a small system
   node group, identity federation and the autoscaler, then call the provider's existing
   node-pool snippet. `bootstrap-addons.sh` installs the NVIDIA GPU Operator, KEDA, Argo CD
   and Kyverno on whichever cluster `kubectl` already points at. The scripts do not duplicate
   the label/taint contract; they delegate to it.
2. **Every provider mutation goes through one wrapper.** A `run()` function prints the command
   under `--dry-run` and executes it otherwise, and `--dry-run` is the default review
   artifact. There is no path that mutates an account while printing a plan, and the dry run
   needs only `bash` — no provider CLI, no credential, no network.
3. **The plan is a test.** `tests/test_cloud_bringup.py` runs all four scripts under
   `--dry-run` and asserts the cluster call, the delegated node-pool call, the add-on install,
   workload-identity and OIDC flags, the absence of `--admin`, and the driver flag each
   provider implies. It also parses `bootstrap-addons.sh` so every chart is pinned with
   `--version` and waited on with `--wait --timeout`, and scans every script and JSON file for
   credential shapes.
4. **IAM is scoped before it is created.** `aws-karpenter-controller-policy.json` grants
   `iam:PassRole` only to `KarpenterNodeRole-*` and only when the target service is
   `ec2.amazonaws.com`; a test fails if a statement widens it. The controller role is created
   through `eksctl create iamserviceaccount` (IRSA), not a hand-built trust document.
5. **Charts are pinned in the script.** `GPU_OPERATOR_VERSION`, `KEDA_VERSION`,
   `ARGOCD_VERSION` and `KYVERNO_VERSION` are variables with fixed defaults, overridable per
   run. `latest` is never used, for the same reason the manifests pin images by digest.
6. **The driver split follows the provider.** The AKS script passes `--driver=true` because
   AKS creates GPU pools with `--gpu-driver none`; the EKS and GKE scripts pass
   `--driver=false` because the node image already carries a driver. Two drivers on one node
   is a known way to break CUDA, so this is encoded, not documented as a hint.
7. **Nothing is applied by CI.** CI runs the dry-run tests. No workflow holds a cloud
   credential, and no script has a "CI mode" that would make one seem plausible.

## Consequences

**Positive:** the first real run is a readable diff instead of a console tutorial. A reviewer
can see the control plane, the IAM role, the autoscaler and the add-on versions before anything
is spent, and the provider-specific traps (OIDC issuer, workload identity pool, `--admin`,
CloudFormation tag propagation for Karpenter discovery) are in one reviewable place. The
driver split and the scoped `PassRole` cannot silently regress.

**Negative / limits, stated rather than hidden:**

- **No state, so no idempotent convergence.** The scripts guard the expensive objects they can
  (`aws iam get-policy`, `get-role`, `get-instance-profile`), and `az aks create` and
  `gcloud container clusters create` fail loudly on an existing name, but there is no plan,
  no destroy, and no drift detection. Re-running after a partial failure is a manual
  judgement; Terraform would do better and costs a toolchain and a state store.
- **No account was used.** A dry run proves the command shape, not that the provider accepts
  it: quota, IAM propagation delay, Karpenter node-class AMI resolution, AKS `NCads` regional
  availability and GKE `nvidia-l4` capacity are unobserved (N-14, R-17). The
  `KARPENTER_VERSION` pin is not checked against the cluster's Kubernetes version by anything
  except a real run.
- **The scripts stop at the cluster boundary.** The tenant Secret, the storage class, Argo
  CD's repository registration and the `config/fleet.yaml` entry stay manual. They depend on
  an identity provider, a storage backend and a review decision that a script cannot know.
- **Two credential models are still unexercised.** Workload identity is requested on AKS and
  GKE and IRSA on EKS, but no workload in this repository binds a Kubernetes service account
  to one; the credential path that matters today is still the mounted keys Secret (ADR-0010).
- **On-prem has no bring-up.** `onprem-workers.sh` labels existing workers; installing
  OpenShift, a MachineSet or the GPU Operator there is the platform team's installer, not this
  repository's.

**Revisit when:** a first provider cluster is actually built and the script is corrected by
what the account rejects, when more than one cluster per provider is needed (the moment a
state store starts paying for itself), or when a destroy path becomes a requirement rather
than a convenience.
