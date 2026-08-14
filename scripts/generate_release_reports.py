#!/usr/bin/env python3
"""Index checksum-backed release-gate evidence into a machine-readable report."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path


def evidence(path: Path) -> dict[str, object]:
    content = path.read_bytes()
    return {"file": path.name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--status", choices=("passed", "failed"), required=True)
    parser.add_argument("--kind", choices=("test", "security", "migration"), required=True)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.inputs:
        if not path.is_file():
            raise FileNotFoundError(path)
    report = {
        "schema": "pm-release-gate/1.0.0",
        "kind": args.kind,
        "version": args.version.removeprefix("v"),
        "revision": args.revision,
        "status": args.status,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "evidence": [evidence(path) for path in args.inputs],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
