#!/usr/bin/env python3
"""Render and verify the immutable digest-pinned TrueNAS release asset."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

SENTINELS = {
    "api": ("0.0.0-unpublished", "UNPUBLISHED_API_DIGEST"),
    "frontend": ("0.0.0-unpublished", "UNPUBLISHED_FRONTEND_DIGEST"),
    "backup": ("0.0.0-unpublished", "UNPUBLISHED_BACKUP_DIGEST"),
}
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$")
EXPECTED_SERVICES = {"postgres", "migrate", "api", "worker", "frontend", "gateway", "backup"}
EXPECTED_DATABASE_ROLES = {
    "migrate": ("pm_migrator", "postgres_migrator_password"),
    "api": ("pm_api", "postgres_api_password"),
    "worker": ("pm_worker", "postgres_worker_password"),
    "backup": ("pm_backup", "postgres_backup_password"),
}


class ReleaseError(ValueError):
    """A release input violates an immutable release invariant."""


def parse_digest(value: str, name: str) -> str:
    value = value.strip().lower()
    if not DIGEST_RE.fullmatch(value):
        raise ReleaseError(f"{name} must be sha256 followed by exactly 64 lowercase hex digits")
    if value.endswith("0" * 64):
        raise ReleaseError(f"{name} must not be a zero/fake digest")
    return value


def load_yaml(text: str) -> dict[str, Any]:
    value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ReleaseError("Compose template must be a mapping")
    return value


def validate_compose(compose: dict[str, Any], *, published: bool) -> None:
    services = compose.get("services")
    if not isinstance(services, dict) or set(services) != EXPECTED_SERVICES:
        raise ReleaseError(f"services must be exactly {sorted(EXPECTED_SERVICES)}")
    if published:
        for name, service in services.items():
            image = service.get("image") if isinstance(service, dict) else None
            if not isinstance(image, str) or "@sha256:" not in image:
                raise ReleaseError(f"service {name} is not digest pinned")
            if "UNPUBLISHED" in image or "latest" in image:
                raise ReleaseError(f"service {name} contains an unpublished or floating image")
    for name, service in services.items():
        if not isinstance(service, dict):
            raise ReleaseError(f"service {name} must be a mapping")
        if service.get("privileged") is True:
            raise ReleaseError(f"service {name} must not be privileged")
        if any("docker.sock" in str(v) for v in service.get("volumes", [])):
            raise ReleaseError(f"service {name} must not mount the Docker socket")
        if name in service.get("depends_on", {}):
            raise ReleaseError(f"service {name} must not depend on itself")
    if not services["gateway"].get("ports", []):
        raise ReleaseError("gateway must publish HTTPS")
    for name, service in services.items():
        if name != "gateway" and service.get("ports"):
            raise ReleaseError(f"only gateway may publish a port, found {name}")
    database = compose.get("networks", {}).get("database", {})
    if database.get("internal") is not True:
        raise ReleaseError("database network must be internal")
    postgres_environment = services["postgres"].get("environment", {})
    if postgres_environment.get("POSTGRES_USER") != "pm_bootstrap":
        raise ReleaseError("PostgreSQL must use the isolated bootstrap role")
    for name, (role, secret_name) in EXPECTED_DATABASE_ROLES.items():
        environment = services[name].get("environment", {})
        if environment.get("PM_DATABASE_USER") != role:
            raise ReleaseError(f"service {name} must use database role {role}")
        if environment.get("PM_DATABASE_PASSWORD_FILE") != f"/run/secrets/{secret_name}":
            raise ReleaseError(f"service {name} database secret does not match its role")
        mounted_secrets = {
            item if isinstance(item, str) else item.get("source")
            for item in services[name].get("secrets", [])
        }
        if secret_name not in mounted_secrets or "postgres_bootstrap_password" in mounted_secrets:
            raise ReleaseError(f"service {name} violates database-secret isolation")
    if services["migrate"].get("command") != ["python", "-m", "backend.app.migrate"]:
        raise ReleaseError("migrate must use the role-aware migration entrypoint")


def render(
    template: str, version: str, api_digest: str, frontend_digest: str, backup_digest: str
) -> str:
    prefix = "ghcr.io/mhilton7/power-monitor-v2"
    unpublished = "0.0.0-unpublished"
    api_sentinel = f"{prefix}-api:{unpublished}@sha256:" + "UNPUBLISHED_API_DIGEST"
    frontend_sentinel = f"{prefix}-frontend:{unpublished}@sha256:"
    frontend_sentinel += "UNPUBLISHED_FRONTEND_DIGEST"
    backup_sentinel = f"{prefix}-backup:{unpublished}@sha256:" + "UNPUBLISHED_BACKUP_DIGEST"
    replacements = {
        api_sentinel: f"{prefix}-api:{version}@{api_digest}",
        frontend_sentinel: f"{prefix}-frontend:{version}@{frontend_digest}",
        backup_sentinel: f"{prefix}-backup:{version}@{backup_digest}",
    }
    text = template
    for old, new in replacements.items():
        count = text.count(old)
        expected = 3 if "-api:" in old else 1
        if count != expected:
            raise ReleaseError(f"expected {expected} occurrence(s) of {old}, found {count}")
        text = text.replace(old, new)
    if "UNPUBLISHED" in text or "0.0.0-unpublished" in text:
        raise ReleaseError("render left an unpublished sentinel")
    return text.replace(
        "# RELEASE TEMPLATE -- INTENTIONALLY NOT DEPLOYABLE.\n"
        "# Application image digests are fail-closed sentinels until the signed release\n"
        "# workflow replaces them. Never hand-edit a digest; use render_truenas_release.py.\n",
        f"# Generated release asset for PowerMeter V2 {version}. Do not hand-edit.\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--api-digest", required=True)
    parser.add_argument("--frontend-digest", required=True)
    parser.add_argument("--backup-digest", required=True)
    parser.add_argument(
        "--release-status",
        choices=(
            "candidate_physical_certification_pending",
            "stable_physical_certification_passed",
        ),
        default="candidate_physical_certification_pending",
    )
    parser.add_argument(
        "--compatible-firmware",
        default="PowerMeter V2 firmware using pm-protocol/1.0.0; see linked firmware release",
    )
    parser.add_argument("--firmware-release-url")
    parser.add_argument("--firmware-tag")
    parser.add_argument("--firmware-revision")
    parser.add_argument("--firmware-image-sha256")
    parser.add_argument("--hardware-certification-sha256")
    args = parser.parse_args()

    version = args.version.removeprefix("v")
    if not VERSION_RE.fullmatch(version):
        raise ReleaseError("version must be semantic version syntax (without build metadata)")
    if not re.fullmatch(r"[0-9a-f]{40,64}", args.revision.lower()):
        raise ReleaseError("revision must be a full Git object ID")
    api_digest = parse_digest(args.api_digest, "api digest")
    frontend_digest = parse_digest(args.frontend_digest, "frontend digest")
    backup_digest = parse_digest(args.backup_digest, "backup digest")
    if args.release_status == "stable_physical_certification_passed":
        if not args.firmware_release_url or not re.fullmatch(
            r"https://github\.com/mhilton7/power-monitor-sensor-headless/releases/tag/v[^/]+",
            args.firmware_release_url,
        ):
            raise ReleaseError("stable releases require the public compatible firmware release URL")
        if not args.firmware_tag or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", args.firmware_tag):
            raise ReleaseError("stable releases require a stable firmware tag")
        if not args.firmware_revision or not re.fullmatch(
            r"[0-9a-f]{40,64}", args.firmware_revision
        ):
            raise ReleaseError("stable releases require a full firmware revision")
        if not args.firmware_image_sha256 or not re.fullmatch(
            r"[0-9a-f]{64}", args.firmware_image_sha256
        ):
            raise ReleaseError("stable releases require the firmware image SHA-256")
        if not args.hardware_certification_sha256 or not re.fullmatch(
            r"[0-9a-f]{64}", args.hardware_certification_sha256
        ):
            raise ReleaseError("stable releases require a hardware-certification SHA-256")
    elif any(
        (
            args.firmware_release_url,
            args.firmware_tag,
            args.firmware_revision,
            args.firmware_image_sha256,
            args.hardware_certification_sha256,
        )
    ):
        raise ReleaseError("hardware certification inputs are accepted only for stable releases")

    template = args.template.read_text(encoding="utf-8")
    validate_compose(load_yaml(template), published=False)
    output = render(template, version, api_digest, frontend_digest, backup_digest)
    validate_compose(load_yaml(output), published=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8", newline="\n")
    output_sha256 = hashlib.sha256(output.encode()).hexdigest()
    manifest = {
        "schema": "pm-server-release/1.0.0",
        "product": "PowerMeter V2",
        "protocol": "pm-protocol/1.0.0",
        "version": version,
        "revision": args.revision.lower(),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "images": {
            "api": {"name": "ghcr.io/mhilton7/power-monitor-v2-api", "digest": api_digest},
            "frontend": {
                "name": "ghcr.io/mhilton7/power-monitor-v2-frontend",
                "digest": frontend_digest,
            },
            "backup": {"name": "ghcr.io/mhilton7/power-monitor-v2-backup", "digest": backup_digest},
        },
        "compose": {"file": args.output.name, "sha256": output_sha256},
        "release_status": args.release_status,
        "compatible_firmware": args.compatible_firmware,
    }
    if args.release_status == "stable_physical_certification_passed":
        manifest["firmware_release_url"] = args.firmware_release_url
        manifest["firmware"] = {
            "repository": "https://github.com/mhilton7/power-monitor-sensor-headless",
            "tag": args.firmware_tag,
            "revision": args.firmware_revision,
            "image_sha256": args.firmware_image_sha256,
            "protocol": "pm-protocol/1.0.0",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
        }
        manifest["hardware_certification"] = {
            "file": "hardware-certification.json",
            "sha256": args.hardware_certification_sha256,
            "status": "passed",
            "physical": True,
        }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(output_sha256)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReleaseError, OSError, yaml.YAMLError) as exc:
        print(f"release render failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
