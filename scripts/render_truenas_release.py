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
    "gateway": ("0.0.0-unpublished", "UNPUBLISHED_GATEWAY_DIGEST"),
    "backup": ("0.0.0-unpublished", "UNPUBLISHED_BACKUP_DIGEST"),
}
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REFERENCE_RE = re.compile(r"^[^\s@]+:[^\s@]+@sha256:[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$")
EXPECTED_SERVICES = {
    "initialize",
    "postgres",
    "migrate",
    "api",
    "worker",
    "frontend",
    "gateway",
    "backup",
}
EXPECTED_DATABASE_ROLES = {
    "migrate": ("pm_migrator", "postgres_migrator_password"),
    "api": ("pm_api", "postgres_api_password"),
    "worker": ("pm_worker", "postgres_worker_password"),
    "backup": ("pm_backup", "postgres_backup_password"),
}
EXPECTED_INITIALIZER_MOUNTS = {
    f"/mnt/Apps/PowerMeterV2/{name}": f"/host/{name}"
    for name in (
        "postgres",
        "config",
        "firmware",
        "backups",
        "logs",
        "rate-source-artifacts",
        "caddy-data",
        "caddy-config",
        "secrets",
    )
}
EXPECTED_HOST_SOURCES = set(EXPECTED_INITIALIZER_MOUNTS)
DEFAULT_DATABASE_REVISION = "20260821_0019"
DEFAULT_BUILD_TIME = "1970-01-01T00:00:00Z"


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
            if not isinstance(image, str) or not IMAGE_REFERENCE_RE.fullmatch(image):
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
        if any(
            not isinstance(volume, dict)
            or volume.get("type") != "bind"
            or volume.get("bind", {}).get("create_host_path") is not False
            for volume in service.get("volumes", [])
        ):
            raise ReleaseError(
                f"service {name} volumes must be long-form bind mounts with "
                "create_host_path disabled"
            )
        if any(
            isinstance(volume, dict) and volume.get("source") not in EXPECTED_HOST_SOURCES
            for volume in service.get("volumes", [])
        ):
            raise ReleaseError(f"service {name} may bind only an exact UI-created dataset root")
        if name in service.get("depends_on", {}):
            raise ReleaseError(f"service {name} must not depend on itself")
    initializer = services["initialize"]
    if initializer.get("image") != services["api"].get("image"):
        raise ReleaseError("initialize must reuse the exact API image")
    if initializer.get("user") != "0:0" or initializer.get("network_mode") != "none":
        raise ReleaseError("initialize must be isolated root with no network")
    if initializer.get("command") != [
        "python",
        "/opt/powermeter/host-initializer/initialize_host.py",
    ]:
        raise ReleaseError("initialize must use the embedded host initializer")
    if initializer.get("cap_drop") != ["ALL"] or set(initializer.get("cap_add", [])) != {
        "CHOWN",
        "FOWNER",
        "DAC_OVERRIDE",
    }:
        raise ReleaseError("initialize must have only the exact file-metadata capabilities")
    initializer_mounts = {
        (volume.get("source"), volume.get("target"), volume.get("read_only", False))
        for volume in initializer.get("volumes", [])
        if isinstance(volume, dict)
    }
    if (
        len(initializer.get("volumes", [])) != len(EXPECTED_INITIALIZER_MOUNTS)
        or (
            initializer_mounts
            != {(source, target, False) for source, target in EXPECTED_INITIALIZER_MOUNTS.items()}
        )
        or any(
            not isinstance(volume, dict)
            or volume.get("type") != "bind"
            or volume.get("bind", {}).get("create_host_path") is not False
            for volume in initializer.get("volumes", [])
        )
    ):
        raise ReleaseError("initialize must have only the exact writable host dataset mounts")
    for name, service in services.items():
        if name != "initialize" and service.get("depends_on", {}).get("initialize") != {
            "condition": "service_completed_successfully"
        }:
            raise ReleaseError(f"service {name} is not gated by successful host initialization")
        if name != "initialize" and any(
            isinstance(volume, dict)
            and (
                str(volume.get("source", "")).rstrip("/") == "/mnt/Apps/PowerMeterV2/secrets"
                or str(volume.get("source", "")).startswith("/mnt/Apps/PowerMeterV2/secrets/")
            )
            for volume in service.get("volumes", [])
        ):
            raise ReleaseError(f"service {name} must not mount the secrets dataset")
    expected_config_mounts = {
        "postgres": "/docker-entrypoint-initdb.d",
        "api": "/data/config",
        "worker": "/data/config",
        "gateway": "/etc/caddy",
    }
    for name, target in expected_config_mounts.items():
        matching = [
            volume
            for volume in services[name].get("volumes", [])
            if isinstance(volume, dict) and volume.get("source") == "/mnt/Apps/PowerMeterV2/config"
        ]
        if (
            len(matching) != 1
            or matching[0].get("target") != target
            or matching[0].get("read_only") is not True
        ):
            raise ReleaseError(f"service {name} must mount the config dataset read-only")
    gateway_ports = services["gateway"].get("ports", [])
    if (
        not isinstance(gateway_ports, list)
        or len(gateway_ports) != 1
        or not isinstance(gateway_ports[0], dict)
        or gateway_ports[0].get("target") != 8443
        or str(gateway_ports[0].get("published")) != "8443"
        or gateway_ports[0].get("protocol") != "tcp"
        or gateway_ports[0].get("app_protocol") != "https"
    ):
        raise ReleaseError("gateway must publish only TCP 8443 as HTTPS")
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
    template: str,
    version: str,
    api_digest: str,
    frontend_digest: str,
    gateway_digest: str,
    backup_digest: str,
    *,
    revision: str = "0" * 40,
    build_time: str = DEFAULT_BUILD_TIME,
    frontend_version: str | None = None,
    frontend_asset_id: str | None = None,
    database_revision: str = DEFAULT_DATABASE_REVISION,
) -> str:
    prefix = "ghcr.io/mhilton7/power-monitor-v2"
    unpublished = "0.0.0-unpublished"
    api_sentinel = f"{prefix}-api:{unpublished}@sha256:" + "UNPUBLISHED_API_DIGEST"
    frontend_sentinel = f"{prefix}-frontend:{unpublished}@sha256:"
    frontend_sentinel += "UNPUBLISHED_FRONTEND_DIGEST"
    gateway_sentinel = f"{prefix}-gateway:{unpublished}@sha256:" + "UNPUBLISHED_GATEWAY_DIGEST"
    backup_sentinel = f"{prefix}-backup:{unpublished}@sha256:" + "UNPUBLISHED_BACKUP_DIGEST"
    replacements = {
        api_sentinel: f"{prefix}-api:{version}@{api_digest}",
        frontend_sentinel: f"{prefix}-frontend:{version}@{frontend_digest}",
        gateway_sentinel: f"{prefix}-gateway:{version}@{gateway_digest}",
        backup_sentinel: f"{prefix}-backup:{version}@{backup_digest}",
        "UNPUBLISHED_RELEASE_VERSION": version,
        "UNPUBLISHED_FRONTEND_VERSION": frontend_version or version,
        "UNPUBLISHED_RELEASE_REVISION": revision,
        "UNPUBLISHED_FRONTEND_REVISION": revision,
        "UNPUBLISHED_FRONTEND_BUILD_TIME": build_time,
        "sha256:UNPUBLISHED_RUNTIME_API_DIGEST": api_digest,
        "sha256:UNPUBLISHED_RUNTIME_FRONTEND_DIGEST": frontend_digest,
        "UNPUBLISHED_FRONTEND_ASSET_ID": frontend_asset_id or f"{version}-{revision[:16]}",
        "UNPUBLISHED_DATABASE_REVISION": database_revision,
    }
    text = template
    for old, new in replacements.items():
        count = text.count(old)
        expected = 4 if "-api:" in old else 1
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
    parser.add_argument("--gateway-digest", required=True)
    parser.add_argument("--backup-digest", required=True)
    parser.add_argument("--build-time", required=True)
    parser.add_argument("--frontend-version")
    parser.add_argument("--frontend-asset-id", required=True)
    parser.add_argument("--database-revision", default=DEFAULT_DATABASE_REVISION)
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
        default=(
            "PowerMeter V2 firmware using pm-protocol/1.0.0 and "
            "pm-telemetry/2.0.0; see linked firmware release"
        ),
    )
    parser.add_argument("--firmware-release-url", required=True)
    parser.add_argument("--firmware-tag", required=True)
    parser.add_argument("--firmware-revision", required=True)
    parser.add_argument("--firmware-image-sha256", required=True)
    parser.add_argument("--firmware-build-number", required=True)
    parser.add_argument("--firmware-build-id", required=True)
    parser.add_argument("--hardware-certification-sha256")
    args = parser.parse_args()

    version = args.version.removeprefix("v")
    if not VERSION_RE.fullmatch(version):
        raise ReleaseError("version must be semantic version syntax (without build metadata)")
    if not re.fullmatch(r"[0-9a-f]{40,64}", args.revision.lower()):
        raise ReleaseError("revision must be a full Git object ID")
    frontend_version = args.frontend_version or version
    if not VERSION_RE.fullmatch(frontend_version):
        raise ReleaseError("frontend version must use semantic version syntax")
    try:
        parsed_build_time = datetime.fromisoformat(args.build_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseError("build time must be an ISO-8601 timestamp") from exc
    if parsed_build_time.tzinfo is None:
        raise ReleaseError("build time must include a UTC offset")
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}", args.frontend_asset_id):
        raise ReleaseError("frontend asset ID must be a bounded opaque identifier")
    if not re.fullmatch(r"[0-9]{8}_[0-9]{4}", args.database_revision):
        raise ReleaseError("database revision must use the Alembic revision format")
    api_digest = parse_digest(args.api_digest, "api digest")
    frontend_digest = parse_digest(args.frontend_digest, "frontend digest")
    gateway_digest = parse_digest(args.gateway_digest, "gateway digest")
    backup_digest = parse_digest(args.backup_digest, "backup digest")
    if not re.fullmatch(
        r"https://github\.com/mhilton7/power-monitor-sensor-headless/releases/tag/v[^/]+",
        args.firmware_release_url,
    ):
        raise ReleaseError("release requires the public compatible firmware release URL")
    firmware_tag_pattern = (
        r"v[0-9]+\.[0-9]+\.[0-9]+"
        if args.release_status == "stable_physical_certification_passed"
        else r"v[0-9]+\.[0-9]+\.[0-9]+-rc\.[1-9][0-9]*"
    )
    if not re.fullmatch(firmware_tag_pattern, args.firmware_tag):
        raise ReleaseError("firmware tag does not match the release status")
    if not re.fullmatch(r"[0-9a-f]{40,64}", args.firmware_revision):
        raise ReleaseError("release requires a full firmware revision")
    if not re.fullmatch(r"[0-9a-f]{64}", args.firmware_image_sha256):
        raise ReleaseError("release requires the firmware image SHA-256")
    if (
        not re.fullmatch(r"[1-9][0-9]{0,9}", args.firmware_build_number)
        or int(args.firmware_build_number) > 4_294_967_295
    ):
        raise ReleaseError("release requires a positive uint32 firmware build number")
    if not re.fullmatch(r"[0-9a-f]{64}", args.firmware_build_id):
        raise ReleaseError("release requires the exact lowercase firmware build ID")
    if args.release_status == "stable_physical_certification_passed":
        if not args.hardware_certification_sha256 or not re.fullmatch(
            r"[0-9a-f]{64}", args.hardware_certification_sha256
        ):
            raise ReleaseError("stable releases require a hardware-certification SHA-256")
    elif args.hardware_certification_sha256:
        raise ReleaseError("hardware certification inputs are accepted only for stable releases")

    template = args.template.read_text(encoding="utf-8")
    validate_compose(load_yaml(template), published=False)
    output = render(
        template,
        version,
        api_digest,
        frontend_digest,
        gateway_digest,
        backup_digest,
        revision=args.revision.lower(),
        build_time=args.build_time,
        frontend_version=frontend_version,
        frontend_asset_id=args.frontend_asset_id,
        database_revision=args.database_revision,
    )
    validate_compose(load_yaml(output), published=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8", newline="\n")
    output_sha256 = hashlib.sha256(output.encode()).hexdigest()
    manifest = {
        "schema": "pm-server-release/1.1.0",
        "product": "PowerMeter V2",
        "protocol": "pm-protocol/1.0.0",
        "telemetry_protocol": "pm-telemetry/2.0.0",
        "version": version,
        "revision": args.revision.lower(),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "images": {
            "api": {"name": "ghcr.io/mhilton7/power-monitor-v2-api", "digest": api_digest},
            "frontend": {
                "name": "ghcr.io/mhilton7/power-monitor-v2-frontend",
                "digest": frontend_digest,
            },
            "gateway": {
                "name": "ghcr.io/mhilton7/power-monitor-v2-gateway",
                "digest": gateway_digest,
            },
            "backup": {"name": "ghcr.io/mhilton7/power-monitor-v2-backup", "digest": backup_digest},
        },
        "compose": {"file": args.output.name, "sha256": output_sha256},
        "release_status": args.release_status,
        "compatible_firmware": args.compatible_firmware,
        "database": {"expected_migration": args.database_revision},
        "frontend": {
            "version": frontend_version,
            "revision": args.revision.lower(),
            "static_asset_build_id": args.frontend_asset_id,
            "build_time": args.build_time,
        },
        "firmware_release_url": args.firmware_release_url,
        "firmware": {
            "repository": "https://github.com/mhilton7/power-monitor-sensor-headless",
            "tag": args.firmware_tag,
            "revision": args.firmware_revision,
            "build_number": int(args.firmware_build_number),
            "firmware_build_id": args.firmware_build_id,
            "image_sha256": args.firmware_image_sha256,
            "protocol": "pm-protocol/1.0.0",
            "telemetry_protocol": "pm-telemetry/2.0.0",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
        },
        "hardware_certification": {
            "status": "passed"
            if args.release_status == "stable_physical_certification_passed"
            else "pending",
            "physical": args.release_status == "stable_physical_certification_passed",
        },
    }
    if args.release_status == "stable_physical_certification_passed":
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
