#!/usr/bin/env bash
# Mark existing on-prem GPU workers so the deployment can find them.
#
#   deploy/cloud/onprem-workers.sh gpu-worker-0 gpu-worker-1
#   deploy/cloud/onprem-workers.sh --dry-run gpu-worker-0
#
# The nodes are provisioned by the platform team - by a MachineSet on vSphere or
# bare metal, or by hand. OpenShift cannot guess which workers have a GPU, so the
# names are passed explicitly rather than selected by a label nobody has applied
# yet. This is idempotent; re-run it after adding a node.
#
# For clusters installed with MachineSets, scale the GPU MachineSet first:
#   oc scale machineset -n openshift-machine-api <cluster-id>-gpu-worker --replicas=2
set -euo pipefail

DRY_RUN=0
NODES=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -*) echo "unknown argument: $arg" >&2; exit 2 ;;
    *) NODES+=("$arg") ;;
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

if [ "${#NODES[@]}" -eq 0 ]; then
  echo "usage: $0 [--dry-run] <gpu-node> [<gpu-node> ...]" >&2
  exit 2
fi

for node in "${NODES[@]}"; do
  # Same label and taint Karpenter or a managed node pool applies, so one set of
  # manifests schedules on-prem and cloud workers alike.
  run oc label node "$node" --overwrite \
    nvidia.com/gpu.present=true slm.fabric/pool=gpu
  run oc adm taint nodes "$node" nvidia.com/gpu=present:NoSchedule --overwrite
done

run oc get nodes -l nvidia.com/gpu.present=true \
  -o custom-columns='NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu,TAINTS:.spec.taints[*].key'
