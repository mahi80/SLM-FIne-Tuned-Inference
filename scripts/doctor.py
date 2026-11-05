#!/usr/bin/env python3
"""Preflight checks before the first `docker compose up`.

Only reads the environment: it never installs anything and never prints tenant
credentials. `--fix` creates .env and config/tenants.yaml from the committed
examples, which is the only change it will make.

    python scripts/doctor.py [--fix]
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_ENV = ROOT / ".env.example"
EXAMPLE_TENANTS = ROOT / "config/tenants.example.yaml"
MIN_PYTHON = (3, 10)
MIN_VRAM_MIB = 10 * 1024
MIN_DISK_GIB = 20
PORT_KEYS = (
    ("GPU tier", "VLLM_PORT", 8010),
    ("gateway", "GATEWAY_PORT", 8020),
    ("CPU simulator", "TIERSIM_PORT", 8011),
    ("cloud simulator", "TIERSIM_CLOUD_PORT", 8012),
    ("Prometheus", "PROMETHEUS_PORT", 9090),
    ("Grafana", "GRAFANA_PORT", 3001),
)


def run(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def gpu_memory_mib() -> tuple[str | None, int | None]:
    if not shutil.which("nvidia-smi"):
        return None, None
    try:
        proc = run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, None
    first = proc.stdout.strip().splitlines()[0]
    try:
        name, memory = first.rsplit(",", 1)
        return name.strip(), int(memory.strip())
    except ValueError:
        return None, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check this machine before `make up`.")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="create .env and config/tenants.yaml from the committed examples",
    )
    args = parser.parse_args(argv)

    results: list[tuple[str, str, str]] = []

    def note(status: str, message: str, hint: str = "") -> None:
        results.append((status, message, hint))

    # Python
    version = platform.python_version()
    if sys.version_info >= MIN_PYTHON:
        note("OK", f"Python {version}")
    else:
        note(
            "FAIL",
            f"Python {version} is too old (need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+)",
            "Install Python 3.12 from python.org and reopen the terminal.",
        )

    # Docker and Compose
    docker = shutil.which("docker")
    daemon_up = False
    if docker is None:
        note(
            "FAIL",
            "Docker is not installed or not on PATH",
            "Install Docker Desktop (Windows/macOS) or Docker Engine (Linux), then reopen the terminal.",
        )
    else:
        try:
            info = run([docker, "info", "--format", "{{.ServerVersion}}"])
            if info.returncode == 0 and info.stdout.strip():
                daemon_up = True
                note("OK", f"Docker {info.stdout.strip()} (daemon reachable)")
            else:
                note(
                    "FAIL",
                    "Docker is installed but the daemon is not running",
                    "Start Docker Desktop and wait for 'Engine running', then rerun this check.",
                )
        except (OSError, subprocess.TimeoutExpired):
            note("FAIL", "Could not query the Docker daemon", "Start Docker Desktop and rerun.")

    compose_ok = False
    if docker is not None:
        try:
            compose = run([docker, "compose", "version", "--short"])
            if compose.returncode == 0 and compose.stdout.strip():
                compose_ok = True
                note("OK", f"Docker Compose {compose.stdout.strip()}")
            else:
                note(
                    "FAIL",
                    "Docker Compose v2 is not available",
                    "Update Docker Desktop, or install the compose plugin for Docker Engine.",
                )
        except (OSError, subprocess.TimeoutExpired):
            note("FAIL", "Could not run 'docker compose'", "Update Docker Desktop and rerun.")

    # GPU. The full stack needs one; the tests and static validators do not.
    gpu_name, vram = gpu_memory_mib()
    if gpu_name and vram:
        gib = vram / 1024
        if vram >= MIN_VRAM_MIB:
            note("OK", f"NVIDIA GPU {gpu_name} ({gib:.1f} GiB VRAM)")
        else:
            note(
                "WARN",
                f"NVIDIA GPU {gpu_name} has {gib:.1f} GiB VRAM; the 4B AWQ tier needs about 10 GiB",
                "Try a smaller model or a bigger GPU; the simulations and tests still run.",
            )
    else:
        note(
            "FAIL",
            "No NVIDIA GPU visible to the host",
            "Install the NVIDIA driver (Windows) or nvidia-container-toolkit (Linux). "
            "Without a GPU you can still run: make test && make deploy-validate",
        )

    if daemon_up and docker:
        try:
            runtimes = run([docker, "info", "--format", "{{json .Runtimes}}"])
            data = json.loads(runtimes.stdout) if runtimes.returncode == 0 else {}
            if "nvidia" in data:
                note("OK", "Docker exposes the nvidia runtime")
            elif gpu_name:
                note(
                    "WARN",
                    "Docker does not list an nvidia runtime",
                    "Run `make gpu-check`. If it fails, enable GPU support in Docker Desktop "
                    "or install nvidia-container-toolkit, then restart Docker.",
                )
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

    # Files. Created only when --fix is given.
    for path, example, label in (
        (ROOT / ".env", EXAMPLE_ENV, ".env"),
        (ROOT / "config/tenants.yaml", EXAMPLE_TENANTS, "config/tenants.yaml"),
    ):
        if path.is_file():
            note("OK", f"{label} exists")
        elif args.fix and example.is_file():
            shutil.copyfile(example, path)
            note("OK", f"{label} created from {example.name}")
        else:
            note(
                "FAIL",
                f"{label} is missing",
                "Run `make env`, or `python scripts/doctor.py --fix`.",
            )

    tenants = ROOT / "config/tenants.yaml"
    if tenants.is_file():
        git = shutil.which("git")
        if git and (ROOT / ".git").exists():
            ignored = run([git, "check-ignore", "-q", "config/tenants.yaml"])
            if ignored.returncode == 0:
                note("OK", "config/tenants.yaml is gitignored")
            else:
                note(
                    "FAIL",
                    "config/tenants.yaml is not gitignored",
                    "Do not commit tenant keys. Restore the .gitignore entry before continuing.",
                )

    # Ports
    env = parse_env(ROOT / ".env")
    for label, key, default in PORT_KEYS:
        try:
            port = int(env.get(key, default))
        except ValueError:
            note("FAIL", f"{key}={env.get(key)!r} is not a port number", "Use a number in .env.")
            continue
        if port_is_free(port):
            note("OK", f"{label} port {port} is free")
        else:
            note(
                "WARN",
                f"{label} port {port} is already in use",
                f"Stop the other process, or change {key} in .env before `make up`.",
            )

    # Adapters are optional: the base model serves without them.
    adapters = list((ROOT / "adapters").glob("*/adapter_model.safetensors"))
    if adapters:
        note("OK", f"{len(adapters)} adapter(s) found under adapters/")
    else:
        note(
            "INFO",
            "no adapters under adapters/ (the base model still serves)",
            "`make adapter-train` builds triage-v1/v2; it takes the GPU for about 25 minutes.",
        )

    # Disk: weights and images need roughly 15-20 GiB.
    free_gib = shutil.disk_usage(ROOT).free / 1024**3
    if free_gib >= MIN_DISK_GIB:
        note("OK", f"{free_gib:.0f} GiB free disk")
    else:
        note(
            "WARN",
            f"only {free_gib:.0f} GiB free disk; the model and images need about 15-20 GiB",
            "Free space before `make up`, or Docker may fail mid-download.",
        )

    # Compose syntax, using the same file the quickstart runs.
    if daemon_up and compose_ok and docker:
        try:
            check = run([docker, "compose", "config", "--quiet"], timeout=30)
            if check.returncode == 0:
                note("OK", "compose.yaml is valid")
            else:
                detail = (check.stderr or check.stdout).strip().splitlines()
                note(
                    "FAIL",
                    "compose.yaml does not validate",
                    detail[0] if detail else "Run `docker compose config` for the full error.",
                )
        except (OSError, subprocess.TimeoutExpired):
            note("WARN", "Could not validate compose.yaml", "Run `docker compose config` manually.")

    # Optional cluster tooling
    if shutil.which("kubectl"):
        note("INFO", "kubectl found (optional; only the cluster path needs it)")
    else:
        note("INFO", "kubectl not found (optional; only the cluster path needs it)")

    failures = [r for r in results if r[0] == "FAIL"]
    warnings = [r for r in results if r[0] == "WARN"]

    print("\nSLM Fabric preflight\n")
    for status, message, hint in results:
        print(f"  {status:<4} {message}")
        if hint and status in {"FAIL", "WARN"}:
            print(f"       -> {hint}")
    print()
    if failures:
        print(f"{len(failures)} blocking problem(s), {len(warnings)} warning(s).")
        print("Fix the FAIL lines above, then run this check again.")
        return 1
    have_make = shutil.which("make") is not None
    start = "make up" if have_make else "docker compose up -d --wait"
    stop = "make down" if have_make else "docker compose down"
    if warnings:
        print(f"Ready with {len(warnings)} warning(s): run `{start}`.")
    else:
        print(f"Ready: run `{start}`.")
    print("First start builds images and downloads ~10 GB of weights; allow 10-20 minutes.")
    print(f"Stop later with `{stop}` (the model cache is kept).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
