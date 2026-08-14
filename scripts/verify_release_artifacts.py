#!/usr/bin/env python3
"""Verify a generated release manifest and its digest-pinned Compose asset."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from render_truenas_release import DIGEST_RE, load_yaml, validate_compose


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("schema") != "pm-server-release/1.0.0":
        raise ValueError("unsupported release manifest schema")
    if manifest.get("protocol") != "pm-protocol/1.0.0":
        raise ValueError("protocol mismatch")
    release_status = manifest.get("release_status")
    if release_status not in {
        "candidate_physical_certification_pending",
        "stable_physical_certification_passed",
    }:
        raise ValueError("unsupported release status")
    expected_images = {
        "api": "ghcr.io/mhilton7/power-monitor-v2-api",
        "frontend": "ghcr.io/mhilton7/power-monitor-v2-frontend",
        "backup": "ghcr.io/mhilton7/power-monitor-v2-backup",
    }
    for name, expected_image in expected_images.items():
        image = manifest["images"][name]
        digest = image["digest"]
        if not DIGEST_RE.fullmatch(digest):
            raise ValueError(f"invalid {name} digest")
        if image.get("name") != expected_image:
            raise ValueError(f"invalid {name} image repository")
    compose_name = manifest["compose"]["file"]
    if not isinstance(compose_name, str) or Path(compose_name).name != compose_name:
        raise ValueError("Compose manifest path must be a local basename")
    compose_path = args.manifest.parent / compose_name
    content = compose_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest["compose"]["sha256"]:
        raise ValueError("Compose checksum mismatch")
    validate_compose(load_yaml(content.decode()), published=True)
    if release_status == "stable_physical_certification_passed":
        certification = manifest.get("hardware_certification")
        if not isinstance(certification, dict) or certification.get("status") != "passed":
            raise ValueError("stable release lacks passed hardware certification metadata")
        evidence_name = certification.get("file")
        if not isinstance(evidence_name, str) or Path(evidence_name).name != evidence_name:
            raise ValueError("hardware certification path must be a local basename")
        evidence_path = args.manifest.parent / evidence_name
        evidence = evidence_path.read_bytes()
        if hashlib.sha256(evidence).hexdigest() != certification.get("sha256"):
            raise ValueError("hardware certification checksum mismatch")
        parsed_evidence = json.loads(evidence)
        if parsed_evidence.get("schema") != "pm-hardware-certification/1.0.0":
            raise ValueError("hardware certification schema mismatch")
        if parsed_evidence.get("result") != "pass":
            raise ValueError("hardware certification did not pass")
        firmware = manifest.get("firmware")
        if not isinstance(firmware, dict):
            raise ValueError("stable release lacks firmware identity metadata")
        evidence_firmware = parsed_evidence.get("firmware")
        if not isinstance(evidence_firmware, dict):
            raise ValueError("hardware certification lacks firmware identity")
        expected_firmware = {
            "repository": evidence_firmware.get("repository"),
            "revision": evidence_firmware.get("commit"),
            "image_sha256": evidence_firmware.get("image_sha256"),
            "protocol": evidence_firmware.get("protocol"),
            "board_profile": evidence_firmware.get("board_profile"),
        }
        for key, value in expected_firmware.items():
            if firmware.get(key) != value:
                raise ValueError(f"stable manifest firmware {key} mismatches certification")
        if firmware.get("tag", "").removeprefix("v") != evidence_firmware.get("version"):
            raise ValueError("stable manifest firmware tag mismatches certification")
    print("release artifacts verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
