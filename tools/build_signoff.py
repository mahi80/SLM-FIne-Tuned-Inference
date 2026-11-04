"""Write the machine-readable evidence snapshot for the sign-off pack.

    python -m tools.build_signoff [--out docs/signoff/evidence.json]

Runs the unit suite, verifies the signed release and hashes the artifacts the pack
cites. The output is committed, so a reviewer can diff two snapshots and see exactly
which evidence moved between releases.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from registry import signing
from registry.manifest import (
    canonical_bytes,
    git_state,
    load_manifest,
    sha256_bytes,
    sha256_text_file,
)

REPO = Path(__file__).resolve().parent.parent
ARTIFACTS = (
    "config/policy.yaml",
    "config/release.yaml",
    "config/eval-gates.yaml",
    "config/cost.yaml",
    "src/eval/golden.jsonl",
    "bench/results/eval-gate.json",
    "bench/results/eval-baseline.json",
    "bench/results/tier-comparison.json",
    "bench/results/cost-model.json",
    "compose.yaml",
)


def run_tests(repo: Path) -> dict:
    loader = unittest.TestLoader()
    suite = loader.discover(str(repo / "tests"))
    with open(os.devnull, "w", encoding="utf-8") as sink:
        result = unittest.TextTestRunner(stream=sink, verbosity=0).run(suite)
    return {
        "ran": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "ok": result.wasSuccessful(),
    }


def openssl_version() -> str | None:
    try:
        proc = subprocess.run(
            ["openssl", "version"], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def release_record(repo: Path) -> dict | None:
    manifests = sorted((repo / "release").glob("*.json"))
    if not manifests:
        return None
    path = manifests[-1]
    manifest = load_manifest(path)
    public_key = repo / "release/keys/release.ed25519.pub.pem"
    signature = Path(f"{path}.sig")
    if signing.available() and signature.is_file() and public_key.is_file():
        verified, detail = signing.verify(path, public_key=public_key, signature=signature)
    else:
        verified, detail = None, "openssl, public key or signature unavailable"
    return {
        "manifest": path.relative_to(repo).as_posix(),
        "manifest_sha256": sha256_text_file(path),
        "canonical_sha256": sha256_bytes(canonical_bytes(manifest)),
        "signature": signature.name,
        "signature_verified": verified,
        "signature_detail": detail,
        "release_id": manifest.get("release_id"),
        "model": manifest.get("model"),
        "policy_version": (manifest.get("policy") or {}).get("version"),
        "gate": {
            "passed": (manifest.get("gate") or {}).get("passed"),
            "model": (manifest.get("gate") or {}).get("model"),
            "metrics": (manifest.get("gate") or {}).get("metrics"),
            "checks_total": (manifest.get("gate") or {}).get("checks_total"),
        },
        "adapters": [
            {"name": a.get("name"), "sha256": a.get("sha256")}
            for a in manifest.get("adapters") or []
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--out", type=Path, default=REPO / "docs/signoff/evidence.json")
    parser.add_argument("--skip-tests", action="store_true", help="for quick local runs only")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()

    tests = {"ran": 0, "ok": None, "skipped": "not run"} if args.skip_tests else run_tests(repo)
    release = release_record(repo)
    artifacts = {}
    for relative in ARTIFACTS:
        path = repo / relative
        artifacts[relative] = sha256_text_file(path) if path.is_file() else None

    evidence = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": git_state(repo),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "openssl": openssl_version(),
        },
        "tests": tests,
        "release": release,
        "artifacts": artifacts,
        "notes": [
            "Artifacts are hashed as they exist in the working tree at generation time.",
            "The release signature is verified against the committed public key, not a trust root.",
            "Evidence generated from the commit named in source; it is committed in the following commit.",
        ],
    }

    out = args.out if args.out.is_absolute() else repo / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )

    print(f"evidence  {out.relative_to(repo).as_posix()}")
    print(f"tests     {tests}")
    if release:
        print(
            f"release   {release['release_id']} signature="
            f"{'verified' if release['signature_verified'] else release['signature_verified']}"
        )
    else:
        print("release   no signed manifest")

    failed = tests.get("ok") is False or (release is not None and release["signature_verified"] is False)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
