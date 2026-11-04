"""Ed25519 detached signatures over the canonical manifest.

The openssl CLI is used instead of a Python crypto dependency for three
reasons: the private key never enters this process, the verification command
is identical in CI, on an operator laptop and in an air-gapped site image, and
the toolchain is already present wherever the deployment tooling runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from .manifest import canonical_bytes, load_manifest

DEFAULT_KEY = Path("release/keys/release.ed25519.pem")
DEFAULT_PUBLIC_KEY = Path("release/keys/release.ed25519.pub.pem")


class SigningError(RuntimeError):
    """Signing or verification could not be attempted."""


def _run(args: list[str], openssl: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [openssl, *args], capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise SigningError("openssl was not found on PATH") from exc


def available(openssl: str = "openssl") -> bool:
    try:
        return _run(["version"], openssl).returncode == 0
    except SigningError:
        return False


def generate_keypair(
    private_key: Path,
    public_key: Path,
    *,
    openssl: str = "openssl",
    force: bool = False,
) -> None:
    private_key, public_key = Path(private_key), Path(public_key)
    if private_key.exists() and not force:
        raise SigningError(
            f"{private_key} exists; refusing to overwrite a signing key (use --force)"
        )
    private_key.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(["genpkey", "-algorithm", "ed25519", "-out", str(private_key)], openssl)
    if proc.returncode != 0:
        raise SigningError(proc.stderr.strip() or "key generation failed")
    try:
        os.chmod(private_key, 0o600)
    except OSError:
        pass
    proc = _run(
        ["pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)], openssl
    )
    if proc.returncode != 0:
        raise SigningError(proc.stderr.strip() or "public key export failed")


def signature_path(manifest_path: Path, signature: Path | None = None) -> Path:
    return Path(signature) if signature else Path(f"{manifest_path}.sig")


def sign(
    manifest_path: Path,
    private_key: Path = DEFAULT_KEY,
    signature: Path | None = None,
    *,
    openssl: str = "openssl",
) -> Path:
    manifest_path, private_key = Path(manifest_path), Path(private_key)
    if not private_key.is_file():
        raise SigningError(
            f"signing key {private_key} not found; run `make release-key` (keys are never committed)"
        )
    target = signature_path(manifest_path, signature)
    payload_bytes = canonical_bytes(load_manifest(manifest_path))
    with tempfile.TemporaryDirectory(prefix="slm-sign-") as tmp:
        payload = Path(tmp) / "manifest.canonical"
        payload.write_bytes(payload_bytes)
        proc = _run(
            [
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(private_key.resolve()),
                "-in",
                str(payload.resolve()),
                "-out",
                str(target.resolve()),
            ],
            openssl,
        )
    if proc.returncode != 0:
        raise SigningError(proc.stderr.strip() or "signing failed")
    return target


def verify(
    manifest_path: Path,
    public_key: Path = DEFAULT_PUBLIC_KEY,
    signature: Path | None = None,
    *,
    openssl: str = "openssl",
) -> tuple[bool, str]:
    manifest_path, public_key = Path(manifest_path), Path(public_key)
    target = signature_path(manifest_path, signature)
    for path in (manifest_path, target, public_key):
        if not Path(path).is_file():
            return False, f"missing {path}"
    try:
        payload_bytes = canonical_bytes(load_manifest(manifest_path))
    except Exception as exc:  # noqa: BLE001 - a malformed manifest is a failed verification
        return False, f"cannot canonicalise manifest: {exc}"
    with tempfile.TemporaryDirectory(prefix="slm-verify-") as tmp:
        payload = Path(tmp) / "manifest.canonical"
        payload.write_bytes(payload_bytes)
        proc = _run(
            [
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(public_key.resolve()),
                "-rawin",
                "-in",
                str(payload.resolve()),
                "-sigfile",
                str(target.resolve()),
            ],
            openssl,
        )
    if proc.returncode == 0:
        return True, "signature verified"
    detail = (proc.stderr or proc.stdout).strip().replace("\n", " ")
    return False, detail or "signature does not match"
