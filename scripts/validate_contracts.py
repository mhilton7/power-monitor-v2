#!/usr/bin/env python3
"""Validate coordinated pm-protocol identity and deterministic JSON vectors."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROTOCOL = "pm-protocol/1.0.0"


def json_files(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for relative in ("shared/auth-test-vectors", "shared/contract-test-vectors", "shared/schemas"):
        directory = root / relative
        if directory.is_dir():
            candidates.extend(directory.glob("*.json"))
    return sorted(candidates)


def protocol_occurs(root: Path) -> bool:
    for relative in (
        "shared/protocol-version.txt",
        "test/vectors/server-device-api.yaml",
        "README.md",
    ):
        path = root / relative
        if path.is_file() and PROTOCOL in path.read_text(encoding="utf-8", errors="ignore"):
            return True
    return any(PROTOCOL in path.read_text(encoding="utf-8") for path in json_files(root))


def protocol_source_occurs(root: Path) -> bool:
    for relative in ("backend/app/constants.py", "backend/app/security/protocol.py"):
        path = root / relative
        if path.is_file() and PROTOCOL in path.read_text(encoding="utf-8", errors="ignore"):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--firmware-root", type=Path)
    args = parser.parse_args()
    roots = [("server", args.root.resolve())]
    if args.firmware_root:
        roots.append(("firmware", args.firmware_root.resolve()))
    count = 0
    for label, root in roots:
        if not (protocol_occurs(root) or (label == "server" and protocol_source_occurs(root))):
            raise ValueError(f"{label} does not declare {PROTOCOL}")
        for path in json_files(root):
            json.loads(path.read_text(encoding="utf-8"))
            count += 1
    if count == 0 and (args.root / "shared").exists():
        raise ValueError("shared directory exists but contains no JSON schemas/vectors")
    print(f"validated {count} JSON schema/vector files and {PROTOCOL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
