#!/usr/bin/env python3
"""Static validation for Compose, workflow, release and secret boundaries."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

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
EXPECTED_USERS = {
    "initialize": "0:0",
    "postgres": "70:70",
    "migrate": "10001:10001",
    "api": "10001:10001",
    "worker": "10001:10001",
    "frontend": "101:101",
    "gateway": "1000:1000",
    "backup": "568:568",
}
EXPECTED_NETWORKS = {
    "initialize": set(),
    "postgres": {"database"},
    "migrate": {"database"},
    "api": {"database", "edge", "egress"},
    "worker": {"database", "egress"},
    "frontend": {"edge"},
    "gateway": {"edge"},
    "backup": {"database"},
}
EXPECTED_DATABASE_ROLES = {
    "migrate": ("pm_migrator", "postgres_migrator_password"),
    "api": ("pm_api", "postgres_api_password"),
    "worker": ("pm_worker", "postgres_worker_password"),
    "backup": ("pm_backup", "postgres_backup_password"),
}
APP_IMAGES = {
    "api": "ghcr.io/mhilton7/power-monitor-v2-api",
    "frontend": "ghcr.io/mhilton7/power-monitor-v2-frontend",
    "gateway": "ghcr.io/mhilton7/power-monitor-v2-gateway",
    "backup": "ghcr.io/mhilton7/power-monitor-v2-backup",
}
EXPECTED_SECRETS = {
    "postgres_bootstrap_password": "postgres_bootstrap_password",
    "postgres_migrator_password": "postgres_migrator_password",
    "postgres_api_password": "postgres_api_password",
    "postgres_worker_password": "postgres_worker_password",
    "postgres_backup_password": "postgres_backup_password",
    "postgres_restore_password": "postgres_restore_password",
    "session_secret": "session_secret",
    "field_encryption_key": "field_encryption_key",
    "ota_manifest_key": "ota_manifest_key",
    "backup_encryption_key": "backup_encryption_key",
    "tls_cert": "tls.crt",
    "tls_key": "tls.key",
    "tls_ca": "tls-ca.crt",
}
EXPECTED_HOST_SOURCES = {
    f"/mnt/Apps/PowerMeterV2/{name}"
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
DIGEST_IMAGE = re.compile(r"^[^\s@]+:[^\s@]+@sha256:[0-9a-f]{64}$")
SENTINEL_IMAGE = re.compile(r"^[^\s@]+:0\.0\.0-unpublished@sha256:UNPUBLISHED_[A-Z]+_DIGEST$")


def load(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a YAML mapping")
    return value


def validate_compose(path: Path) -> list[str]:
    errors: list[str] = []
    data = load(path)
    services = data.get("services")
    if not isinstance(services, dict) or set(services) != EXPECTED_SERVICES:
        return [f"{path}: services must be exactly {sorted(EXPECTED_SERVICES)}"]
    for name, service in services.items():
        if not isinstance(service, dict):
            errors.append(f"{path}: {name} is not a mapping")
            continue
        image = service.get("image", "")
        if not (DIGEST_IMAGE.fullmatch(str(image)) or SENTINEL_IMAGE.fullmatch(str(image))):
            errors.append(
                f"{path}: {name} image is neither a real digest nor a fail-closed sentinel"
            )
        if ":latest" in str(image):
            errors.append(f"{path}: {name} uses latest")
        if service.get("privileged") is True:
            errors.append(f"{path}: {name} is privileged")
        required = (
            "read_only",
            "tmpfs",
            "cap_drop",
            "security_opt",
            "pids_limit",
            "mem_limit",
            "cpus",
            "restart",
            "stop_grace_period",
        )
        for key in required:
            if key not in service:
                errors.append(f"{path}: {name} lacks {key}")
        if name not in {"initialize", "migrate"} and "healthcheck" not in service:
            errors.append(f"{path}: {name} lacks healthcheck")
        if service.get("read_only") is not True:
            errors.append(f"{path}: {name} root filesystem is not read-only")
        if service.get("user") != EXPECTED_USERS[name]:
            errors.append(f"{path}: {name} does not use expected numeric UID:GID")
        uid, gid = EXPECTED_USERS[name].split(":", maxsplit=1)
        tmpfs_entries = [str(entry) for entry in service.get("tmpfs", [])]
        if not any(f"uid={uid}" in entry and f"gid={gid}" in entry for entry in tmpfs_entries):
            errors.append(f"{path}: {name} lacks a tmpfs owned by its runtime UID:GID")
        if set(service.get("networks", [])) != EXPECTED_NETWORKS[name]:
            errors.append(f"{path}: {name} has unexpected network membership")
        if service.get("cap_drop") != ["ALL"]:
            errors.append(f"{path}: {name} must drop all capabilities")
        if name != "initialize" and service.get("cap_add"):
            errors.append(f"{path}: {name} must not add capabilities")
        if "no-new-privileges:true" not in service.get("security_opt", []):
            errors.append(f"{path}: {name} lacks no-new-privileges")
        if name != "gateway" and service.get("ports"):
            errors.append(f"{path}: only gateway may publish ports")
        if any("docker.sock" in str(v) for v in service.get("volumes", [])):
            errors.append(f"{path}: {name} mounts Docker socket")
        if name in service.get("depends_on", {}):
            errors.append(f"{path}: {name} depends on itself")
        for env_key, env_value in service.get("environment", {}).items():
            if any(fragment in str(env_key).lower() for fragment in ("password", "secret", "key")):
                if not str(env_key).endswith("_FILE"):
                    errors.append(f"{path}: {name} secret-like env {env_key} is not file-based")
                if not str(env_value).startswith("/run/secrets/"):
                    errors.append(f"{path}: {name} {env_key} does not point to /run/secrets")
        for volume in service.get("volumes", []):
            if not isinstance(volume, dict) or volume.get("type") != "bind":
                errors.append(f"{path}: {name} volume is not a long-form bind mount")
                continue
            source = str(volume.get("source", ""))
            if source not in EXPECTED_HOST_SOURCES:
                errors.append(f"{path}: {name} bind mount is not an exact UI-created dataset root")
            if volume.get("bind", {}).get("create_host_path") is not False:
                errors.append(f"{path}: {name} bind mount may create an unexpected host path")
    expected_application_images = {
        "initialize": "api",
        "migrate": "api",
        "api": "api",
        "worker": "api",
        "frontend": "frontend",
        "gateway": "gateway",
        "backup": "backup",
    }
    for service_name, component in expected_application_images.items():
        expected_repository = APP_IMAGES[component]
        expected_sentinel = (
            f"{expected_repository}:0.0.0-unpublished@sha256:UNPUBLISHED_{component.upper()}_DIGEST"
        )
        image = str(services[service_name].get("image", ""))
        published_pattern = re.compile(
            rf"^{re.escape(expected_repository)}:[^\s@]+@sha256:[0-9a-f]{{64}}$"
        )
        if image != expected_sentinel and not published_pattern.fullmatch(image):
            errors.append(f"{path}: {service_name} must use the exact {component} image contract")
    if data.get("networks", {}).get("database", {}).get("internal") is not True:
        errors.append(f"{path}: database network is not internal")
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
        errors.append(f"{path}: gateway must publish only target port 8443")
    if services["migrate"].get("image") != services["api"].get("image"):
        errors.append(f"{path}: migrate must reuse the API image")
    if services["initialize"].get("image") != services["api"].get("image"):
        errors.append(f"{path}: initialize must reuse the API image")
    if services["worker"].get("image") != services["api"].get("image"):
        errors.append(f"{path}: worker must reuse the API image")
    postgres_environment = services["postgres"].get("environment", {})
    if postgres_environment.get("POSTGRES_USER") != "pm_bootstrap":
        errors.append(f"{path}: PostgreSQL must use the isolated bootstrap role")
    if postgres_environment.get("POSTGRES_PASSWORD_FILE") != (
        "/run/secrets/postgres_bootstrap_password"
    ):
        errors.append(f"{path}: PostgreSQL bootstrap secret is incorrect")
    for name, (role, secret_name) in EXPECTED_DATABASE_ROLES.items():
        environment = services[name].get("environment", {})
        if environment.get("PM_DATABASE_USER") != role:
            errors.append(f"{path}: {name} must use database role {role}")
        if environment.get("PM_DATABASE_PASSWORD_FILE") != f"/run/secrets/{secret_name}":
            errors.append(f"{path}: {name} database secret is incorrect")
        mounted_secrets = {
            item if isinstance(item, str) else item.get("source")
            for item in services[name].get("secrets", [])
        }
        if secret_name not in mounted_secrets:
            errors.append(f"{path}: {name} does not mount its database secret")
        if "postgres_bootstrap_password" in mounted_secrets:
            errors.append(f"{path}: {name} mounts the prohibited bootstrap secret")
    if services["migrate"].get("command") != ["python", "-m", "backend.app.migrate"]:
        errors.append(f"{path}: migrate must use the role-aware migration entrypoint")
    for name in ("migrate", "api", "worker"):
        if services[name].get("environment", {}).get("PM_SERVICE_ROLE") != name:
            errors.append(f"{path}: {name} PM_SERVICE_ROLE is incorrect")
    if services["backup"].get("environment", {}).get("PM_RESTORE_DATABASE_USER") != (
        "pm_restore_test"
    ):
        errors.append(f"{path}: backup lacks the isolated restore role")
    if (
        services["backup"].get("environment", {}).get("PM_RESTORE_DATABASE_PASSWORD_FILE")
        != "/run/secrets/postgres_restore_password"
    ):
        errors.append(f"{path}: backup restore secret is incorrect")
    initializer = services["initialize"]
    if initializer.get("command") != [
        "python",
        "/opt/powermeter/host-initializer/initialize_host.py",
    ]:
        errors.append(f"{path}: initialize must use the embedded host initializer")
    if initializer.get("network_mode") != "none":
        errors.append(f"{path}: initialize must have no network namespace connectivity")
    if set(initializer.get("cap_add", [])) != {"CHOWN", "FOWNER", "DAC_OVERRIDE"}:
        errors.append(f"{path}: initialize has unexpected added capabilities")
    expected_initializer_sources = EXPECTED_HOST_SOURCES
    initializer_mounts = {
        (item.get("source"), item.get("target"), item.get("read_only", False))
        for item in initializer.get("volumes", [])
        if isinstance(item, dict)
    }
    expected_initializer_mounts = {
        (source, source.replace("/mnt/Apps/PowerMeterV2", "/host"), False)
        for source in expected_initializer_sources
    }
    if len(initializer.get("volumes", [])) != len(expected_initializer_mounts) or (
        initializer_mounts != expected_initializer_mounts
    ):
        errors.append(f"{path}: initialize must mount only the exact required host datasets")
    initializer_secrets = {
        (
            item.get("source"),
            item.get("target"),
            str(item.get("uid")),
            str(item.get("gid")),
            item.get("mode"),
        )
        for item in initializer.get("secrets", [])
        if isinstance(item, dict)
    }
    expected_initializer_secrets = {
        (source, target, "0", "0", 0o400) for source, target in EXPECTED_SECRETS.items()
    }
    if initializer_secrets != expected_initializer_secrets:
        errors.append(f"{path}: initialize must receive every secret individually")
    for name, service in services.items():
        if name == "initialize":
            continue
        if service.get("depends_on", {}).get("initialize") != {
            "condition": "service_completed_successfully"
        }:
            errors.append(f"{path}: {name} is not gated by successful host initialization")
        if any(
            isinstance(item, dict)
            and (
                str(item.get("source", "")).rstrip("/") == "/mnt/Apps/PowerMeterV2/secrets"
                or str(item.get("source", "")).startswith("/mnt/Apps/PowerMeterV2/secrets/")
            )
            for item in service.get("volumes", [])
        ):
            errors.append(f"{path}: {name} must not mount the secrets dataset")
    postgres_mounts = services["postgres"].get("volumes", [])
    if not any(
        isinstance(item, dict)
        and item.get("source") == "/mnt/Apps/PowerMeterV2/config"
        and item.get("target") == "/docker-entrypoint-initdb.d"
        and item.get("read_only") is True
        for item in postgres_mounts
    ):
        errors.append(f"{path}: PostgreSQL role initializer bind is missing or writable")
    expected_config_mounts = {
        "postgres": "/docker-entrypoint-initdb.d",
        "api": "/data/config",
        "worker": "/data/config",
        "gateway": "/etc/caddy",
    }
    for name, target in expected_config_mounts.items():
        matching = [
            item
            for item in services[name].get("volumes", [])
            if isinstance(item, dict) and item.get("source") == "/mnt/Apps/PowerMeterV2/config"
        ]
        if (
            len(matching) != 1
            or matching[0].get("target") != target
            or matching[0].get("read_only") is not True
        ):
            errors.append(f"{path}: {name} must mount the config dataset read-only")
    worker_environment = services["worker"].get("environment", {})
    worker_health_file = "/tmp/worker-health.json"  # noqa: S108 - container-private tmpfs
    if worker_environment.get("PM_WORKER_HEALTH_FILE") != worker_health_file:
        errors.append(f"{path}: worker health evidence must use writable tmpfs")
    if worker_health_file not in str(services["worker"].get("healthcheck", {})):
        errors.append(f"{path}: worker health check does not validate heartbeat evidence")
    api_environment = services["api"].get("environment", {})
    if api_environment.get("PM_LOG_DIR") != "/data/logs/application":
        errors.append(f"{path}: API structured log directory is not configured")
    if str(api_environment.get("PM_LOG_RETENTION_DAYS")) != "${PM_LOG_RETENTION_DAYS:-90}":
        errors.append(f"{path}: API log retention does not default to 90 days")
    for name in ("api", "worker"):
        environment = services[name].get("environment", {})
        if environment.get("PM_BACKUP_STATUS_DIR") != "/data/backups/status":
            errors.append(f"{path}: {name} backup evidence directory is not configured")
    for name in ("initialize", "migrate"):
        if services[name].get("restart") != "no":
            errors.append(f"{path}: one-shot {name} must not restart")
    for name in EXPECTED_SERVICES - {"initialize", "migrate"}:
        if services[name].get("restart") != "unless-stopped":
            errors.append(f"{path}: {name} must use unless-stopped restart policy")
    for name in ("api", "worker"):
        mounts = {
            (volume.get("source"), volume.get("target"), volume.get("read_only", False))
            for volume in services[name].get("volumes", [])
            if isinstance(volume, dict)
        }
        if (
            "/mnt/Apps/PowerMeterV2/backups",
            "/data/backups",
            True,
        ) not in mounts:
            errors.append(f"{path}: {name} lacks the read-only backup evidence mount")
    secrets = data.get("secrets", {})
    if not isinstance(secrets, dict) or set(secrets) != set(EXPECTED_SECRETS):
        errors.append(f"{path}: file secrets must be the exact required 13-file set")
    else:
        for name, expected_basename in EXPECTED_SECRETS.items():
            secret = secrets[name]
            source = secret.get("file") if isinstance(secret, dict) else None
            expected_source = f"/mnt/Apps/PowerMeterV2/secrets/{expected_basename}"
            if source != expected_source:
                errors.append(f"{path}: secret {name} does not use its exact host file")
    return errors


def validate_gateway(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    required = (
        "tls /run/secrets/tls_cert /run/secrets/tls_key",
        "/health/live",
        "/health/ready",
        "reverse_proxy api:8000",
        "reverse_proxy frontend:8080",
        "/var/log/powermeter/gateway/gateway-access.json",
        "Strict-Transport-Security",
        "Content-Security-Policy",
    )
    for value in required:
        if value not in text:
            errors.append(f"{path}: missing {value}")
    if "trusted_proxies static private_ranges" in text:
        errors.append(f"{path}: broadly trusted private-network proxy headers are prohibited")
    return errors


def validate_actions(root: Path) -> list[str]:
    errors: list[str] = []
    for path in sorted((root / ".github/workflows").glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        if "pull_request_target:" in text:
            errors.append(f"{path}: pull_request_target is prohibited")
        for line_number, line in enumerate(text.splitlines(), 1):
            if match := re.search(r"\buses:\s*([^\s#]+)", line):
                value = match.group(1)
                if value.startswith("./") or value.startswith("docker://"):
                    continue
                ref = value.rsplit("@", 1)[-1]
                if not re.fullmatch(r"[0-9a-f]{40}", ref):
                    errors.append(f"{path}:{line_number}: action must be pinned to a 40-char SHA")
    return errors


def validate_source_boundaries(root: Path) -> list[str]:
    errors: list[str] = []
    forbidden = re.compile(r"(?i)(BEGIN (RSA|EC|OPENSSH) PRIVATE KEY|AKIA[0-9A-Z]{16})")
    excluded = {
        ".git",
        ".venv",
        ".npm-cache",
        "node_modules",
        "dist",
        "build",
        ".docker-tmp",
        ".test-runtime",
    }
    for path in root.rglob("*"):
        if not path.is_file() or any(part in excluded for part in path.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".bin", ".pyc"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if forbidden.search(text):
            errors.append(f"{path}: possible committed credential/private key")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    errors = validate_compose(root / "deploy/truenas/power-monitor-v2.yaml")
    errors.extend(validate_gateway(root / "deploy/caddy/Caddyfile"))
    errors.extend(validate_actions(root))
    errors.extend(validate_source_boundaries(root))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("release/deployment static validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
