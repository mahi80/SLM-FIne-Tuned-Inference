#!/usr/bin/env bash
# Create an AKS cluster with the identity and network settings the platform
# needs, add the GPU/CPU pools, and install the add-ons.
#
#   RESOURCE_GROUP=slm-rg CLUSTER=slm-fabric LOCATION=westeurope \
#     deploy/cloud/azure-aks-cluster.sh
#   ... --dry-run    # print every az/helm command, create nothing
#
# Without --dry-run you need an Azure identity (az login or a service
# principal), helm and kubectl. The script prints the subscription name and id
# it is about to spend money in; it never prints a credential.
#
# The system pool runs Karpenter's role model only indirectly (AKS has its own
# cluster autoscaler) and hosts the platform controllers. The GPU pool is
# created by azure-aks-nodepools.sh with `--gpu-driver none`, so the GPU
# Operator installs the driver.
#
# Quota is not created here: the NCads A100 v4 family must have vCPUs available
# in LOCATION, or the GPU pool never leaves 0 nodes.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

RESOURCE_GROUP=${RESOURCE_GROUP:-slm-rg}
CLUSTER=${CLUSTER:-slm-fabric}
LOCATION=${LOCATION:-westeurope}
K8S_VERSION=${K8S_VERSION:-1.33}
SYSTEM_VM_SIZE=${SYSTEM_VM_SIZE:-Standard_D8ds_v5}
DRY_RUN=0

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
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
    --dry-run) DRY_RUN=1 ;;
    -h | --help) usage; exit 0 ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if ! dry_run; then
  need az
  need helm
  need kubectl
  SUBSCRIPTION=$(az account show --query id --output tsv)
  echo "subscription $SUBSCRIPTION, resource group $RESOURCE_GROUP, cluster $CLUSTER" >&2
fi

run az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --tags "slm.fabric/cluster=$CLUSTER"

# Workload identity and the OIDC issuer are on from creation: retrofitting them
# means recreating the cluster's identity plane. Network policy is enforced
# because the default-deny manifests assume it is.
run az aks create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CLUSTER" \
  --location "$LOCATION" \
  --kubernetes-version "$K8S_VERSION" \
  --nodepool-name system \
  --node-count 2 \
  --node-vm-size "$SYSTEM_VM_SIZE" \
  --nodepool-labels "slm.fabric/pool=system" \
  --enable-cluster-autoscaler \
  --min-count 2 \
  --max-count 4 \
  --enable-oidc-issuer \
  --enable-workload-identity \
  --network-plugin azure \
  --network-policy calico \
  --generate-ssh-keys

# --overwrite-existing replaces a stale context instead of failing; it does not
# use --admin, so the kubeconfig carries a normal, revocable identity.
run az aks get-credentials \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CLUSTER" \
  --overwrite-existing

if dry_run; then
  # Print this script's plan and the node-pool plan it delegates to.
  RESOURCE_GROUP="$RESOURCE_GROUP" CLUSTER="$CLUSTER" \
    "${ROOT}/deploy/cloud/azure-aks-nodepools.sh" --dry-run
  "${ROOT}/deploy/cloud/bootstrap-addons.sh" --driver=true --dry-run
else
  RESOURCE_GROUP="$RESOURCE_GROUP" CLUSTER="$CLUSTER" \
    "${ROOT}/deploy/cloud/azure-aks-nodepools.sh"

  # AKS created the GPU pool with --gpu-driver none, so the operator owns the
  # driver as well as the device plugin.
  "${ROOT}/deploy/cloud/bootstrap-addons.sh" --driver=true
fi

cat <<EOF

AKS cluster is up: $CLUSTER in $RESOURCE_GROUP ($LOCATION).
Next:
  1. make deploy-secret                                   # tenant Secret, out of band
  2. add this cluster to config/fleet.yaml with its kubectl context
  3. point Argo CD at the repository and sync the platform Application
EOF
