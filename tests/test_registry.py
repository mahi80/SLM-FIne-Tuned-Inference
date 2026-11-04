import json
import shutil
import tempfile
import unittest
from pathlib import Path

from registry import signing
from registry.manifest import (
    ManifestError,
    build_manifest,
    canonical_bytes,
    hash_tree,
    load_manifest,
    load_release_config,
    sha256_bytes,
    sha256_file,
    validate_manifest,
)

REPO = Path(__file__).resolve().parent.parent
HAS_OPENSSL = signing.available()


def gate_report(passed=True, exact=0.83):
    checks = [
        {"name": "exact_labels", "actual": exact, "limit": 0.7, "ok": passed, "detail": ""},
        {"name": "schema", "actual": 1.0, "limit": 0.99, "ok": True, "detail": ""},
    ]
    return {
        "golden": "golden.jsonl",
        "golden_sha256": "a" * 64,
        "n": 24,
        "models": [
            {
                "model": "candidate",
                "n": 24,
                "strict_json_rate": 1.0,
                "schema_rate": 1.0,
                "exact_labels_rate": exact,
                "slices": {"clean": {"n": 8, "schema": 1.0, "exact": exact}},
                "gate": {
                    "model": "candidate",
                    "passed": passed,
                    "checks": checks,
                },
            }
        ],
    }


def release_config():
    return {
        "release_id": "slm-test-1",
        "model": {
            "id": "org/model",
            "revision": "b" * 40,
            "served_name": "slm-small",
            "quantization": "awq-w4a16",
        },
        "images": [{"name": "slm/gateway", "tag": "1.2.3"}],
        "adapters": [{"name": "a1", "served_name": "a1"}],
        "policy": "config/policy.yaml",
        "gate": {
            "model": "candidate",
            "report": "bench/results/eval.json",
            "config": "config/gates.yaml",
            "baseline": "bench/results/baseline.json",
        },
        "evidence": {
            "benchmark": "bench/results/bench.json",
            "cost": "bench/results/cost.json",
        },
    }


class FixtureRepo:
    def __init__(self, root: Path):
        self.root = root
        (root / "config").mkdir(parents=True)
        (root / "adapters/a1").mkdir(parents=True)
        (root / "bench/results").mkdir(parents=True)
        (root / "config/policy.yaml").write_text('version: "1.0"\naliases: {}\n', encoding="utf-8")
        (root / "config/gates.yaml").write_text("thresholds: {}\ndrift: {}\n", encoding="utf-8")
        (root / "adapters/a1/adapter_config.json").write_text('{"r": 16}\n', encoding="utf-8")
        (root / "adapters/a1/adapter_model.safetensors").write_bytes(b"\x00" * 64)
        (root / "bench/results/eval.json").write_text(
            json.dumps(gate_report()), encoding="utf-8"
        )
        (root / "bench/results/bench.json").write_text(
            json.dumps(
                {
                    "runs": [
                        {
                            "label": "gpu-direct c=1",
                            "output_tokens_per_s": 125.5,
                            "concurrency": 1,
                        },
                        {
                            "label": "gpu-direct c=12",
                            "output_tokens_per_s": 1123.2,
                            "concurrency": 12,
                        },
                        {
                            "label": "cpu-sim",
                            "output_tokens_per_s": 16.0,
                            "concurrency": 1,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        (root / "bench/results/cost.json").write_text(
            json.dumps(
                {
                    "workload": {"requests_per_day": 20000},
                    "tiers": [
                        {"tier": "gpu-owned", "usd_per_1m_at_workload": 2.24},
                    ],
                    "break_even_vs_owned": [
                        {"tier": "gpu-l4-4b", "requests_per_day": 56069.0}
                    ],
                }
            ),
            encoding="utf-8",
        )


class CanonicalTests(unittest.TestCase):
    def test_key_order_and_formatting_do_not_change_bytes(self):
        a = {"b": 1, "a": [1, 2], "c": {"z": True, "y": None}}
        b = {"c": {"y": None, "z": True}, "a": [1, 2], "b": 1}
        self.assertEqual(canonical_bytes(a), canonical_bytes(b))

    def test_value_change_changes_digest(self):
        self.assertNotEqual(
            sha256_bytes(canonical_bytes({"tag": "1.0.0"})),
            sha256_bytes(canonical_bytes({"tag": "1.0.1"})),
        )


class HashTreeTests(unittest.TestCase):
    def test_digest_covers_paths_and_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.bin").write_bytes(b"one")
            first = hash_tree(root)
            (root / "a.bin").write_bytes(b"two")
            second = hash_tree(root)
            self.assertNotEqual(first["sha256"], second["sha256"])
            (root / "a.bin").write_bytes(b"one")
            (root / "b.bin").write_bytes(b"one")
            third = hash_tree(root)
            self.assertNotEqual(first["sha256"], third["sha256"])
            self.assertEqual([f["path"] for f in third["files"]], ["a.bin", "b.bin"])

    def test_empty_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ManifestError):
                hash_tree(Path(tmp))


class BuildTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        FixtureRepo(self.root)
        self.config = release_config()

    def tearDown(self):
        self._tmp.cleanup()

    def manifest(self, **kwargs):
        kwargs.setdefault("created_at", "2026-11-04T00:00:00Z")
        return build_manifest(self.root, self.config, adapters_dir=self.root / "adapters", **kwargs)

    def test_records_digests_and_gate(self):
        manifest = self.manifest()
        self.assertEqual(validate_manifest(manifest), [])
        self.assertEqual(manifest["gate"]["passed"], True)
        self.assertEqual(manifest["gate"]["checks_total"], 2)
        self.assertEqual(manifest["policy"]["sha256"], sha256_file(self.root / "config/policy.yaml"))
        self.assertEqual(
            manifest["adapters"][0]["sha256"],
            hash_tree(self.root / "adapters/a1")["sha256"],
        )
        self.assertEqual(manifest["benchmark"]["gpu_output_tps_max"], 1123.2)
        self.assertEqual(manifest["benchmark"]["gpu_concurrency_at_max"], 12)
        self.assertEqual(manifest["cost"]["owned_usd_per_1m_at_workload"], 2.24)
        self.assertEqual(manifest["created_at"], "2026-11-04T00:00:00Z")

    def test_failing_gate_blocks_the_manifest(self):
        (self.root / "bench/results/eval.json").write_text(
            json.dumps(gate_report(passed=False)), encoding="utf-8"
        )
        with self.assertRaises(ManifestError) as caught:
            self.manifest()
        self.assertIn("release gate failed", str(caught.exception))

    def test_missing_adapter_is_not_silently_skipped(self):
        shutil.rmtree(self.root / "adapters/a1")
        with self.assertRaises(ManifestError):
            self.manifest()


class ValidationTests(unittest.TestCase):
    def base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            FixtureRepo(root)
            return build_manifest(
                root,
                release_config(),
                adapters_dir=root / "adapters",
                created_at="2026-11-04T00:00:00Z",
            )

    def test_unpinned_image_is_rejected(self):
        manifest = self.base()
        manifest["images"][0]["tag"] = "latest"
        self.assertTrue(any("not pinned" in p for p in validate_manifest(manifest)))

    def test_missing_revision_is_rejected(self):
        manifest = self.base()
        del manifest["model"]["revision"]
        self.assertTrue(any("model.revision" in p for p in validate_manifest(manifest)))

    def test_short_adapter_digest_is_rejected(self):
        manifest = self.base()
        manifest["adapters"][0]["sha256"] = "abc"
        self.assertTrue(any("tree digest" in p for p in validate_manifest(manifest)))


@unittest.skipUnless(HAS_OPENSSL, "openssl CLI is required for signing tests")
class SigningTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.private = self.root / "keys/release.pem"
        self.public = self.root / "keys/release.pub.pem"
        signing.generate_keypair(self.private, self.public)
        self.manifest = self.root / "release.json"
        self.manifest.write_text(
            json.dumps({"release_id": "slm-test-1", "model": {"revision": "a" * 40}}),
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip_and_determinism(self):
        first = signing.sign(self.manifest, self.private, openssl="openssl")
        ok, detail = signing.verify(self.manifest, self.public, first)
        self.assertTrue(ok, detail)
        signature = first.read_bytes()
        second = signing.sign(self.manifest, self.private, self.root / "again.sig")
        self.assertEqual(signature, second.read_bytes())

    def test_reformatting_does_not_break_the_signature(self):
        signature = signing.sign(self.manifest, self.private)
        self.manifest.write_text(
            json.dumps(json.loads(self.manifest.read_text()), indent=4, sort_keys=False),
            encoding="utf-8",
        )
        ok, detail = signing.verify(self.manifest, self.public, signature)
        self.assertTrue(ok, detail)

    def test_any_value_change_breaks_the_signature(self):
        signature = signing.sign(self.manifest, self.private)
        data = json.loads(self.manifest.read_text())
        data["model"]["revision"] = "b" * 40
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        ok, detail = signing.verify(self.manifest, self.public, signature)
        self.assertFalse(ok)

    def test_missing_signature_is_a_failed_verification(self):
        ok, detail = signing.verify(self.manifest, self.public, self.root / "absent.sig")
        self.assertFalse(ok)
        self.assertIn("missing", detail)

    def test_key_generation_refuses_to_overwrite(self):
        with self.assertRaises(signing.SigningError):
            signing.generate_keypair(self.private, self.public)


class CommittedReleaseTests(unittest.TestCase):
    """The release that ships with the repository."""

    def manifest_path(self):
        manifests = sorted((REPO / "release").glob("*.json"))
        if not manifests:
            self.skipTest("no committed release manifest")
        return manifests[-1]

    def test_manifest_matches_config_release_yaml(self):
        manifest = load_manifest(self.manifest_path())
        config = load_release_config(REPO / "config/release.yaml")
        self.assertEqual(manifest["release_id"], config["release_id"])
        self.assertEqual(manifest["model"]["revision"], config["model"]["revision"])
        self.assertEqual(
            [(i["name"], i["tag"]) for i in manifest["images"]],
            [(i["name"], str(i["tag"])) for i in config["images"]],
        )
        self.assertEqual(
            [a["name"] for a in manifest["adapters"]],
            [a["name"] for a in config["adapters"]],
        )
        self.assertEqual(validate_manifest(manifest), [])

    @unittest.skipUnless(HAS_OPENSSL, "openssl CLI is required for signature verification")
    def test_committed_signature_verifies_with_the_committed_public_key(self):
        manifest = self.manifest_path()
        ok, detail = signing.verify(
            manifest,
            public_key=REPO / "release/keys/release.ed25519.pub.pem",
            signature=Path(f"{manifest}.sig"),
        )
        self.assertTrue(ok, detail)

    def test_policy_digest_matches_the_file(self):
        manifest = load_manifest(self.manifest_path())
        self.assertEqual(
            manifest["policy"]["sha256"],
            sha256_file(REPO / manifest["policy"]["path"]),
        )


if __name__ == "__main__":
    unittest.main()
