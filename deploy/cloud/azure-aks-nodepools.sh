#!/usr/bin/env bash
# Create the GPU and CPU pools on an existing AKS cluster.
#
#   RESOURCE_GROUP=slm-rg CLUSTER=slm-fabric deploy/cloud/azure-aks-nodepools.sh
#   RESOURCE_GROUP=slm-rg CLUSTER=slm-fabric deploy/cloud/azure-aks-nodepools.sh --dry-run
#
# The cluster autoscaler is enabled per pool, because AKS scales node pools
# independently. `--gpu-driver none` leaves the driver to the NVIDIA GPU
# Operator, which is the same component that adds nvidia.com/gpu as a
# schedulable resource.
set -euo pipefail

RESOURCE_GROUP=${RESOURCE_GROUP:?set RESOURCE_GROUP to the cluster resource group}
CLUSTER=${CLUSTER:?set CLUSTER to the AKS cluster name}
GPU_VM_SIZE=${GPU_VM_SIZE:-Standard_NC24ads_A100_v4}
CPU_VM_SIZE=${CPU_VM_SIZE:-Standard_D8ds_v5}
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '+ %s
' "$*"
  else
    "$@"
  fi
}

# The taint matches the toleration in the vLLM Deployment; the label matches its
# nodeSelector. Minimum one keeps a warm replica, maximum follows
# config/capacity.yaml.
run az aks nodepool add \
  --resource-group "$RESOURCE_GROUP" \
  --cluster-name "$CLUSTER" \
  --name gpu1 \
  --node-count 1 \
  --min-count 1 \
  --max-count 3 \
  --enable-cluster-autoscaler \
  --node-vm-size "$GPU_VM_SIZE" \
  --node-labels nvidia.com/gpu.present=true slm.fabric/pool=gpu \
  --node-taints "nvidia.com/gpu=present:NoSchedule" \
  --gpu-driver none \
  --os-sku Ubuntu

# The simulators prefer this pool and tolerate its taint. They have no latency
# SLO, so the floor is one node and scaling is optional.
run az aks nodepool add \
  --resource-group "$RESOURCE_GROUP" \
  --cluster-name "$CLUSTER" \
  --name cpu1 \
  --node-count 1 \
  --min-count 1 \
  --max-count 4 \
  --enable-cluster-autoscaler \
  --node-vm-size "$CPU_VM_SIZE" \
  --node-labels slm.fabric/pool=cpu \
  --node-taints "slm.fabric/pool=cpu:NoSchedule" \
  --os-sku Ubuntu

run az aks nodepool list --resource-group "$RESOURCE_GROUP" --cluster-name "$CLUSTER" \
  --query "[].{name:name, vm:vmSize, min:minCount, max:maxCount, labels:nodeLabels, taints:nodeTaints}" \
  --output table
