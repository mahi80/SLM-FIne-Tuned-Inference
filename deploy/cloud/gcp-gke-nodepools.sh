#!/usr/bin/env bash
# Create the GPU and CPU pools on an existing GKE cluster.
#
#   CLUSTER=slm-fabric LOCATION=europe-west1 deploy/cloud/gcp-gke-nodepools.sh
#
# GKE installs the driver when gpu-driver-version is set; pair the pool with the
# NVIDIA GPU Operator if you want one driver/device-plugin path across providers
# (then pass gpu-driver-version=disabled instead).
set -euo pipefail

CLUSTER=${CLUSTER:?set CLUSTER to the GKE cluster name}
LOCATION=${LOCATION:?set LOCATION to the region or zone}
GPU_MACHINE_TYPE=${GPU_MACHINE_TYPE:-g2-standard-8}
GPU_TYPE=${GPU_TYPE:-nvidia-l4}
CPU_MACHINE_TYPE=${CPU_MACHINE_TYPE:-e2-standard-8}

# Minimum one keeps a warm replica; maximum follows config/capacity.yaml.
gcloud container node-pools create slm-gpu \
  --cluster "$CLUSTER" \
  --location "$LOCATION" \
  --machine-type "$GPU_MACHINE_TYPE" \
  --accelerator "type=${GPU_TYPE},count=1,gpu-driver-version=latest" \
  --node-labels "nvidia.com/gpu.present=true,slm.fabric/pool=gpu" \
  --node-taints "nvidia.com/gpu=present:NoSchedule" \
  --enable-autoscaling \
  --min-nodes 1 \
  --max-nodes 3 \
  --enable-autorepair \
  --enable-autoupgrade

gcloud container node-pools create slm-cpu \
  --cluster "$CLUSTER" \
  --location "$LOCATION" \
  --machine-type "$CPU_MACHINE_TYPE" \
  --node-labels "slm.fabric/pool=cpu" \
  --node-taints "slm.fabric/pool=cpu:NoSchedule" \
  --enable-autoscaling \
  --min-nodes 1 \
  --max-nodes 4 \
  --enable-autorepair \
  --enable-autoupgrade

gcloud container node-pools list --cluster "$CLUSTER" --location "$LOCATION" \
  --format="table(name, config.machineType, autoscaling.minNodeCount, autoscaling.maxNodeCount)"
