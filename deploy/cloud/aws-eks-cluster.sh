#!/usr/bin/env bash
# Create an EKS cluster that the node-pool contract can land on, install
# Karpenter, and hand the rest to bootstrap-addons.sh.
#
#   CLUSTER=slm-fabric REGION=eu-west-1 deploy/cloud/aws-eks-cluster.sh
#   CLUSTER=slm-fabric REGION=eu-west-1 deploy/cloud/aws-eks-cluster.sh --dry-run
#
# --dry-run prints every command and touches nothing. Without it you need an
# AWS identity, eksctl, helm and kubectl; the script never prints or stores a
# credential, only the account id it is running as.
#
# What this creates, and what it costs:
#   * a managed EKS control plane and one small managed CPU node group that
#     runs Karpenter and the platform controllers;
#   * an IAM policy, controller role and node role for Karpenter;
#   * Karpenter itself, then the GPU/CPU NodePools from
#     aws-karpenter-nodepools.yaml.
# It does not buy GPU quota: g5/g6 on-demand vCPUs must already be available in
# the region, or the GPU pods stay Pending.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

CLUSTER=${CLUSTER:-slm-fabric}
REGION=${REGION:-${AWS_REGION:-us-east-1}}
K8S_VERSION=${K8S_VERSION:-1.33}
SYSTEM_NODE_TYPE=${SYSTEM_NODE_TYPE:-m6i.large}
SYSTEM_MIN=${SYSTEM_MIN:-2}
SYSTEM_MAX=${SYSTEM_MAX:-4}
KARPENTER_VERSION=${KARPENTER_VERSION:-1.2.1}
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

exists() {
  if dry_run; then
    return 1
  fi
  "$@" >/dev/null 2>&1
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
  ACCOUNT_ID=${ACCOUNT_ID:-<account-id>}
  CONTEXT="$CLUSTER.$REGION.eksctl.io"
else
  need aws
  need eksctl
  need helm
  need kubectl
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
  echo "account $ACCOUNT_ID, cluster $CLUSTER in $REGION" >&2
fi

# --tags reaches the VPC, subnets and security groups, which is how Karpenter
# discovers where it may place nodes. A tag applied only to the node group
# would leave the NodePools unable to launch anything.
run eksctl create cluster \
  --name "$CLUSTER" \
  --region "$REGION" \
  --version "$K8S_VERSION" \
  --with-oidc \
  --managed \
  --nodegroup-name system \
  --node-type "$SYSTEM_NODE_TYPE" \
  --nodes "$SYSTEM_MIN" \
  --nodes-min "$SYSTEM_MIN" \
  --nodes-max "$SYSTEM_MAX" \
  --tags "karpenter.sh/discovery=$CLUSTER,slm.fabric/cluster=$CLUSTER"

# The controller policy is scoped to the node role it may pass to EC2 and the
# instance profile it may manage, not to the account.
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/KarpenterControllerPolicy-${CLUSTER}"
if ! exists aws iam get-policy --policy-arn "$POLICY_ARN"; then
  run aws iam create-policy \
    --policy-name "KarpenterControllerPolicy-${CLUSTER}" \
    --policy-document "file://${ROOT}/deploy/cloud/aws-karpenter-controller-policy.json"
fi

# eksctl creates the IRSA trust relationship; the role name is fixed so the
# Helm call below can address it without parsing eksctl output.
CONTROLLER_ROLE="KarpenterController-${CLUSTER}"
run eksctl create iamserviceaccount \
  --cluster "$CLUSTER" \
  --region "$REGION" \
  --namespace kube-system \
  --name karpenter \
  --role-name "$CONTROLLER_ROLE" \
  --attach-policy-arn "$POLICY_ARN" \
  --override-existing-serviceaccounts \
  --approve

# Nodes Karpenter launches need their own role: it must not inherit the
# controller's permissions, and the EC2 trust policy is what makes it a node.
NODE_ROLE="KarpenterNodeRole-${CLUSTER}"
INSTANCE_PROFILE="KarpenterNodeInstanceProfile-${CLUSTER}"
TRUST=$(mktemp)
NODEPOOLS=""
trap 'rm -f "$TRUST" ${NODEPOOLS:+"$NODEPOOLS"}' EXIT
cat >"$TRUST" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "ec2.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }
  ]
}
JSON

if ! exists aws iam get-role --role-name "$NODE_ROLE"; then
  run aws iam create-role \
    --role-name "$NODE_ROLE" \
    --assume-role-policy-document "file://$TRUST"
fi
for policy in AmazonEKSWorkerNodePolicy AmazonEKS_CNI_Policy AmazonEC2ContainerRegistryReadOnly; do
  run aws iam attach-role-policy \
    --role-name "$NODE_ROLE" \
    --policy-arn "arn:aws:iam::aws:policy/$policy"
done

if ! exists aws iam get-instance-profile --instance-profile-name "$INSTANCE_PROFILE"; then
  run aws iam create-instance-profile --instance-profile-name "$INSTANCE_PROFILE"
  run aws iam add-role-to-instance-profile \
    --instance-profile-name "$INSTANCE_PROFILE" \
    --role-name "$NODE_ROLE"
fi

run helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter \
  --namespace kube-system \
  --version "$KARPENTER_VERSION" \
  --set "settings.clusterName=$CLUSTER" \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=arn:aws:iam::${ACCOUNT_ID}:role/${CONTROLLER_ROLE}" \
  --wait --timeout 10m

# The manifest pins the default cluster name in two places; render it for this
# cluster instead of editing the committed contract.
NODEPOOLS=$(mktemp)
sed \
  -e "s/KarpenterNodeRole-slm-fabric/KarpenterNodeRole-${CLUSTER}/g" \
  -e "s/karpenter\.sh\/discovery: slm-fabric/karpenter.sh\/discovery: ${CLUSTER}/g" \
  "${ROOT}/deploy/cloud/aws-karpenter-nodepools.yaml" >"$NODEPOOLS"
run kubectl apply -f "$NODEPOOLS"

# EKS accelerated AL2023 images already carry the NVIDIA driver; the operator
# adds the device plugin and NFD.
if dry_run; then
  "${ROOT}/deploy/cloud/bootstrap-addons.sh" --driver=false --dry-run
else
  "${ROOT}/deploy/cloud/bootstrap-addons.sh" --driver=false
fi

cat <<EOF

EKS cluster is up: $CLUSTER ($REGION), context $CONTEXT.
Next:
  1. make deploy-secret                                   # tenant Secret, out of band
  2. add this cluster to config/fleet.yaml with context $CONTEXT
  3. point Argo CD at the repository and sync the platform Application
EOF
