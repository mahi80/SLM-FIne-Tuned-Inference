"""Load every adapter under adapters/ into a running vLLM server.

The serving container mounts ./adapters read-only and starts without adapters;
this script promotes them at runtime over the OpenAI-compatible control API,
which is also the path a release pipeline uses (Phase 4).

    python scripts/load_adapters.py [--dir adapters] [--url http://localhost:8010]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="adapters")
    ap.add_argument("--url", default="http://localhost:8010")
    args = ap.parse_args()

    root = Path(args.dir)
    adapters = sorted(p for p in root.iterdir() if (p / "adapter_config.json").is_file()) if root.is_dir() else []
    if not adapters:
        print(f"no adapters found under {root}/ - run `make adapter-train` first")
        return 1

    ok = True
    for path in adapters:
        name = path.name
        if name.startswith("_"):
            continue
        body = {"lora_name": name, "lora_path": f"/adapters/{name}"}
        r = httpx.post(f"{args.url}/v1/load_lora_adapter", json=body, timeout=120.0)
        text = r.text.strip()
        if r.status_code == 200:
            print(f"{name:14} loaded")
        elif r.status_code == 400 and "already" in text.lower():
            print(f"{name:14} already loaded")
        else:
            ok = False
            print(f"{name:14} FAILED {r.status_code} {text[:200]}")

    models = httpx.get(f"{args.url}/v1/models", timeout=30.0).json()["data"]
    print("served:", json.dumps(sorted(m["id"] for m in models)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
