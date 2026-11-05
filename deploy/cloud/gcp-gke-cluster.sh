#!/usr/bin/env bash
# Create a regional GKE cluster with the identity and network settings the
# platform needs, add the GPU/CPU node pools, and install the add-ons.
#
#   PROJECT=my-project CLUSTER=slm-fabric LOCATION=europe-west1 \
#     deploy/cloud/gcp-gke-cluster.sh
#   ... --dry-run    # print every gcloud/helm command, create nothing
#
# Without --dry-run you need a Google identity with container.admin,
# compute.admin and iam.serviceAccountUser on PROJECT, plus helm and kubectl.
# The script prints the project and region it is about to spend money in; it
# never prints a credential.
#
# Workload Identity is bound at cluster creation (`--workload-pool`), which is
# what lets a Kubernetes service account hold a Google identity later. The GPU
# pool is created with `gpu-driver-version=latest`, so the GPU Operator is
# installed with --driver=false: two drivers on one node is a known way to
# break CUDA.
#
# Quota is not created here: nvidia-l4 (or whichever GPU_TYPE you choose) must
# be available in LOCATION, or the pool reports 0 nodes.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

CLUSTER=${CLUSTER:-slm-fabric}
LOCATION=${LOCATION:-europe-west1}
K8S_VERSION=${K8S_VERSION:-1.33}
SYSTEM_MACHINE_TYPE=${SYSTEM_MACHINE_TYPE:-e2-standard-8}
SYSTEM_MIN=${SYSTEM_MIN:-2}
SYSTEM_MAX=${SYSTEM_MAX:-4}
DRY_RUN=0

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
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

if dry_run; then
  PROJECT=${PROJECT:-<project-id>}
else
  need gcloud
  need helm
  need kubectl
  PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null)}
  if [ -z "$PROJECT" ] || [ "$PROJECT" = "(unset)" ]; then
    echo "set PROJECT to the Google Cloud project that will own the cluster" >&2
    exit 1
  fi
  echo "project $PROJECT, cluster $CLUSTER in $LOCATION" >&2
fi

run gcloud container clusters create "$CLUSTER" \
  --project "$PROJECT" \
  --location "$LOCATION" \
  --kubernetes-version "$K8S_VERSION" \
  --release-channel regular \
  --machine-type "$SYSTEM_MACHINE_TYPE" \
  --num-nodes "$SYSTEM_MIN" \
  --enable-autoscaling \
  --min-nodes "$SYSTEM_MIN" \
  --max-nodes "$SYSTEM_MAX" \
  --enable-ip-alias \
  --enable-shielded-nodes \
  --workload-pool "${PROJECT}.svc.id.goog"

run gcloud container clusters get-credentials "$CLUSTER" \
  --project "$PROJECT" \
  --location "$LOCATION"

if dry_run; then
  # Print this script's plan and the node-pool plan it delegates to.
  CLUSTER="$CLUSTER" LOCATION="$LOCATION" \
    "${ROOT}/deploy/cloud/gcp-gke-nodepools.sh" --dry-run
  "${ROOT}/deploy/cloud/bootstrap-addons.sh" --driver=false --dry-run
else
  CLUSTER="$CLUSTER" LOCATION="$LOCATION" \
    "${ROOT}/deploy/cloud/gcp-gke-nodepools.sh"

  # GKE installed the driver with the node pool; installing it again via the
  # operator can leave the node with two libcuda paths. Device plugin and NFD
  # are still needed.
  "${ROOT}/deploy/cloud/bootstrap-addons.sh" --driver=false
fi

cat <<EOF

GKE cluster is up: $CLUSTER in $PROJECT ($LOCATION).
Next:
  1. make deploy-secret                                   # tenant Secret, out of band
  2. add this cluster to config/fleet.yaml with its kubectl context
  3. point Argo CD at the repository and sync the platform Application
EOF
