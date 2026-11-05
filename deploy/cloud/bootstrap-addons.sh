#!/usr/bin/env bash
# Install the cluster-wide pieces the platform assumes exist:
# NVIDIA GPU Operator, KEDA, Argo CD and Kyverno.
#
#   deploy/cloud/bootstrap-addons.sh --driver=true          # fresh managed GPU nodes
#   deploy/cloud/bootstrap-addons.sh --driver=false         # provider already ships the driver
#   deploy/cloud/bootstrap-addons.sh --skip-keda --skip-gitops
#   deploy/cloud/bootstrap-addons.sh --dry-run              # print the helm commands, run nothing
#
# This installs controllers, not the platform. Argo CD then reconciles the
# overlays from Git; nothing here creates a Secret or applies a policy.
#
# The GPU driver is the one choice that depends on the provider:
#   * AKS creates GPU pools with `--gpu-driver none`, so the operator must
#     install the driver (`--driver=true`).
#   * EKS accelerated AL2023 and GKE with `gpu-driver-version` already carry a
#     driver; the operator then installs only the device plugin and NFD
#     (`--driver=false`).
set -euo pipefail

DRIVER=true
SKIP_KEDA=0
SKIP_GITOPS=0
DRY_RUN=0

# Pinned so two clusters built a month apart run the same controllers. Bump a
# pin deliberately; `--version` is never left to `latest`.
GPU_OPERATOR_VERSION=${GPU_OPERATOR_VERSION:-v24.9.2}
KEDA_VERSION=${KEDA_VERSION:-2.16.1}
ARGOCD_VERSION=${ARGOCD_VERSION:-7.7.11}
KYVERNO_VERSION=${KYVERNO_VERSION:-3.3.4}

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
}

dry_run() { [ "$DRY_RUN" = 1 ]; }

run() {
  if dry_run; then
    printf '+ %s\n' "$*"
  else
    "$@"
  fi
}

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required tool: $1" >&2
    exit 1
  }
}

for arg in "$@"; do
  case "$arg" in
    --driver=true) DRIVER=true ;;
    --driver=false) DRIVER=false ;;
    --skip-keda) SKIP_KEDA=1 ;;
    --skip-gitops) SKIP_GITOPS=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h | --help) usage; exit 0 ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if ! dry_run; then
  need helm
  need kubectl
fi

run helm repo add nvidia https://helm.ngc.nvidia.com/nvidia --force-update
if [ "$SKIP_KEDA" = 0 ]; then
  run helm repo add kedacore https://kedacore.github.io/charts --force-update
fi
if [ "$SKIP_GITOPS" = 0 ]; then
  run helm repo add argo https://argoproj.github.io/argo-helm --force-update
  run helm repo add kyverno https://kyverno.github.io/kyverno --force-update
fi
run helm repo update

# The device plugin is what turns `nvidia.com/gpu` into an allocatable
# resource; without it the vLLM pod stays Pending with a confusing message.
run helm upgrade --install gpu-operator nvidia/gpu-operator \
  --namespace gpu-operator --create-namespace \
  --version "$GPU_OPERATOR_VERSION" \
  --set "driver.enabled=$DRIVER" \
  --set toolkit.enabled=true \
  --wait --timeout 15m

# KEDA owns the vLLM ScaledObject; without it the GPU pool stays at one
# replica. It is optional because a fixed replica is a valid deployment.
if [ "$SKIP_KEDA" = 0 ]; then
  run helm upgrade --install keda kedacore/keda \
    --namespace keda --create-namespace \
    --version "$KEDA_VERSION" \
    --wait --timeout 10m
fi

# Argo CD and Kyverno are what make the Git loop real: sync waves, drift
# repair, and the six admission policies the manifests assume.
if [ "$SKIP_GITOPS" = 0 ]; then
  run helm upgrade --install argocd argo/argo-cd \
    --namespace argocd --create-namespace \
    --version "$ARGOCD_VERSION" \
    --wait --timeout 15m
  run helm upgrade --install kyverno kyverno/kyverno \
    --namespace kyverno --create-namespace \
    --version "$KYVERNO_VERSION" \
    --wait --timeout 15m
fi

cat <<'EOF'

Add-ons installed. What is still yours to do:
  1. Create the tenant Secret out of band: make deploy-secret (a local file; never Git).
  2. Point Argo CD at this repository and apply the platform Application.
  3. Add the cluster to config/fleet.yaml so the inventory and `make fleet-check` see it.
EOF
