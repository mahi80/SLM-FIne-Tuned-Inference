"""Release manifests: one signed statement of what a deployment contains.

A manifest records hashes, not artefacts. It stays small enough to review in a
pull request, and every entry can be re-hashed from the repository or from the
artifact store it names.

Signing covers the canonical JSON form, so reformatting the file is free but
changing a single value invalidates the signature. Numbers and ordering are
canonicalised on purpose: two machines that build the same bundle describe it
with identical bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

SCHEMA_VERSION = 1
RELEASE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{7,40}$")
NOTES = (
    "Adapter weights are training artifacts and are not committed; the manifest pins their digests.",
    "CPU, cloud and GPU-reference benchmark tiers are MODELLED; measured rows are labelled in tier-comparison.json.",
    "The source commit names the tree the manifest was built from; it is not the commit that contains the manifest.",
)


class ManifestError(RuntimeError):
    """The inputs do not describe a releasable bundle."""


def canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_tree(root: Path) -> dict:
    """Digest a directory by path and content, so a rename is a change too."""
    root = Path(root)
    if not root.is_dir():
        raise ManifestError(f"artifact directory {root} does not exist")
    files = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    if not files:
        raise ManifestError(f"artifact directory {root} is empty")
    tree = hashlib.sha256()
    for entry in sorted(files, key=lambda item: item["path"]):
        tree.update(f"{entry['sha256']}  {entry['path']}\n".encode("utf-8"))
    return {"sha256": tree.hexdigest(), "files": files}


def git_state(repo: Path) -> dict:
    """Tracked-file state only: release artefacts are usually untracked."""

    def run(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise ManifestError(proc.stderr.strip() or "git failed")
        return proc.stdout.strip()

    try:
        return {"commit": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain", "--untracked-files=no"))}
    except (OSError, ManifestError):
        return {"commit": None, "dirty": None}


def load_release_config(path: Path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ManifestError(f"{path} is not a mapping")
    for key in ("release_id", "model", "images", "adapters", "gate", "evidence"):
        if key not in data:
            raise ManifestError(f"{path} is missing '{key}'")
    return data


def _read_json(path: Path) -> dict:
    if not Path(path).is_file():
        raise ManifestError(f"missing evidence file {path}")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path} is not valid JSON: {exc}") from exc


def _gate_record(repo: Path, gate_config: dict) -> dict:
    report_path = repo / gate_config["report"]
    config_path = repo / gate_config["config"]
    report = _read_json(report_path)
    model = gate_config["model"]
    entry = next((m for m in report.get("models", []) if m.get("model") == model), None)
    if entry is None:
        raise ManifestError(f"{report_path} has no entry for model '{model}'")
    gate = entry.get("gate")
    if not isinstance(gate, dict):
        raise ManifestError(f"{report_path} has no gate result for '{model}' (run make eval-gate)")
    if gate.get("passed") is not True:
        failed = [c["name"] for c in gate.get("checks", []) if not c.get("ok")]
        raise ManifestError(f"release gate failed for '{model}': {', '.join(failed) or 'unknown'}")
    record = {
        "report": gate_config["report"],
        "report_sha256": sha256_file(report_path),
        "config": gate_config["config"],
        "config_sha256": sha256_file(config_path),
        "model": model,
        "golden": report.get("golden"),
        "golden_sha256": report.get("golden_sha256"),
        "n": entry.get("n"),
        "passed": bool(gate.get("passed")),
        "checks_total": len(gate.get("checks") or []),
        "failed_checks": [c["name"] for c in gate.get("checks") or [] if not c.get("ok")],
        "metrics": {
            "strict_json_rate": entry.get("strict_json_rate"),
            "schema_rate": entry.get("schema_rate"),
            "exact_labels_rate": entry.get("exact_labels_rate"),
            "slices": entry.get("slices"),
        },
    }
    baseline = gate_config.get("baseline")
    if baseline:
        baseline_path = repo / baseline
        if baseline_path.is_file():
            record["baseline"] = baseline
            record["baseline_sha256"] = sha256_file(baseline_path)
    if gate_config.get("in_cluster_model"):
        record["in_cluster_model"] = gate_config["in_cluster_model"]
    return record


def _benchmark_record(repo: Path, path: str) -> dict:
    report = _read_json(repo / path)
    measured = [
        run
        for run in report.get("runs", [])
        if str(run.get("label", "")).startswith("gpu-direct")
    ]
    best = max(measured, key=lambda run: run.get("output_tokens_per_s") or 0, default=None)
    return {
        "path": path,
        "sha256": sha256_file(repo / path),
        "gpu_output_tps_max": (best or {}).get("output_tokens_per_s"),
        "gpu_concurrency_at_max": (best or {}).get("concurrency"),
        "runs": len(report.get("runs", [])),
    }


def _cost_record(repo: Path, path: str) -> dict:
    report = _read_json(repo / path)
    owned = next((t for t in report.get("tiers", []) if t.get("tier") == "gpu-owned"), {})
    break_even = {
        item["tier"]: item["requests_per_day"]
        for item in report.get("break_even_vs_owned", [])
        if "tier" in item and "requests_per_day" in item
    }
    return {
        "path": path,
        "sha256": sha256_file(repo / path),
        "requests_per_day": (report.get("workload") or {}).get("requests_per_day"),
        "owned_usd_per_1m_at_workload": owned.get("usd_per_1m_at_workload"),
        "break_even_requests_per_day": break_even,
    }


def _policy_record(repo: Path, path: str) -> dict:
    policy_path = repo / path
    if not policy_path.is_file():
        raise ManifestError(f"missing policy file {path}")
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    version = policy.get("version")
    if not version:
        raise ManifestError(f"{path} has no version")
    return {"path": path, "version": str(version), "sha256": sha256_file(policy_path)}


def _adapter_records(repo: Path, config: dict, adapters_dir: Path) -> list[dict]:
    records = []
    for adapter in config["adapters"]:
        name = adapter["name"]
        root = Path(adapters_dir) / name
        tree = hash_tree(root)
        records.append(
            {
                "name": name,
                "served_name": adapter.get("served_name", name),
                "path": f"{Path(adapters_dir).relative_to(repo).as_posix()}/{name}"
                if Path(adapters_dir).is_relative_to(repo)
                else str(root),
                "sha256": tree["sha256"],
                "files": tree["files"],
            }
        )
    return records


def build_manifest(
    repo: Path,
    config: dict,
    *,
    adapters_dir: Path | None = None,
    created_at: str | None = None,
) -> dict:
    repo = Path(repo).resolve()
    adapters_dir = Path(adapters_dir) if adapters_dir else repo / "adapters"
    if not adapters_dir.is_absolute():
        adapters_dir = repo / adapters_dir

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "release_id": config["release_id"],
        "created_at": created_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": git_state(repo),
        "model": dict(config["model"]),
        "images": [
            {"name": image["name"], "tag": str(image["tag"])} for image in config["images"]
        ],
        "adapters": _adapter_records(repo, config, adapters_dir),
        "policy": _policy_record(repo, config.get("policy", "config/policy.yaml")),
        "gate": _gate_record(repo, config["gate"]),
        "benchmark": _benchmark_record(repo, config["evidence"]["benchmark"]),
        "cost": _cost_record(repo, config["evidence"]["cost"]),
        "notes": list(NOTES),
    }
    problems = validate_manifest(manifest)
    if problems:
        raise ManifestError("; ".join(problems))
    return manifest


def validate_manifest(manifest: dict) -> list[str]:
    if not isinstance(manifest, dict):
        return ["manifest is not an object"]
    problems = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version is {manifest.get('schema_version')!r}")
    if not RELEASE_ID.match(str(manifest.get("release_id", ""))):
        problems.append("release_id is missing or not lowercase")

    model = manifest.get("model") or {}
    for key in ("id", "revision", "served_name", "quantization"):
        if not model.get(key):
            problems.append(f"model.{key} is missing")
    if model.get("revision") and not REVISION.match(str(model["revision"])):
        problems.append("model.revision is not a hex revision")

    images = manifest.get("images") or []
    if not images:
        problems.append("no images declared")
    for image in images:
        if not image.get("name"):
            problems.append("image without a name")
        if not image.get("tag") or image.get("tag") == "latest":
            problems.append(f"image {image.get('name')} is not pinned")

    adapters = manifest.get("adapters") or []
    if not adapters:
        problems.append("no adapters declared")
    for adapter in adapters:
        if not HEX_SHA256.match(str(adapter.get("sha256", ""))):
            problems.append(f"adapter {adapter.get('name')} has no tree digest")

    policy = manifest.get("policy") or {}
    if not policy.get("version"):
        problems.append("policy.version is missing")
    if not HEX_SHA256.match(str(policy.get("sha256", ""))):
        problems.append("policy has no digest")

    gate = manifest.get("gate") or {}
    if gate.get("passed") is not True:
        problems.append("release gate did not pass")
    if gate.get("failed_checks"):
        problems.append(f"gate has failed checks: {', '.join(gate['failed_checks'])}")
    if not HEX_SHA256.match(str(gate.get("report_sha256", ""))):
        problems.append("gate report has no digest")
    return problems


def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path} is not a JSON object")
    return data


def summarize(manifest: dict) -> list[str]:
    gate = manifest.get("gate") or {}
    metrics = gate.get("metrics") or {}
    model = manifest.get("model") or {}
    adapters = ", ".join(a.get("name", "?") for a in manifest.get("adapters") or [])
    images = ", ".join(
        "{}:{}".format(i.get("name"), i.get("tag")) for i in manifest.get("images") or []
    )
    lines = [
        f"release   {manifest.get('release_id')} (schema {manifest.get('schema_version')})",
        f"model     {model.get('id')} @ {model.get('revision')} ({model.get('quantization')})",
        f"adapters  {adapters}",
        f"images    {images}",
        f"policy    {manifest.get('policy', {}).get('version')} {str(manifest.get('policy', {}).get('sha256'))[:12]}",
        f"gate      {'PASS' if gate.get('passed') else 'FAIL'} "
        f"exact={metrics.get('exact_labels_rate')} schema={metrics.get('schema_rate')} "
        f"({gate.get('checks_total')} checks, {gate.get('model')})",
    ]
    source = manifest.get("source") or {}
    lines.append(f"source    {source.get('commit')} dirty={source.get('dirty')}")
    return lines
