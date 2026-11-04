"""Check that the deployment tree is the bundle that was signed and gated.

    python -m tools.validate_release

The chain is deliberate:

    config/release.yaml  ->  release/<id>.json (+ .sig)  ->  rendered overlays

Each link is checked against the previous one, so a release cannot be signed
for one artifact set and deployed with another. `validate_deploy.py` checks
that the manifests are internally safe; this checks that they are the right
manifests.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from registry import signing
from registry.manifest import (
    hash_tree,
    load_manifest,
    load_release_config,
    sha256_bytes,
    sha256_text_file,
    validate_manifest,
)
from tools.validate_deploy import (
    OVERLAYS,
    REPO,
    containers,
    find,
    label,
    pod_spec,
    render_overlay,
    split_image,
    workloads,
)

DEFAULT_PUBLIC_KEY = REPO / "release/keys/release.ed25519.pub.pem"


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


def _latest_manifest(repo: Path) -> Path:
    manifests = sorted((repo / "release").glob("*.json"))
    if not manifests:
        raise FileNotFoundError("no release manifest under release/; run `make release`")
    return manifests[-1]


def check_config(manifest: dict, config: dict) -> list[str]:
    errors = []
    if manifest.get("release_id") != config.get("release_id"):
        errors.append(
            f"config/release.yaml is {config.get('release_id')!r}, "
            f"manifest is {manifest.get('release_id')!r}"
        )
    if manifest.get("model") != config.get("model"):
        errors.append("config/release.yaml model block differs from the signed manifest")
    declared = [(i["name"], str(i["tag"])) for i in manifest.get("images") or []]
    configured = [(i["name"], str(i["tag"])) for i in config.get("images") or []]
    if declared != configured:
        errors.append(f"config images {configured} differ from the signed manifest {declared}")
    adapters = [(a["name"], a.get("served_name")) for a in manifest.get("adapters") or []]
    expected = [(a["name"], a.get("served_name", a["name"])) for a in config.get("adapters") or []]
    if adapters != expected:
        errors.append(f"config adapters {expected} differ from the signed manifest {adapters}")
    if manifest.get("policy", {}).get("path") != config.get("policy"):
        errors.append("config policy path differs from the signed manifest")
    return errors


def check_evidence(manifest: dict, repo: Path) -> list[str]:
    errors = []
    policy = manifest.get("policy") or {}
    policy_path = repo / str(policy.get("path"))
    if not policy_path.is_file():
        errors.append(f"policy {policy.get('path')} is missing")
    elif sha256_text_file(policy_path) != policy.get("sha256"):
        errors.append("config/policy.yaml changed since the manifest was signed")

    gate = manifest.get("gate") or {}
    for key, digest_key in (("report", "report_sha256"), ("config", "config_sha256")):
        path = repo / str(gate.get(key))
        if not path.is_file():
            errors.append(f"gate {key} {gate.get(key)} is missing")
        elif sha256_text_file(path) != gate.get(digest_key):
            errors.append(f"gate {key} {gate.get(key)} changed since the manifest was signed")
    if gate.get("baseline"):
        path = repo / gate["baseline"]
        if path.is_file() and sha256_text_file(path) != gate.get("baseline_sha256"):
            errors.append("eval baseline changed since the manifest was signed")
    if gate.get("golden"):
        path = repo / gate["golden"]
        if path.is_file() and sha256_text_file(path) != gate.get("golden_sha256"):
            errors.append("golden set changed since the gate produced the manifest")

    for name in ("benchmark", "cost"):
        record = manifest.get(name) or {}
        path = repo / str(record.get("path"))
        if not path.is_file():
            errors.append(f"{name} evidence {record.get('path')} is missing")
        elif sha256_text_file(path) != record.get("sha256"):
            errors.append(f"{name} evidence {record.get('path')} changed since signing")
    return errors


def check_adapters(manifest: dict, repo: Path) -> tuple[list[str], list[str]]:
    """Digests are re-checked where the artifacts exist; CI usually has none."""
    errors, notes = [], []
    for adapter in manifest.get("adapters") or []:
        root = repo / str(adapter.get("path"))
        if not root.is_dir():
            notes.append(
                f"adapter {adapter.get('name')}: weights not present locally, digest not re-checked"
            )
            continue
        tree = hash_tree(root)
        if tree["sha256"] != adapter.get("sha256"):
            errors.append(
                f"adapter {adapter.get('name')}: local weights {tree['sha256'][:12]} "
                f"do not match the signed {str(adapter.get('sha256'))[:12]}"
            )
    return errors, notes


def check_rendered(overlay: str, docs: list[dict], manifest: dict) -> list[str]:
    errors = []
    declared = {image["name"]: str(image["tag"]) for image in manifest.get("images") or []}

    for doc in workloads(docs):
        for container in containers(pod_spec(doc)):
            image = container.get("image")
            if not image:
                continue
            name, ref = split_image(image)
            if ref and ref.startswith("@"):
                continue  # a digest is stronger than the tag the manifest pins
            if name not in declared:
                errors.append(f"{overlay}: {label(doc)} uses undeclared image {image}")
            elif ref != declared[name]:
                errors.append(
                    f"{overlay}: {label(doc)} runs {image}, signed release declares "
                    f"{name}:{declared[name]}"
                )

    vllm = find(docs, "Deployment", "vllm-gpu")
    if vllm is None:
        errors.append(f"{overlay}: no Deployment/vllm-gpu")
    else:
        args = [
            str(arg)
            for container in pod_spec(vllm).get("containers") or []
            for arg in container.get("args") or []
        ]
        model = manifest.get("model") or {}
        if model.get("revision") not in args:
            errors.append(f"{overlay}: vLLM does not run the signed model revision")
        if "--served-model-name" in args:
            index = args.index("--served-model-name")
            served = args[index + 1] if index + 1 < len(args) else None
            if served != model.get("served_name"):
                errors.append(
                    f"{overlay}: vLLM serves {served!r}, signed release declares "
                    f"{model.get('served_name')!r}"
                )

    adapters = find(docs, "ConfigMap", "slm-adapter-set")
    if adapters is None:
        if overlay != "vanilla":
            errors.append(f"{overlay}: no ConfigMap/slm-adapter-set")
    else:
        listed = [
            line.strip()
            for line in (adapters.get("data") or {}).get("adapters.txt", "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        signed = [adapter["name"] for adapter in manifest.get("adapters") or []]
        if listed != signed:
            errors.append(f"{overlay}: adapter set {listed} is not the signed set {signed}")

    policy = next(
        (
            doc
            for doc in docs
            if doc.get("kind") == "ConfigMap"
            and str(doc.get("metadata", {}).get("name", "")).startswith("slm-policy")
        ),
        None,
    )
    if policy is None:
        errors.append(f"{overlay}: no generated slm-policy ConfigMap")
    else:
        rendered = normalize((policy.get("data") or {}).get("policy.yaml", ""))
        digest = sha256_bytes(rendered.encode("utf-8"))
        if digest != manifest.get("policy", {}).get("sha256"):
            errors.append(f"{overlay}: policy ConfigMap is not the signed policy")

    job = find(docs, "Job", "release-gate")
    if job is None:
        if overlay != "vanilla":
            errors.append(f"{overlay}: no Job/release-gate")
    else:
        args = [
            str(arg)
            for container in pod_spec(job).get("containers") or []
            for arg in container.get("args") or []
        ]
        expected = (manifest.get("gate") or {}).get("in_cluster_model")
        if expected and ("--model" not in args or args[args.index("--model") + 1] != expected):
            errors.append(f"{overlay}: release gate does not score {expected!r}")
    return errors


def check_compose(manifest: dict, repo: Path) -> list[str]:
    """The developer stack must run the same bundle the cluster is signed for."""
    path = repo / "compose.yaml"
    if not path.is_file():
        return ["compose.yaml is missing"]
    services = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("services") or {}
    vllm = services.get("vllm-gpu") or {}
    images = {image["name"]: str(image["tag"]) for image in manifest.get("images") or []}
    model = manifest.get("model") or {}

    def resolve(value) -> str:
        return re.sub(r"\$\{[A-Z0-9_]+:-([^}]*)\}", r"\1", str(value))

    errors = []
    expected_image = f"vllm/vllm-openai:{images.get('vllm/vllm-openai')}"
    if resolve(vllm.get("image")) != expected_image:
        errors.append(f"compose vLLM image is {vllm.get('image')}, release pins {expected_image}")
    command = [resolve(arg) for arg in vllm.get("command") or []]
    if "--revision" in command:
        revision = command[command.index("--revision") + 1]
        if revision != model.get("revision"):
            errors.append(f"compose pins revision {revision}, release pins {model.get('revision')}")
    else:
        errors.append("compose does not pin --revision")
    if "--served-model-name" in command:
        served = command[command.index("--served-model-name") + 1]
        if served != model.get("served_name"):
            errors.append(f"compose serves {served!r}, release serves {model.get('served_name')!r}")
    else:
        errors.append("compose does not set --served-model-name")
    return errors


def check_signature(manifest_path: Path, public_key: Path) -> list[str]:
    ok, detail = signing.verify(
        manifest_path, public_key=public_key, signature=Path(f"{manifest_path}.sig")
    )
    return [] if ok else [f"signature: {detail}"]


def validate(
    manifest: dict,
    config: dict,
    repo: Path,
    *,
    manifest_path: Path | None = None,
    public_key: Path | None = None,
    rendered: dict[str, list[dict]] | None = None,
) -> tuple[list[str], list[str]]:
    errors = list(validate_manifest(manifest))
    errors += check_config(manifest, config)
    errors += check_evidence(manifest, repo)
    adapter_errors, notes = check_adapters(manifest, repo)
    errors += adapter_errors
    errors += check_compose(manifest, repo)
    if manifest_path is not None and public_key is not None:
        errors += check_signature(manifest_path, public_key)
    for overlay, docs in (rendered or {}).items():
        errors += check_rendered(overlay, docs, manifest)
        if overlay == "vanilla":
            notes.append(
                "vanilla: adapter promotion and the in-cluster gate are operator-run; "
                "the overlay deliberately carries no PostSync hooks"
            )
    return errors, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--public-key", type=Path, default=DEFAULT_PUBLIC_KEY)
    parser.add_argument("--config", type=Path, default=REPO / "config/release.yaml")
    parser.add_argument("--overlay", action="append", choices=OVERLAYS, help="default: all")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--skip-signature", action="store_true")
    parser.add_argument("--skip-render", action="store_true")
    args = parser.parse_args(argv)

    try:
        manifest_path = args.manifest or _latest_manifest(REPO)
        manifest = load_manifest(manifest_path)
        config = load_release_config(args.config)
    except Exception as exc:  # noqa: BLE001 - report the reason and fail closed
        print(f"FAIL {exc}")
        return 1

    rendered: dict[str, list[dict]] = {}
    render_errors: list[str] = []
    if not args.skip_render:
        for overlay in args.overlay or list(OVERLAYS):
            try:
                rendered[overlay] = render_overlay(overlay, args.kubectl)
            except (RuntimeError, FileNotFoundError) as exc:
                render_errors.append(f"{overlay}: {exc}")

    errors, notes = validate(
        manifest,
        config,
        REPO,
        manifest_path=None if args.skip_signature else manifest_path,
        public_key=None if args.skip_signature else args.public_key,
        rendered=rendered,
    )
    errors += render_errors

    for note in notes:
        print(f"note {note}")
    for error in errors:
        print(f"  - {error}")
    if errors:
        print(f"FAIL release {manifest.get('release_id')}: {len(errors)} problem(s)")
        return 1
    print(
        f"OK   release {manifest.get('release_id')} = signed manifest = "
        f"{len(rendered)} overlay(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
