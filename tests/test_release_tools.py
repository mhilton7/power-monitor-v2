from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from scripts.redact_deployment_logs import LOG_EVENTS as SANITIZED_LOG_EVENTS
from scripts.redact_deployment_logs import MAX_LOG_LINES, sanitize_stream
from scripts.render_truenas_release import (
    ReleaseError,
    load_yaml,
    render,
    validate_compose,
)
from scripts.render_truenas_release import (
    main as render_release_main,
)
from scripts.validate_deployment_evidence import (
    FAILURE_DIAGNOSTICS,
    SUCCESS_CHECKS,
    SUCCESS_SERVICES,
    EvidenceError,
    validate,
)
from scripts.validate_deployment_evidence import (
    LOG_EVENTS as VALIDATED_LOG_EVENTS,
)
from scripts.validate_release import validate_compose as validate_static_compose
from scripts.verify_hardware_certification import canonical_record_sha256, load_strict_json, verify
from scripts.verify_release_artifacts import verify_release_artifacts

ROOT = Path(__file__).resolve().parents[1]
DIGEST_A = "sha256:" + "1" * 64
DIGEST_B = "sha256:" + "2" * 64
DIGEST_C = "sha256:" + "3" * 64
DIGEST_D = "sha256:" + "4" * 64
COMMIT = "a" * 40
IMAGE = "b" * 64
FIRMWARE_BUILD_ID = "c" * 64
VERSION = "0.1.0-rc.22"


@pytest.fixture
def evidence_dir() -> Iterator[Path]:
    path = ROOT / ".test-runtime" / f"deployment-evidence-{uuid.uuid4()}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)


def _write(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def _candidate_release_bundle(directory: Path) -> tuple[Path, dict[str, object]]:
    template = (ROOT / "deploy/truenas/power-monitor-v2.yaml").read_text(encoding="utf-8")
    compose_text = render(
        template,
        VERSION,
        DIGEST_A,
        DIGEST_B,
        DIGEST_D,
        DIGEST_C,
        revision=COMMIT,
        build_time="2026-08-17T00:00:00Z",
        frontend_asset_id=f"{VERSION}-{COMMIT[:16]}",
    )
    compose_path = directory / "power-monitor-v2-test.yaml"
    compose_path.write_text(compose_text, encoding="utf-8")
    manifest: dict[str, object] = {
        "schema": "pm-server-release/1.1.0",
        "protocol": "pm-protocol/1.0.0",
        "telemetry_protocol": "pm-telemetry/2.0.0",
        "version": VERSION,
        "revision": COMMIT,
        "release_status": "candidate_physical_certification_pending",
        "database": {"expected_migration": "20260820_0018"},
        "frontend": {
            "version": VERSION,
            "revision": COMMIT,
            "static_asset_build_id": f"{VERSION}-{COMMIT[:16]}",
            "build_time": "2026-08-17T00:00:00Z",
        },
        "firmware_release_url": (
            "https://github.com/mhilton7/power-monitor-sensor-headless/releases/tag/v0.1.0-rc.22"
        ),
        "firmware": {
            "repository": "https://github.com/mhilton7/power-monitor-sensor-headless",
            "tag": "v0.1.0-rc.22",
            "revision": COMMIT,
            "build_number": 25,
            "firmware_build_id": FIRMWARE_BUILD_ID,
            "image_sha256": IMAGE,
            "protocol": "pm-protocol/1.0.0",
            "telemetry_protocol": "pm-telemetry/2.0.0",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
        },
        "hardware_certification": {"status": "pending", "physical": False},
        "images": {
            "api": {"name": "ghcr.io/mhilton7/power-monitor-v2-api", "digest": DIGEST_A},
            "frontend": {
                "name": "ghcr.io/mhilton7/power-monitor-v2-frontend",
                "digest": DIGEST_B,
            },
            "gateway": {
                "name": "ghcr.io/mhilton7/power-monitor-v2-gateway",
                "digest": DIGEST_D,
            },
            "backup": {
                "name": "ghcr.io/mhilton7/power-monitor-v2-backup",
                "digest": DIGEST_C,
            },
        },
        "compose": {
            "file": compose_path.name,
            "sha256": hashlib.sha256(compose_path.read_bytes()).hexdigest(),
        },
    }
    manifest_path = directory / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest_path, manifest


def _authenticated_evidence() -> dict[str, object]:
    return {
        "schema": "pm-deployment-authenticated-evidence/1.0.0",
        "status": "passed",
        "protocol": "pm-protocol/1.0.0",
        "device_id": "123e4567-e89b-12d3-a456-426614174000",
        "enrollment": "authenticated",
        "heartbeat": "authenticated_pzem",
        "reading_sequence": 1,
        "energy_kwh": "0.2452",
        "usage_source": "authenticated PZEM-004T sensor intervals only",
        "rate_source": "reviewed_rate_only_pdf",
        "rate_source_sha256": "c" * 64,
        "cost": "0.17",
        "cost_rule": "sum_only_when_every_visible_interval_is_priced",
        "command": {
            "id": "123e4567-e89b-42d3-a456-426614174001",
            "type": "reboot",
            "delivery": "authenticated",
            "state": "succeeded",
            "result_code": "REBOOT_COMPLETED",
        },
    }


def _backup_evidence(*, restore: bool) -> dict[str, object]:
    minimum = 5 if restore else 4
    return {
        "format": "pm-backup/1.0.0",
        "state": "verified",
        "run_id": "restore-verified" if restore else "backup-verified",
        "sha256": "d" * 64,
        "verification_checks": [f"check-{index}" for index in range(minimum)],
    }


def _write_success_evidence(prefix: Path) -> None:
    authenticated = _authenticated_evidence()
    report = {
        "schema": "pm-deployment-test/1.0.0",
        "version": VERSION,
        "revision": COMMIT,
        "completed_at": "2026-08-14T12:00:00Z",
        "status": "passed",
        "services": SUCCESS_SERVICES,
        "checks": SUCCESS_CHECKS,
        "rollback": "not_exercised_github_hosted_smoke",
        "pdf_sandbox": {
            "schema_id": "pm-pdf-sandbox-health/1.0.0",
            "pdf_sandbox": "enforced",
        },
        "authenticated_sensor_evidence": authenticated,
        "backup": _backup_evidence(restore=False),
        "restore_test": _backup_evidence(restore=True),
    }
    _write(prefix.with_name(prefix.name + ".json"), json.dumps(report, allow_nan=False))
    _write(
        prefix.with_name(prefix.name + "-authenticated.json"),
        json.dumps(authenticated, allow_nan=False),
    )
    compose_records = [
        {"service": service, "state": "running", "health": "healthy", "exit_code": 0}
        for service in ("postgres", "api", "worker", "frontend", "gateway", "backup")
    ]
    compose_records.extend(
        {"service": service, "state": "exited", "health": None, "exit_code": 0}
        for service in ("initialize", "migrate")
    )
    _write(
        prefix.with_name(prefix.name + "-compose-ps.jsonl"),
        "".join(json.dumps(record) + "\n" for record in compose_records),
    )
    base = "/mnt/Apps/PowerMeterV2"
    permission_records = [
        f"directory|{base}/postgres|70:70|700",
        f"directory|{base}/config|0:0|755",
        f"directory|{base}/firmware|10001:10001|750",
        f"directory|{base}/backups|568:568|750",
        f"directory|{base}/backups/status|568:568|750",
        f"directory|{base}/logs|0:0|711",
        f"directory|{base}/logs/application|10001:10001|750",
        f"directory|{base}/logs/gateway|1000:1000|750",
        f"directory|{base}/rate-source-artifacts|10001:10001|750",
        f"directory|{base}/caddy-data|1000:1000|750",
        f"directory|{base}/caddy-config|1000:1000|750",
        f"directory|{base}/secrets|0:0|711",
        f"config|{base}/config/Caddyfile|0:0|440",
        f"config|{base}/config/postgres-init-roles.sh|0:0|440",
        f"acl|{base}/config/Caddyfile|exact-caddy-read-only",
        f"acl|{base}/config/postgres-init-roles.sh|exact-postgres-read-only",
        f"acl|{base}/backups|exact-api-traverse-only",
        f"acl|{base}/backups/status|exact-api-read-default",
    ]
    readers = {
        "postgres_bootstrap_password": "70",
        "postgres_migrator_password": "70,10001",
        "postgres_api_password": "70,10001",
        "postgres_worker_password": "70,10001",
        "postgres_backup_password": "70,568",
        "postgres_restore_password": "70,568",
        "session_secret": "10001",
        "field_encryption_key": "10001",
        "ota_manifest_key": "10001",
        "backup_encryption_key": "568",
        "tls.crt": "1000",
        "tls.key": "1000",
        "tls-ca.crt": "1000",
    }
    for name, allowed in readers.items():
        permission_records.extend(
            (f"secret|{base}/secrets/{name}|0:0|440", f"readers|{name}|{allowed}")
        )
    _write(
        prefix.with_name(prefix.name + "-permissions.txt"),
        "\n".join(permission_records) + "\n",
    )


def _write_failure_evidence(prefix: Path) -> None:
    failure = {
        "schema": "pm-deployment-failure/1.0.0",
        "version": VERSION,
        "revision": COMMIT,
        "completed_at": "2026-08-14T12:00:00Z",
        "status": "failed",
        "exit_code": 1,
        "failed_assertion": "outside_instrumented_recovery",
        "diagnostics": FAILURE_DIAGNOSTICS,
    }
    _write(
        prefix.with_name(prefix.name + "-failure.json"),
        json.dumps(failure, allow_nan=False),
    )
    _write(
        prefix.with_name(prefix.name + "-failure-compose-ps.jsonl"),
        '{"service":"api","state":"running","health":"unhealthy","exit_code":0}\n',
    )
    health = {
        "service": "api",
        "container_id": "a" * 64,
        "state": {
            "status": "running",
            "running": True,
            "restarting": False,
            "oom_killed": False,
            "dead": False,
            "exit_code": 0,
            "health": {"status": "unhealthy", "failing_streak": 3},
            "readiness": {
                "http_status": 503,
                "status": "not_ready",
                "database": "ready",
                "pdf_sandbox": "unavailable",
            },
        },
    }
    _write(
        prefix.with_name(prefix.name + "-failure-health.jsonl"),
        json.dumps(health, allow_nan=False) + "\n",
    )
    _write(
        prefix.with_name(prefix.name + "-failure-log-events.jsonl"),
        '{"line_number":1,"service":"api","timestamp":"2026-08-14T12:00:00Z","event":"readiness_not_ready"}\n',
    )


def _validate(prefix: Path, *, outcome: str) -> None:
    validate(
        prefix,
        outcome=outcome,
        expected_version=VERSION,
        expected_revision=COMMIT,
    )


def test_deployment_evidence_validator_accepts_only_complete_exact_sets(
    evidence_dir: Path,
) -> None:
    success = evidence_dir / "success-report"
    _write_success_evidence(success)
    _validate(success, outcome="success")

    failure = evidence_dir / "failure-report"
    _write_failure_evidence(failure)
    _validate(failure, outcome="failure")

    partial = evidence_dir / "partial-report"
    _write_success_evidence(partial)
    partial.with_name(partial.name + "-permissions.txt").unlink()
    with pytest.raises(EvidenceError, match=r"missing|exact|nonempty"):
        _validate(partial, outcome="success")

    mixed = evidence_dir / "mixed-report"
    _write_failure_evidence(mixed)
    _write(mixed.with_name(mixed.name + ".json"), "{}")
    with pytest.raises(EvidenceError, match=r"unexpected|opposite"):
        _validate(mixed, outcome="failure")

    with pytest.raises(EvidenceError, match="unsupported"):
        _validate(failure, outcome="cancelled")

    malformed = evidence_dir / "malformed-report"
    _write_success_evidence(malformed)
    report_path = malformed.with_name(malformed.name + ".json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("authenticated_sensor_evidence")
    _write(report_path, json.dumps(report))
    with pytest.raises(EvidenceError, match="missing or unexpected"):
        _validate(malformed, outcome="success")

    invalid_rollback = evidence_dir / "invalid-rollback-report"
    _write_success_evidence(invalid_rollback)
    invalid_report_path = invalid_rollback.with_name(invalid_rollback.name + ".json")
    invalid_report = json.loads(invalid_report_path.read_text(encoding="utf-8"))
    invalid_report["rollback"] = "passed"
    _write(invalid_report_path, json.dumps(invalid_report))
    with pytest.raises(EvidenceError, match="rollback status is unexpected"):
        _validate(invalid_rollback, outcome="success")

    with pytest.raises(EvidenceError, match="expected release"):
        validate(
            failure,
            outcome="failure",
            expected_version=VERSION,
            expected_revision="b" * 40,
        )

    unsafe_assertion = evidence_dir / "unsafe-assertion-report"
    _write_failure_evidence(unsafe_assertion)
    unsafe_assertion_path = unsafe_assertion.with_name(unsafe_assertion.name + "-failure.json")
    unsafe_report = json.loads(unsafe_assertion_path.read_text(encoding="utf-8"))
    unsafe_report["failed_assertion"] = "password=arbitrary diagnostic text"
    _write(unsafe_assertion_path, json.dumps(unsafe_report))
    with pytest.raises(EvidenceError, match="fixed allowlist"):
        _validate(unsafe_assertion, outcome="failure")


@pytest.mark.parametrize("mutation", ["unsafe_extra", "duplicate", "blank"])
def test_deployment_permissions_evidence_requires_one_exact_record_set(
    evidence_dir: Path,
    mutation: str,
) -> None:
    prefix = evidence_dir / f"permissions-{mutation}"
    _write_success_evidence(prefix)
    path = prefix.with_name(prefix.name + "-permissions.txt")
    original = path.read_text(encoding="utf-8")
    first = original.splitlines()[0]
    if mutation == "unsafe_extra":
        changed = original + "secret|/mnt/Apps/PowerMeterV2/secrets/tls.key|0:0|777\n"
        match = "exact corrected state"
    elif mutation == "duplicate":
        changed = original + first + "\n"
        match = "duplicate record"
    else:
        changed = original + "\n"
        match = "blank record"
    _write(path, changed)
    with pytest.raises(EvidenceError, match=match):
        _validate(prefix, outcome="success")


def test_deployment_failure_health_allowlist_checks_every_jsonl_record(
    evidence_dir: Path,
) -> None:
    prefix = evidence_dir / "failure-report"
    _write_failure_evidence(prefix)
    health_path = prefix.with_name(prefix.name + "-failure-health.jsonl")
    unsafe = {
        "service": "api",
        "container_id": "b" * 64,
        "state": {
            "status": "running",
            "running": True,
            "restarting": False,
            "oom_killed": False,
            "dead": False,
            "exit_code": 0,
            "health": {"status": "unhealthy", "failing_streak": 3, "log": "secret"},
        },
    }
    safe = json.loads(health_path.read_text(encoding="utf-8"))
    _write(health_path, json.dumps(unsafe) + "\n" + json.dumps(safe) + "\n")
    with pytest.raises(EvidenceError, match="non-allowlisted"):
        _validate(prefix, outcome="failure")

    unsafe.pop("state")
    unsafe.update({"service": "arbitrary secret text", "state": None})
    _write(health_path, json.dumps(unsafe) + "\n")
    with pytest.raises(EvidenceError, match="fixed allowlist"):
        _validate(prefix, outcome="failure")

    _write(health_path, json.dumps(safe) + "\n\n" + json.dumps(safe) + "\n")
    with pytest.raises(EvidenceError, match="blank JSONL"):
        _validate(prefix, outcome="failure")


def test_deployment_log_sanitizer_is_bounded_and_emits_no_raw_text() -> None:
    secret = "A" * 96
    lines = [
        f"api-1 | 2026-08-14T12:00:00.123456789Z password={secret}\n",
        "api-1 | -----BEGIN PRIVATE KEY-----\n",
        f"api-1 | {secret}\n",
        "api-1 | INFO: 127.0.0.1 GET /health/ready HTTP/1.1 503 Service Unavailable\n",
    ]
    records = sanitize_stream(lines * (MAX_LOG_LINES + 1))
    encoded = json.dumps(records, allow_nan=False)
    assert SANITIZED_LOG_EVENTS == VALIDATED_LOG_EVENTS
    assert len(records) == MAX_LOG_LINES
    assert secret not in encoded
    assert "PRIVATE KEY" not in encoded
    assert all(
        set(record) == {"line_number", "service", "timestamp", "event"} for record in records
    )
    assert records[-1] == {
        "line_number": MAX_LOG_LINES,
        "service": "api",
        "timestamp": None,
        "event": "readiness_not_ready",
    }


def test_deployment_evidence_rejects_symlinks_when_supported(evidence_dir: Path) -> None:
    prefix = evidence_dir / "success-report"
    _write_success_evidence(prefix)
    permissions = prefix.with_name(prefix.name + "-permissions.txt")
    target = evidence_dir / "target.txt"
    _write(target, "permissions verified\n")
    permissions.unlink()
    try:
        permissions.symlink_to(target)
    except OSError:
        pytest.skip("the test environment does not permit symlink creation")
    with pytest.raises(EvidenceError, match="symlink"):
        _validate(prefix, outcome="success")


def test_local_compose_backup_uses_the_backup_script_contract() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    backup = compose["services"]["backup"]
    assert backup["environment"] == {
        "PM_DATABASE_HOST": "postgres",
        "PM_DATABASE_PORT": "5432",
        "PM_DATABASE_NAME": "powermeter",
        "PM_DATABASE_USER": "pm_backup",
        "PM_DATABASE_PASSWORD_FILE": "/run/secrets/postgres_backup_password",
        "PM_RESTORE_DATABASE_USER": "pm_restore_test",
        "PM_RESTORE_DATABASE_PASSWORD_FILE": "/run/secrets/postgres_restore_password",
        "PM_BACKUP_ENCRYPTION_KEY_FILE": "/run/secrets/backup_key",
        "PM_BACKUP_DIR": "/backups",
    }
    assert set(backup["secrets"]) == {
        "postgres_backup_password",
        "postgres_restore_password",
        "backup_key",
    }

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "PM_BACKUP_ENCRYPTION_KEY_FILE=/run/secrets/backup_key" in example
    assert "PM_BACKUP_DIR=/backups" in example
    assert "PM_BACKUP_KEY_FILE=" not in example

    installation = (ROOT / "docs/INSTALLATION.md").read_text(encoding="utf-8")
    command = "docker compose -f compose.yaml -f compose.dev.yaml up --build --wait"
    assert command in installation
    assert "docker compose -f compose.dev.yaml up" not in installation


def test_compose_separates_database_roles_and_application_secrets() -> None:
    for path in (ROOT / "compose.yaml", ROOT / "deploy/truenas/power-monitor-v2.yaml"):
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        services = compose["services"]
        expected = {
            "migrate": ("pm_migrator", "postgres_migrator_password"),
            "api": ("pm_api", "postgres_api_password"),
            "worker": ("pm_worker", "postgres_worker_password"),
            "backup": ("pm_backup", "postgres_backup_password"),
        }
        for service_name, (role, secret) in expected.items():
            service = services[service_name]
            environment = service["environment"]
            assert environment["PM_DATABASE_USER"] == role
            assert environment["PM_DATABASE_PASSWORD_FILE"].endswith(secret)
            mounted = {
                entry if isinstance(entry, str) else entry["source"] for entry in service["secrets"]
            }
            assert secret in mounted
        migrate_secrets = services["migrate"]["secrets"]
        worker_secrets = services["worker"]["secrets"]
        assert len(migrate_secrets) == 1
        assert len(worker_secrets) == 1
        assert services["migrate"]["environment"]["PM_SERVICE_ROLE"] == "migrate"
        assert services["worker"]["environment"]["PM_SERVICE_ROLE"] == "worker"

    role_script = (ROOT / "deploy/postgres/init-roles.sh").read_text(encoding="utf-8")
    assert "ALTER ROLE pm_bootstrap NOLOGIN" in role_script
    assert "GRANT SELECT ON TABLES TO pm_backup" in role_script
    assert "NOSUPERUSER" in role_script
    development_override = (ROOT / "compose.dev.yaml").read_text(encoding="utf-8")
    assert "postgresql+asyncpg://powermeter:" not in development_override
    assert "secrets: []" not in development_override
    local_secret_script = (ROOT / "scripts/create_local_secrets.ps1").read_text(encoding="utf-8")
    for name in (
        "postgres_bootstrap_password",
        "postgres_migrator_password",
        "postgres_api_password",
        "postgres_worker_password",
        "postgres_backup_password",
        "postgres_restore_password",
    ):
        assert name in local_secret_script


def test_gateway_enforces_ingress_body_limits_before_api_buffering() -> None:
    caddy = (ROOT / "deploy/caddy/Caddyfile").read_text(encoding="utf-8")
    expected = {
        "/api/v1/device/heartbeat /api/v1/device/permanent-loss /api/v1/devices/enroll": "64KiB",
        "/api/v1/device/readings": "1MiB",
        "/api/v1/bill-rate-imports": "11MiB",
        "/api/v1/firmware/releases": "9MiB",
    }
    for paths, maximum in expected.items():
        start = caddy.index(f"path {paths}")
        handle = caddy.index("handle @", start)
        next_handle = caddy.find("\n\thandle ", handle + 1)
        block = caddy[handle : next_handle if next_handle >= 0 else len(caddy)]
        assert "request_body" in block
        assert f"max_size {maximum}" in block
    generic = caddy[caddy.index("handle @api") :]
    assert "max_size 1MiB" in generic


def certification() -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "pm-hardware-certification/2.0.0",
        "evidence_id": "123e4567-e89b-12d3-a456-426614174000",
        "generated_at": "2026-08-13T00:00:00Z",
        "result": "pass",
        "firmware": {
            "repository": "https://github.com/mhilton7/power-monitor-sensor-headless",
            "commit": COMMIT,
            "image_sha256": IMAGE,
            "version": VERSION,
            "esp_idf_version": "v6.0.2",
            "target": "esp32s3",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
            "protocol": "pm-protocol/1.0.0",
            "telemetry_protocol": "pm-telemetry/2.0.0",
        },
        "marked_unit": {
            "unit_id": "marked-fixture-1",
            "esp32s3_marking": "ESP32-S3-DevKitC-1 N16R8",
            "pzem_model_marking": "PZEM-004T 100A",
            "pzem_revision_marking": "V4.0",
            "pzem_terminal_labels": "5V RX TX GND",
            "ct_marking": "100A/50mA",
            "photo_sha256": ["c" * 64],
        },
        "electrical": {
            "qualified_person": "Test operator",
            "isolated_test_fixture": "Marked isolated fixture",
            "ttl_idle_voltage_v": 3.3,
            "logic_high_voltage_v": 3.3,
            "logic_low_voltage_v": 0.1,
            "uart_baud": 9600,
            "data_bits": 8,
            "parity": "none",
            "stop_bits": 1,
            "register_map_variant": "physically verified V4 map",
        },
        "tests": {
            name: True
            for name in {
                "pzem_authenticated_samples",
                "crc_rejection",
                "wrong_slave_rejection",
                "no_sd_runtime_access",
                "no_telemetry_nvs_writes",
                "independent_sample_acceptance",
                "later_sample_after_gap",
                "latest_value_after_outage",
                "wifi_recovery",
                "server_recovery",
                "identity_preserved",
                "configuration_preserved",
                "https_chain",
                "https_hostname",
                "hmac_replay",
                "ota_success",
                "ota_rollback",
                "ota_identity_confirmed",
                "com_recovery",
                "watchdog_recovery",
            }
        },
        "soak": {
            "started_at": "2026-08-01T00:00:00Z",
            "ended_at": "2026-08-04T00:00:00Z",
            "duration_hours": 72,
            "samples_attempted": 259200,
            "samples_authenticated": 259200,
            "reboots": 4,
            "unexplained_reboots": 0,
            "data_gaps": 0,
            "maximum_resident_telemetry_samples": 2,
            "identity_changes": 0,
            "configuration_losses": 0,
            "pass": True,
        },
        "signoff": {"operator": "Operator", "reviewer": "Reviewer", "record_sha256": ""},
    }
    value["signoff"]["record_sha256"] = canonical_record_sha256(value)  # type: ignore[index]
    return value


def test_release_template_requires_exact_sentinels_and_real_digests() -> None:
    template = (ROOT / "deploy/truenas/power-monitor-v2.yaml").read_text(encoding="utf-8")
    output = render(template, VERSION, DIGEST_A, DIGEST_B, DIGEST_D, DIGEST_C)
    assert output.count(f"power-monitor-v2-api:{VERSION}@{DIGEST_A}") == 4
    assert output.count(f"power-monitor-v2-gateway:{VERSION}@{DIGEST_D}") == 1
    assert "UNPUBLISHED" not in output
    assert f'PM_RELEASE_VERSION: "{VERSION}"' in output
    assert f'PM_RELEASE_REVISION: "{"0" * 40}"' in output
    assert 'PM_EXPECTED_DATABASE_REVISION: "20260820_0018"' in output
    validate_compose(load_yaml(output), published=True)
    with pytest.raises(ReleaseError):
        render(
            template.replace("UNPUBLISHED_API_DIGEST", "MISSING", 1),
            VERSION,
            DIGEST_A,
            DIGEST_B,
            DIGEST_D,
            DIGEST_C,
        )
    with pytest.raises(ReleaseError):
        render(
            template.replace("UNPUBLISHED_GATEWAY_DIGEST", "MISSING", 1),
            VERSION,
            DIGEST_A,
            DIGEST_B,
            DIGEST_D,
            DIGEST_C,
        )
    invalid_gateway = render(
        template,
        VERSION,
        DIGEST_A,
        DIGEST_B,
        "sha256:" + "4" * 63,
        DIGEST_C,
    )
    with pytest.raises(ReleaseError, match="digest pinned"):
        validate_compose(load_yaml(invalid_gateway), published=True)


def test_static_release_validation_binds_each_exact_component_sentinel(
    evidence_dir: Path,
) -> None:
    template = (ROOT / "deploy/truenas/power-monitor-v2.yaml").read_text(encoding="utf-8")
    wrong = template.replace("UNPUBLISHED_GATEWAY_DIGEST", "UNPUBLISHED_API_DIGEST", 1)
    path = evidence_dir / "wrong-gateway-sentinel.yaml"
    path.write_text(wrong, encoding="utf-8")
    errors = validate_static_compose(path)
    assert any("gateway must use the exact gateway image contract" in error for error in errors)


def test_static_release_validation_never_echoes_secret_mapping_details(
    evidence_dir: Path,
) -> None:
    compose = yaml.safe_load(
        (ROOT / "deploy/truenas/power-monitor-v2.yaml").read_text(encoding="utf-8")
    )
    compose["secrets"]["tls_key"]["file"] = "SENSITIVE_FIXTURE_DO_NOT_LOG"
    path = evidence_dir / "invalid-secret-mapping.yaml"
    path.write_text(yaml.safe_dump(compose), encoding="utf-8")

    errors = validate_static_compose(path)

    assert errors == [f"{path}: file secrets must match the exact required 13-file mapping"]
    assert "SENSITIVE_FIXTURE_DO_NOT_LOG" not in errors[0]
    assert "tls_key" not in errors[0]


def test_release_artifact_verifier_accepts_exact_four_image_binding(
    evidence_dir: Path,
) -> None:
    manifest_path, _ = _candidate_release_bundle(evidence_dir)
    verify_release_artifacts(manifest_path)


def test_release_renderer_separates_firmware_build_number_and_exact_build_id(
    evidence_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = evidence_dir / "power-monitor-v2-v0.1.0-rc.22.yaml"
    manifest_path = evidence_dir / "release-manifest.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_truenas_release.py",
            "--template",
            str(ROOT / "deploy/truenas/power-monitor-v2.yaml"),
            "--output",
            str(output),
            "--manifest",
            str(manifest_path),
            "--version",
            VERSION,
            "--revision",
            COMMIT,
            "--api-digest",
            DIGEST_A,
            "--frontend-digest",
            DIGEST_B,
            "--gateway-digest",
            DIGEST_D,
            "--backup-digest",
            DIGEST_C,
            "--build-time",
            "2026-08-17T00:00:00Z",
            "--frontend-asset-id",
            f"{VERSION}-{COMMIT[:16]}",
            "--firmware-release-url",
            "https://github.com/mhilton7/power-monitor-sensor-headless/releases/tag/v0.1.0-rc.22",
            "--firmware-tag",
            "v0.1.0-rc.22",
            "--firmware-revision",
            COMMIT,
            "--firmware-image-sha256",
            IMAGE,
            "--firmware-build-number",
            "25",
            "--firmware-build-id",
            FIRMWARE_BUILD_ID,
        ],
    )

    assert render_release_main() == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "pm-server-release/1.1.0"
    assert manifest["firmware"]["build_number"] == 25
    assert manifest["firmware"]["firmware_build_id"] == FIRMWARE_BUILD_ID
    assert "build_id" not in manifest["firmware"]
    verify_release_artifacts(manifest_path)


def test_release_artifact_verifier_intentionally_accepts_legacy_server_manifest(
    evidence_dir: Path,
) -> None:
    manifest_path, manifest = _candidate_release_bundle(evidence_dir)
    manifest["schema"] = "pm-server-release/1.0.0"
    firmware = manifest["firmware"]
    assert isinstance(firmware, dict)
    firmware.pop("build_number")
    firmware.pop("firmware_build_id")
    firmware["build_id"] = 25
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    verify_release_artifacts(manifest_path)


def test_release_artifact_verifier_rejects_schema_downgrade_with_new_identity_fields(
    evidence_dir: Path,
) -> None:
    manifest_path, manifest = _candidate_release_bundle(evidence_dir)
    manifest["schema"] = "pm-server-release/1.0.0"
    firmware = manifest["firmware"]
    assert isinstance(firmware, dict)
    firmware["build_id"] = 25
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="mixes incompatible"):
        verify_release_artifacts(manifest_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("build_number", 0),
        ("build_number", "25"),
        ("firmware_build_id", "c" * 63),
        ("firmware_build_id", "C" * 64),
        ("firmware_build_id", 25),
    ],
)
def test_release_artifact_verifier_rejects_conflated_or_invalid_firmware_identity(
    evidence_dir: Path,
    field: str,
    value: object,
) -> None:
    manifest_path, manifest = _candidate_release_bundle(evidence_dir)
    firmware = manifest["firmware"]
    assert isinstance(firmware, dict)
    firmware[field] = value
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="firmware build"):
        verify_release_artifacts(manifest_path)


def test_release_artifact_verifier_rejects_legacy_field_in_new_manifest(
    evidence_dir: Path,
) -> None:
    manifest_path, manifest = _candidate_release_bundle(evidence_dir)
    firmware = manifest["firmware"]
    assert isinstance(firmware, dict)
    firmware["build_id"] = 25
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="ambiguous legacy"):
        verify_release_artifacts(manifest_path)


@pytest.mark.parametrize("field", ["database", "frontend", "firmware"])
def test_release_artifact_verifier_requires_runtime_build_identity(
    evidence_dir: Path,
    field: str,
) -> None:
    manifest_path, manifest = _candidate_release_bundle(evidence_dir)
    del manifest[field]
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError):
        verify_release_artifacts(manifest_path)


def test_release_images_embed_exact_source_and_frontend_asset_identity() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    frontend_dockerfile = (ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")
    backend_dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")

    for argument in (
        "VERSION=${{ steps.identity.outputs.version }}",
        "REVISION=${{ github.sha }}",
        "BUILD_TIME=${{ steps.identity.outputs.build_time }}",
        "FRONTEND_ASSET_ID=${{ steps.identity.outputs.frontend_asset_id }}",
    ):
        assert argument in workflow
    assert 'PM_FRONTEND_VERSION="${VERSION}"' in frontend_dockerfile
    assert 'PM_FRONTEND_REVISION="${REVISION}"' in frontend_dockerfile
    assert 'PM_FRONTEND_ASSET_ID="${FRONTEND_ASSET_ID}"' in frontend_dockerfile
    assert 'PM_BUILD_REVISION="${REVISION}"' in backend_dockerfile
    assert 'PM_BUILD_TIME="${BUILD_TIME}"' in backend_dockerfile


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_release_artifact_verifier_requires_exact_manifest_image_keys(
    evidence_dir: Path,
    mutation: str,
) -> None:
    manifest_path, manifest = _candidate_release_bundle(evidence_dir)
    images = manifest["images"]
    assert isinstance(images, dict)
    if mutation == "missing":
        del images["gateway"]
    else:
        images["unexpected"] = {
            "name": "ghcr.io/mhilton7/power-monitor-v2-unexpected",
            "digest": DIGEST_D,
        }
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly the expected images"):
        verify_release_artifacts(manifest_path)


def test_release_artifact_verifier_rejects_manifest_yaml_digest_mismatch(
    evidence_dir: Path,
) -> None:
    manifest_path, manifest = _candidate_release_bundle(evidence_dir)
    images = manifest["images"]
    assert isinstance(images, dict)
    gateway = images["gateway"]
    assert isinstance(gateway, dict)
    gateway["digest"] = "sha256:" + "5" * 64
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="gateway does not match"):
        verify_release_artifacts(manifest_path)


@pytest.mark.parametrize("mutation", ["wrong_gateway", "frontend_published"])
def test_release_compose_rejects_any_port_other_than_gateway_tcp_8443(
    mutation: str,
) -> None:
    template = (ROOT / "deploy/truenas/power-monitor-v2.yaml").read_text(encoding="utf-8")
    compose = load_yaml(render(template, VERSION, DIGEST_A, DIGEST_B, DIGEST_D, DIGEST_C))
    if mutation == "wrong_gateway":
        compose["services"]["gateway"]["ports"][0]["published"] = "8444"
    else:
        compose["services"]["frontend"]["ports"] = [
            {"target": 8080, "published": "8080", "protocol": "tcp"}
        ]
    with pytest.raises(ReleaseError):
        validate_compose(compose, published=True)


@pytest.mark.parametrize("field", ["commit", "image_sha256", "version"])
def test_hardware_certification_binds_exact_firmware(field: str) -> None:
    evidence = certification()
    evidence["firmware"][field] = "wrong"  # type: ignore[index]
    evidence["signoff"]["record_sha256"] = canonical_record_sha256(evidence)  # type: ignore[index]
    with pytest.raises(ValueError):
        verify(
            evidence,
            expected_commit=COMMIT,
            expected_image_sha256=IMAGE,
            expected_version=VERSION,
        )


def test_hardware_certification_happy_path_and_fail_closed_cases() -> None:
    valid = certification()
    verify(valid, expected_commit=COMMIT, expected_image_sha256=IMAGE, expected_version=VERSION)
    mutations = []
    zero_samples = copy.deepcopy(valid)
    zero_samples["soak"]["samples_authenticated"] = 0  # type: ignore[index]
    mutations.append(zero_samples)
    short = copy.deepcopy(valid)
    short["soak"]["duration_hours"] = 71.99  # type: ignore[index]
    mutations.append(short)
    failed_test = copy.deepcopy(valid)
    failed_test["tests"]["ota_rollback"] = False  # type: ignore[index]
    mutations.append(failed_test)
    bad_hash = copy.deepcopy(valid)
    bad_hash["signoff"]["record_sha256"] = "d" * 64  # type: ignore[index]
    mutations.append(bad_hash)
    for evidence in mutations:
        if evidence is not bad_hash:
            evidence["signoff"]["record_sha256"] = canonical_record_sha256(evidence)  # type: ignore[index]
        with pytest.raises(ValueError):
            verify(
                evidence,
                expected_commit=COMMIT,
                expected_image_sha256=IMAGE,
                expected_version=VERSION,
            )


def test_hardware_certification_rejects_legacy_stateful_schema() -> None:
    evidence = certification()
    evidence["schema"] = "pm-hardware-certification/1.0.0"
    evidence["signoff"]["record_sha256"] = canonical_record_sha256(evidence)  # type: ignore[index]

    with pytest.raises(ValueError, match="physical schema"):
        verify(
            evidence,
            expected_commit=COMMIT,
            expected_image_sha256=IMAGE,
            expected_version=VERSION,
        )


def test_stable_release_artifact_verifier_accepts_stateless_hardware_schema_v2(
    evidence_dir: Path,
) -> None:
    manifest_path, manifest = _candidate_release_bundle(evidence_dir)
    hardware = certification()
    evidence_path = evidence_dir / "hardware-certification.json"
    evidence_path.write_text(json.dumps(hardware, sort_keys=True), encoding="utf-8")
    manifest["release_status"] = "stable_physical_certification_passed"
    manifest["hardware_certification"] = {
        "file": evidence_path.name,
        "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "status": "passed",
        "physical": True,
    }
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    verify_release_artifacts(manifest_path)


def test_release_workflows_bind_both_firmware_build_identities() -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    promotion = (ROOT / ".github/workflows/stable-promotion.yml").read_text(encoding="utf-8")

    assert '.schema == "pm-firmware-release/1.1.0"' in release
    assert '--argjson firmware_build_number "$build_number"' in release
    assert '--arg firmware_build_id "$firmware_build_id"' in release
    assert "--argjson firmware_build_id" not in release
    assert release.count("--firmware-build-number") == 2
    assert release.count("--firmware-build-id") == 2
    assert "pm-cross-repository-contract/1.1.0" in release
    assert "--firmware-build-number 25" in ci
    assert f"--firmware-build-id {FIRMWARE_BUILD_ID}" in ci

    assert '.schema == "pm-firmware-release/1.1.0"' in promotion
    assert "firmware_build_number=\"$(jq -er '.build_number'" in promotion
    assert "firmware_build_id=\"$(jq -er '.firmware_build_id'" in promotion
    assert '--firmware-build-number "$firmware_build_number"' in promotion
    assert '--firmware-build-id "$firmware_build_id"' in promotion
    assert ".firmware.build_id" not in promotion
    assert "pm-firmware-compatibility/1.1.0" in promotion


def test_nonfinite_certification_json_is_rejected() -> None:
    path = ROOT / ".test-runtime" / "nonfinite-evidence.json"
    path.write_text('{"duration_hours": NaN}', encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="non-finite"):
            load_strict_json(path)
    finally:
        path.unlink(missing_ok=True)


def test_stable_release_inputs_are_not_accepted_for_candidate() -> None:
    # CLI enforces stable metadata pairing; this checks the parser invariant source
    # remains explicit rather than accepting a certification hash in an RC manifest.
    source = (ROOT / "scripts/render_truenas_release.py").read_text(encoding="utf-8")
    assert "hardware certification inputs are accepted only for stable releases" in source
    assert json.dumps(certification(), allow_nan=False)
