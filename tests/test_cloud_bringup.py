"""Cloud bring-up scripts are plans, not promises.

None of these scripts can be pointed at a real account in CI, so the tests hold
the parts that are still true offline: every provider CLI call is reachable via
`--dry-run` without credentials, the labels and taints still come from the
capacity registry, chart versions are pinned, and no secret is committed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLOUD = REPO / "deploy/cloud"

NODE_POOL_SCRIPTS = {"azure-aks-nodepools.sh", "gcp-gke-nodepools.sh", "onprem-workers.sh"}
CLUSTER_SCRIPTS = {"aws-eks-cluster.sh", "azure-aks-cluster.sh", "gcp-gke-cluster.sh"}
ADDON_SCRIPTS = {"bootstrap-addons.sh"}

# A credential in the repository is a defect even if the script never runs:
# a clone is the whole distribution channel.
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)aws_secret_access_key\s*=\s*['\"]?[A-Za-z0-9/+=]{20,}"),
    re.compile(r"(?i)--password\s+\S+"),
    re.compile(r"(?i)client_secret\s*=\s*['\"][^'\"]+['\"]"),
]


def run_dry(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run(
        ["bash", f"deploy/cloud/{script}", "--dry-run"],
        cwd=REPO,
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


class ScriptInventory(unittest.TestCase):
    def test_every_cloud_script_is_classified(self):
        """A new script has to be placed in one of the three groups on purpose."""
        found = {path.name for path in CLOUD.glob("*.sh")}
        self.assertEqual(found, NODE_POOL_SCRIPTS | CLUSTER_SCRIPTS | ADDON_SCRIPTS)

    def test_scripts_run_under_strict_mode(self):
        for path in sorted(CLOUD.glob("*.sh")):
            with self.subTest(script=path.name):
                self.assertIn("set -euo pipefail", path.read_text(encoding="utf-8"))

    def test_no_credentials_are_committed(self):
        for path in sorted(list(CLOUD.glob("*.sh")) + list(CLOUD.glob("*.json"))):
            text = path.read_text(encoding="utf-8")
            for pattern in SECRET_PATTERNS:
                with self.subTest(file=path.name, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text))


class ClusterBringUp(unittest.TestCase):
    CASES = {
        "aws-eks-cluster.sh": (
            {"CLUSTER": "test-cluster", "REGION": "eu-west-1"},
            "eksctl create cluster",
            "kubectl apply -f",
        ),
        "azure-aks-cluster.sh": (
            {"RESOURCE_GROUP": "test-rg", "CLUSTER": "test-cluster", "LOCATION": "westeurope"},
            "az aks create",
            "az aks nodepool add",
        ),
        "gcp-gke-cluster.sh": (
            {"PROJECT": "demo-project", "CLUSTER": "test-cluster", "LOCATION": "europe-west1"},
            "gcloud container clusters create",
            "gcloud container node-pools create",
        ),
    }

    def test_each_script_prints_its_plan_without_a_provider_cli(self):
        for script, (env, cluster_call, delegated_call) in self.CASES.items():
            with self.subTest(script=script):
                result = run_dry(script, env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(cluster_call, result.stdout)
                self.assertIn(delegated_call, result.stdout)
                # The add-on plan is printed by the add-on script itself, so
                # seeing its first install proves the hand-off happened.
                self.assertIn("helm upgrade --install gpu-operator", result.stdout)

    def test_cluster_scripts_apply_the_same_contract_as_the_node_pools(self):
        """The bring-up scripts delegate; they never redefine the label/taint."""
        delegates = {
            "aws-eks-cluster.sh": "aws-karpenter-nodepools.yaml",
            "azure-aks-cluster.sh": "azure-aks-nodepools.sh",
            "gcp-gke-cluster.sh": "gcp-gke-nodepools.sh",
        }
        for script, target in delegates.items():
            with self.subTest(script=script):
                self.assertTrue((CLOUD / target).is_file())
                text = (CLOUD / script).read_text(encoding="utf-8")
                self.assertIn(target, text)

    def test_aws_bringup_enables_oidc_and_installs_karpenter(self):
        text = (CLOUD / "aws-eks-cluster.sh").read_text(encoding="utf-8")
        for expected in (
            "--with-oidc",
            "karpenter.sh/discovery=",
            "eksctl create iamserviceaccount",
            "aws iam create-role",
            "KarpenterNodeRole-",
            "--override-existing-serviceaccounts",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_karpenter_policy_cannot_pass_any_role(self):
        policy = json.loads((CLOUD / "aws-karpenter-controller-policy.json").read_text(encoding="utf-8"))
        statements = policy["Statement"]
        actions = {action for statement in statements for action in _actions(statement)}
        for required in (
            "ec2:CreateFleet",
            "ec2:RunInstances",
            "ec2:TerminateInstances",
            "eks:DescribeCluster",
            "ssm:GetParameter",
        ):
            with self.subTest(action=required):
                self.assertIn(required, actions)
        self.assertNotIn("*", actions)
        pass_role = [s for s in statements if "iam:PassRole" in _actions(s)]
        self.assertEqual(len(pass_role), 1)
        self.assertEqual(
            pass_role[0]["Condition"]["StringEquals"]["iam:PassedToService"],
            "ec2.amazonaws.com",
        )

    def test_azure_bringup_uses_workload_identity_and_a_user_kubeconfig(self):
        text = (CLOUD / "azure-aks-cluster.sh").read_text(encoding="utf-8")
        for expected in (
            "--enable-oidc-issuer",
            "--enable-workload-identity",
            "--network-policy calico",
            "az aks get-credentials",
            "--overwrite-existing",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)
        # --admin would hand out a certificate identity that cannot be revoked
        # per person; the normal kubeconfig is the supported path.
        self.assertNotIn("--admin", "\n".join(_commands(text)))

    def test_gcp_bringup_binds_workload_identity(self):
        text = (CLOUD / "gcp-gke-cluster.sh").read_text(encoding="utf-8")
        for expected in ("--workload-pool", "--enable-ip-alias", "--enable-shielded-nodes"):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)


class AddOnInstall(unittest.TestCase):
    CHARTS = ("gpu-operator", "keda", "argocd", "kyverno")

    def test_every_chart_is_pinned_and_waited_for(self):
        text = (CLOUD / "bootstrap-addons.sh").read_text(encoding="utf-8")
        installs = {
            command.split("helm upgrade --install ", 1)[1].split()[0]: command
            for command in _logical_lines(text)
            if "helm upgrade --install" in command
        }
        self.assertEqual(set(installs), set(self.CHARTS))
        for chart, command in installs.items():
            with self.subTest(chart=chart):
                self.assertIn('--version "$', command)
                self.assertIn("--wait --timeout", command)

    def test_the_driver_choice_follows_the_provider(self):
        """AKS passes --gpu-driver none; the operator must install the driver."""
        aks = (CLOUD / "azure-aks-nodepools.sh").read_text(encoding="utf-8")
        self.assertIn("--gpu-driver none", aks)
        self.assertIn("--driver=true", (CLOUD / "azure-aks-cluster.sh").read_text(encoding="utf-8"))
        for script in ("aws-eks-cluster.sh", "gcp-gke-cluster.sh"):
            self.assertIn("--driver=false", (CLOUD / script).read_text(encoding="utf-8"))

    def test_dry_run_names_every_component_and_the_driver_choice(self):
        result = run_dry("bootstrap-addons.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        for chart in self.CHARTS:
            self.assertIn(f"helm upgrade --install {chart} ", result.stdout)
        self.assertIn("driver.enabled=true", result.stdout)

        off = subprocess.run(
            ["bash", "deploy/cloud/bootstrap-addons.sh", "--driver=false", "--dry-run"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertIn("driver.enabled=false", off.stdout)

    def test_gitops_and_keda_can_be_skipped(self):
        result = subprocess.run(
            ["bash", "deploy/cloud/bootstrap-addons.sh", "--skip-keda", "--skip-gitops", "--dry-run"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("helm upgrade --install keda ", result.stdout)
        self.assertNotIn("helm upgrade --install argocd ", result.stdout)
        self.assertNotIn("helm upgrade --install kyverno ", result.stdout)


def _commands(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def _logical_lines(text: str) -> list[str]:
    """Join backslash continuations so a wrapped command reads as one line."""
    joined: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        joined.append((pending + line).strip())
        pending = ""
    if pending:
        joined.append(pending.strip())
    return joined


def _actions(statement: dict) -> list[str]:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else list(action)


if __name__ == "__main__":
    unittest.main()
