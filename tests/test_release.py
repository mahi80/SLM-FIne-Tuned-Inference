import json
import tempfile
import unittest
from pathlib import Path

from registry.manifest import load_manifest, load_release_config, sha256_bytes, sha256_file
from tools import validate_release as vr

REPO = Path(__file__).resolve().parent.parent
POLICY = 'version: "1"\naliases: {}\n'


def manifest_for(policy_text=POLICY):
    return {
        "schema_version": 1,
        "release_id": "slm-test-1",
        "model": {
            "id": "org/model",
            "revision": "a" * 40,
            "served_name": "slm-small",
            "quantization": "awq-w4a16",
        },
        "images": [
            {"name": "vllm/vllm-openai", "tag": "v0.33.0"},
            {"name": "slm/gateway", "tag": "0.5.0"},
            {"name": "slm/eval", "tag": "0.5.0"},
        ],
        "adapters": [
            {"name": "triage-v1", "served_name": "triage-v1"},
            {"name": "triage-v2", "served_name": "triage-v2"},
        ],
        "policy": {
            "path": "config/policy.yaml",
            "version": "1",
            "sha256": sha256_bytes(policy_text.encode()),
        },
        "gate": {
            "passed": True,
            "report_sha256": "a" * 64,
            "failed_checks": [],
            "model": "slm-triage-v2",
            "in_cluster_model": "triage-v2",
        },
    }


def deployment(name, image, args=None):
    container = {"name": name, "image": image}
    if args:
        container["args"] = args
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": "slm-fabric"},
        "spec": {"template": {"spec": {"containers": [container]}}},
    }


def docs_for(
    manifest,
    *,
    gateway_image="slm/gateway:0.5.0",
    revision=None,
    served=None,
    adapters=("triage-v1", "triage-v2"),
    policy_text=POLICY,
    gate_model="triage-v2",
):
    model = manifest["model"]
    return [
        deployment(
            "vllm-gpu",
            "vllm/vllm-openai:v0.33.0",
            [
                model["id"],
                "--revision",
                revision or model["revision"],
                "--served-model-name",
                served or model["served_name"],
            ],
        ),
        deployment("gateway", gateway_image),
        {
            "kind": "ConfigMap",
            "metadata": {"name": "slm-adapter-set"},
            "data": {"adapters.txt": "\n".join(adapters) + "\n"},
        },
        {
            "kind": "ConfigMap",
            "metadata": {"name": "slm-policy-abc123"},
            "data": {"policy.yaml": policy_text},
        },
        {
            "kind": "Job",
            "metadata": {"name": "release-gate"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "gate",
                                "image": "slm/eval:0.5.0",
                                "args": ["--model", gate_model],
                            }
                        ]
                    }
                }
            },
        },
    ]


class RenderedTests(unittest.TestCase):
    def setUp(self):
        self.manifest = manifest_for()
        self.docs = docs_for(self.manifest)

    def errors(self, docs=None):
        return vr.check_rendered("openshift", docs or self.docs, self.manifest)

    def test_consistent_overlay_passes(self):
        self.assertEqual(self.errors(), [])

    def test_undeclared_image_is_rejected(self):
        errors = self.errors(docs_for(self.manifest, gateway_image="other/gateway:9.9.9"))
        self.assertTrue(any("undeclared image" in e for e in errors))

    def test_image_tag_drift_is_rejected(self):
        errors = self.errors(docs_for(self.manifest, gateway_image="slm/gateway:0.5.1"))
        self.assertTrue(any("signed release declares slm/gateway:0.5.0" in e for e in errors))

    def test_model_revision_drift_is_rejected(self):
        errors = self.errors(docs_for(self.manifest, revision="b" * 40))
        self.assertTrue(any("signed model revision" in e for e in errors))

    def test_served_name_drift_is_rejected(self):
        errors = self.errors(docs_for(self.manifest, served="other-model"))
        self.assertTrue(any("vLLM serves" in e for e in errors))

    def test_adapter_set_drift_is_rejected(self):
        errors = self.errors(docs_for(self.manifest, adapters=("triage-v1",)))
        self.assertTrue(any("signed set" in e for e in errors))

    def test_policy_digest_drift_is_rejected(self):
        errors = self.errors(docs_for(self.manifest, policy_text='version: "2"\n'))
        self.assertTrue(any("not the signed policy" in e for e in errors))

    def test_gate_model_drift_is_rejected(self):
        errors = self.errors(docs_for(self.manifest, gate_model="triage-v1"))
        self.assertTrue(any("release gate does not score" in e for e in errors))

    def test_vanilla_without_hooks_has_no_adapter_or_gate_error(self):
        docs = [
            doc
            for doc in self.docs
            if doc["metadata"]["name"] not in {"slm-adapter-set", "release-gate"}
        ]
        errors = vr.check_rendered("vanilla", docs, self.manifest)
        self.assertEqual(errors, [])


class ConfigTests(unittest.TestCase):
    def test_drift_in_release_yaml_is_rejected(self):
        manifest = manifest_for()
        config = {
            "release_id": "slm-test-1",
            "model": dict(manifest["model"]),
            "images": [dict(i) for i in manifest["images"]],
            "adapters": [dict(a) for a in manifest["adapters"]],
            "policy": "config/policy.yaml",
        }
        self.assertEqual(vr.check_config(manifest, config), [])
        config["images"][1]["tag"] = "0.5.1"
        self.assertTrue(any("config images" in e for e in vr.check_config(manifest, config)))
        config["images"][1]["tag"] = "0.5.0"
        config["release_id"] = "slm-test-2"
        self.assertTrue(any("release.yaml is" in e for e in vr.check_config(manifest, config)))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.files = {
            "config/policy.yaml": POLICY,
            "config/gates.yaml": "thresholds: {}\ndrift: {}\n",
            "bench/results/eval.json": json.dumps({"models": []}),
            "bench/results/baseline.json": json.dumps({"models": []}),
            "src/eval/golden.jsonl": '{"id": "a"}\n',
            "bench/results/bench.json": "{}",
            "bench/results/cost.json": "{}",
        }
        for rel, text in self.files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.manifest = {
            "policy": {
                "path": "config/policy.yaml",
                "sha256": sha256_file(self.root / "config/policy.yaml"),
            },
            "gate": {
                "report": "bench/results/eval.json",
                "report_sha256": sha256_file(self.root / "bench/results/eval.json"),
                "config": "config/gates.yaml",
                "config_sha256": sha256_file(self.root / "config/gates.yaml"),
                "baseline": "bench/results/baseline.json",
                "baseline_sha256": sha256_file(self.root / "bench/results/baseline.json"),
                "golden": "src/eval/golden.jsonl",
                "golden_sha256": sha256_file(self.root / "src/eval/golden.jsonl"),
            },
            "benchmark": {
                "path": "bench/results/bench.json",
                "sha256": sha256_file(self.root / "bench/results/bench.json"),
            },
            "cost": {
                "path": "bench/results/cost.json",
                "sha256": sha256_file(self.root / "bench/results/cost.json"),
            },
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_matching_evidence_passes(self):
        self.assertEqual(vr.check_evidence(self.manifest, self.root), [])

    def test_policy_edit_after_signing_is_rejected(self):
        (self.root / "config/policy.yaml").write_text('version: "2"\n', encoding="utf-8")
        errors = vr.check_evidence(self.manifest, self.root)
        self.assertTrue(any("config/policy.yaml changed" in e for e in errors))

    def test_golden_set_edit_is_rejected(self):
        (self.root / "src/eval/golden.jsonl").write_text('{"id": "b"}\n', encoding="utf-8")
        errors = vr.check_evidence(self.manifest, self.root)
        self.assertTrue(any("golden set changed" in e for e in errors))

    def test_missing_evidence_is_rejected(self):
        (self.root / "bench/results/cost.json").unlink()
        errors = vr.check_evidence(self.manifest, self.root)
        self.assertTrue(any("cost evidence" in e for e in errors))


class ComposeTests(unittest.TestCase):
    def test_repository_compose_matches_the_committed_release(self):
        manifests = sorted((REPO / "release").glob("*.json"))
        if not manifests:
            self.skipTest("no committed release manifest")
        manifest = load_manifest(manifests[-1])
        self.assertEqual(vr.check_compose(manifest, REPO), [])

    def test_compose_revision_drift_is_rejected(self):
        manifests = sorted((REPO / "release").glob("*.json"))
        if not manifests:
            self.skipTest("no committed release manifest")
        manifest = load_manifest(manifests[-1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = (REPO / "compose.yaml").read_text(encoding="utf-8")
            text = text.replace(manifest["model"]["revision"], "c" * 40)
            (root / "compose.yaml").write_text(text, encoding="utf-8")
            errors = vr.check_compose(manifest, root)
            self.assertTrue(any("compose pins revision" in e for e in errors))


class CommittedReleaseTests(unittest.TestCase):
    def test_committed_release_and_config_agree(self):
        manifests = sorted((REPO / "release").glob("*.json"))
        if not manifests:
            self.skipTest("no committed release manifest")
        manifest = load_manifest(manifests[-1])
        config = load_release_config(REPO / "config/release.yaml")
        self.assertEqual(vr.check_config(manifest, config), [])
        self.assertEqual(vr.check_evidence(manifest, REPO), [])

    def test_full_check_without_rendering(self):
        manifests = sorted((REPO / "release").glob("*.json"))
        if not manifests:
            self.skipTest("no committed release manifest")
        manifest = load_manifest(manifests[-1])
        config = load_release_config(REPO / "config/release.yaml")
        errors, _ = vr.validate(
            manifest,
            config,
            REPO,
            manifest_path=manifests[-1],
            public_key=REPO / "release/keys/release.ed25519.pub.pem",
        )
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
