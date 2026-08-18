#!/usr/bin/env python3
"""Verify a generated release manifest and its digest-pinned Compose asset."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

try:
    from .render_truenas_release import DIGEST_RE, VERSION_RE, load_yaml, validate_compose
except ImportError:  # pragma: no cover - direct script execution
    from render_truenas_release import DIGEST_RE, VERSION_RE, load_yaml, validate_compose


def verify_release_artifacts(manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
        "gateway": "ghcr.io/mhilton7/power-monitor-v2-gateway",
        "backup": "ghcr.io/mhilton7/power-monitor-v2-backup",
    }
    images = manifest.get("images")
    if not isinstance(images, dict) or set(images) != set(expected_images):
        raise ValueError("release manifest must contain exactly the expected images")
    for name, expected_image in expected_images.items():
        image = images[name]
        if not isinstance(image, dict):
            raise ValueError(f"invalid {name} image record")
        digest = image.get("digest")
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise ValueError(f"invalid {name} digest")
        if image.get("name") != expected_image:
            raise ValueError(f"invalid {name} image repository")
    compose_name = manifest["compose"]["file"]
    if not isinstance(compose_name, str) or Path(compose_name).name != compose_name:
        raise ValueError("Compose manifest path must be a local basename")
    compose_path = manifest_path.parent / compose_name
    content = compose_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest["compose"]["sha256"]:
        raise ValueError("Compose checksum mismatch")
    compose = load_yaml(content.decode())
    validate_compose(compose, published=True)
    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError("release manifest version is invalid")
    database = manifest.get("database")
    if (
        not isinstance(database, dict)
        or not isinstance(database.get("expected_migration"), str)
        or not database["expected_migration"].replace("_", "").isdigit()
    ):
        raise ValueError("release manifest lacks the expected database migration")
    frontend = manifest.get("frontend")
    if (
        not isinstance(frontend, dict)
        or not isinstance(frontend.get("version"), str)
        or not VERSION_RE.fullmatch(frontend["version"])
        or not isinstance(frontend.get("revision"), str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", frontend["revision"])
        or not isinstance(frontend.get("static_asset_build_id"), str)
        or not frontend["static_asset_build_id"]
        or not isinstance(frontend.get("build_time"), str)
        or not frontend["build_time"]
    ):
        raise ValueError("release manifest lacks frontend build identity")
    firmware = manifest.get("firmware")
    if not isinstance(firmware, dict):
        raise ValueError("release manifest lacks firmware identity metadata")
    expected_firmware_repository = "https://github.com/mhilton7/power-monitor-sensor-headless"
    if firmware.get("repository") != expected_firmware_repository:
        raise ValueError("release manifest firmware repository is invalid")
    if not isinstance(firmware.get("build_id"), int) or firmware["build_id"] < 1:
        raise ValueError("release manifest firmware build identifier is invalid")
    if firmware.get("protocol") != manifest["protocol"]:
        raise ValueError("release manifest firmware protocol is incompatible")
    if not isinstance(firmware.get("revision"), str) or len(firmware["revision"]) < 40:
        raise ValueError("release manifest firmware revision is invalid")
    image_sha256 = firmware.get("image_sha256")
    if not isinstance(image_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", image_sha256):
        raise ValueError("release manifest firmware digest is invalid")
    firmware_release_url = manifest.get("firmware_release_url")
    if firmware_release_url != (
        f"{expected_firmware_repository}/releases/tag/{firmware.get('tag')}"
    ):
        raise ValueError("release manifest firmware URL and tag do not match")
    component_services = {
        "api": ("initialize", "migrate", "api", "worker"),
        "frontend": ("frontend",),
        "gateway": ("gateway",),
        "backup": ("backup",),
    }
    services = compose["services"]
    for component, service_names in component_services.items():
        image = images[component]
        expected_reference = f"{image['name']}:{version}@{image['digest']}"
        for service_name in service_names:
            if services[service_name].get("image") != expected_reference:
                raise ValueError(
                    f"Compose service {service_name} does not match manifest {component} image"
                )
    runtime_environment = services["api"].get("environment")
    if not isinstance(runtime_environment, dict):
        raise ValueError("API runtime identity environment is missing")
    expected_runtime_identity = {
        "PM_RELEASE_VERSION": version,
        "PM_RELEASE_REVISION": manifest.get("revision"),
        "PM_API_IMAGE_DIGEST": images["api"]["digest"],
        "PM_FRONTEND_VERSION": frontend["version"],
        "PM_FRONTEND_REVISION": frontend["revision"],
        "PM_FRONTEND_BUILD_TIME": frontend["build_time"],
        "PM_FRONTEND_IMAGE_DIGEST": images["frontend"]["digest"],
        "PM_FRONTEND_STATIC_ASSET_ID": frontend["static_asset_build_id"],
        "PM_EXPECTED_DATABASE_REVISION": database["expected_migration"],
    }
    for key, expected in expected_runtime_identity.items():
        if runtime_environment.get(key) != expected:
            raise ValueError(f"API runtime identity {key} does not match the release manifest")
    if release_status == "stable_physical_certification_passed":
        certification = manifest.get("hardware_certification")
        if not isinstance(certification, dict) or certification.get("status") != "passed":
            raise ValueError("stable release lacks passed hardware certification metadata")
        evidence_name = certification.get("file")
        if not isinstance(evidence_name, str) or Path(evidence_name).name != evidence_name:
            raise ValueError("hardware certification path must be a local basename")
        evidence_path = manifest_path.parent / evidence_name
        evidence = evidence_path.read_bytes()
        if hashlib.sha256(evidence).hexdigest() != certification.get("sha256"):
            raise ValueError("hardware certification checksum mismatch")
        parsed_evidence = json.loads(evidence)
        if parsed_evidence.get("schema") != "pm-hardware-certification/1.0.0":
            raise ValueError("hardware certification schema mismatch")
        if parsed_evidence.get("result") != "pass":
            raise ValueError("hardware certification did not pass")
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    verify_release_artifacts(args.manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
