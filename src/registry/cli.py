"""Build, sign, inspect and verify model release manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import signing
from .manifest import (
    ManifestError,
    build_manifest,
    canonical_bytes,
    load_manifest,
    load_release_config,
    sha256_bytes,
    summarize,
    validate_manifest,
)

REPO = Path(__file__).resolve().parent.parent.parent


def _latest_manifest(repo: Path) -> Path:
    candidates = sorted((repo / "release").glob("*.json"))
    if not candidates:
        raise ManifestError("no release manifest found under release/")
    return candidates[-1]


def _print_manifest(manifest: dict, path: Path) -> None:
    for line in summarize(manifest):
        print(line)
    digest = sha256_bytes(canonical_bytes(manifest))
    print(f"canonical {digest}")
    print(f"file      {path}")


def cmd_build(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    config = load_release_config(repo / args.config)
    manifest = build_manifest(
        repo,
        config,
        adapters_dir=repo / args.adapters,
        created_at=args.created_at,
    )
    out = Path(args.out) if args.out else repo / "release" / f"{manifest['release_id']}.json"
    if out.exists() and not args.force:
        raise ManifestError(f"{out} exists; pass --force to replace it")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    _print_manifest(manifest, out)
    print("next      sign it: python -m registry sign --manifest " + str(out.relative_to(repo)))
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    manifest_path = Path(args.manifest) if args.manifest else _latest_manifest(repo)
    target = signing.sign(
        manifest_path,
        private_key=Path(args.key),
        signature=Path(args.signature) if args.signature else None,
    )
    ok, detail = signing.verify(
        manifest_path,
        public_key=Path(args.public_key),
        signature=target,
    )
    if not ok:
        print(f"FAIL signature written to {target} but does not verify: {detail}")
        return 1
    print(f"signed    {manifest_path} -> {target}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    manifest_path = Path(args.manifest) if args.manifest else _latest_manifest(repo)
    manifest = load_manifest(manifest_path)
    problems = validate_manifest(manifest)
    if problems:
        print("FAIL manifest is not releasable:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    ok, detail = signing.verify(
        manifest_path,
        public_key=Path(args.public_key),
        signature=Path(args.signature) if args.signature else None,
    )
    if not ok:
        print(f"FAIL {manifest_path}: {detail}")
        return 1
    print(f"OK       {manifest_path}: {detail}")
    print(f"release  {manifest['release_id']} gate=PASS "
          f"exact={manifest['gate']['metrics']['exact_labels_rate']}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    manifest_path = Path(args.manifest) if args.manifest else _latest_manifest(repo)
    manifest = load_manifest(manifest_path)
    problems = validate_manifest(manifest)
    _print_manifest(manifest, manifest_path)
    if problems:
        print("problems  " + "; ".join(problems))
        return 1
    return 0


def cmd_keygen(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    out = repo / args.out_dir
    private_key = out / "release.ed25519.pem"
    public_key = out / "release.ed25519.pub.pem"
    signing.generate_keypair(private_key, public_key, force=args.force)
    print(f"private   {private_key} (gitignored)")
    print(f"public    {public_key} (commit this)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(REPO), help="repository root")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build a manifest from the current artifacts")
    build.add_argument("--config", default="config/release.yaml")
    build.add_argument("--adapters", default="adapters")
    build.add_argument("--out", default="")
    build.add_argument("--created-at", default=None, help="fixed timestamp, for tests")
    build.add_argument("--force", action="store_true")
    build.set_defaults(func=cmd_build)

    sign = sub.add_parser("sign", help="sign a manifest with the local Ed25519 key")
    sign.add_argument("--manifest", default="")
    sign.add_argument("--key", default=str(signing.DEFAULT_KEY))
    sign.add_argument("--signature", default="")
    sign.add_argument("--public-key", default=str(signing.DEFAULT_PUBLIC_KEY))
    sign.set_defaults(func=cmd_sign)

    verify = sub.add_parser("verify", help="verify a manifest signature and required fields")
    verify.add_argument("--manifest", default="")
    verify.add_argument("--signature", default="")
    verify.add_argument("--public-key", default=str(signing.DEFAULT_PUBLIC_KEY))
    verify.set_defaults(func=cmd_verify)

    inspect = sub.add_parser("inspect", help="print a manifest summary")
    inspect.add_argument("--manifest", default="")
    inspect.set_defaults(func=cmd_inspect)

    keygen = sub.add_parser("keygen", help="generate a release signing keypair")
    keygen.add_argument("--out-dir", default="release/keys")
    keygen.add_argument("--force", action="store_true")
    keygen.set_defaults(func=cmd_keygen)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ManifestError, signing.SigningError) as exc:
        print(f"FAIL {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
