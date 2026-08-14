from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from scripts.render_truenas_release import ReleaseError, load_yaml, render, validate_compose
from scripts.verify_hardware_certification import canonical_record_sha256, load_strict_json, verify

ROOT = Path(__file__).resolve().parents[1]
DIGEST_A = "sha256:" + "1" * 64
DIGEST_B = "sha256:" + "2" * 64
DIGEST_C = "sha256:" + "3" * 64
COMMIT = "a" * 40
IMAGE = "b" * 64
VERSION = "0.1.0-rc.1"


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
                entry if isinstance(entry, str) else entry["source"]
                for entry in service["secrets"]
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
    local_secret_script = (ROOT / "scripts/create_local_secrets.ps1").read_text(
        encoding="utf-8"
    )
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
        "schema": "pm-hardware-certification/1.0.0",
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
        },
        "marked_unit": {
            "unit_id": "marked-fixture-1",
            "esp32s3_marking": "ESP32-S3-DevKitC-1 N16R8",
            "pzem_model_marking": "PZEM-004T 100A",
            "pzem_revision_marking": "V4.0",
            "pzem_terminal_labels": "5V RX TX GND",
            "ct_marking": "100A/50mA",
            "sd_module_marking": "3.3V SPI",
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
                "sd_recovery",
                "sequence_monotonic",
                "ack_replay",
                "https_chain",
                "https_hostname",
                "hmac_replay",
                "ota_success",
                "ota_rollback",
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
            "sequence_regressions": 0,
            "pass": True,
        },
        "signoff": {"operator": "Operator", "reviewer": "Reviewer", "record_sha256": ""},
    }
    value["signoff"]["record_sha256"] = canonical_record_sha256(value)  # type: ignore[index]
    return value


def test_release_template_requires_exact_sentinels_and_real_digests() -> None:
    template = (ROOT / "deploy/truenas/power-monitor-v2.yaml").read_text(encoding="utf-8")
    output = render(template, VERSION, DIGEST_A, DIGEST_B, DIGEST_C)
    assert output.count(f"power-monitor-v2-api:{VERSION}@{DIGEST_A}") == 3
    assert "UNPUBLISHED" not in output
    validate_compose(load_yaml(output), published=True)
    with pytest.raises(ReleaseError):
        render(
            template.replace("UNPUBLISHED_API_DIGEST", "MISSING", 1),
            VERSION,
            DIGEST_A,
            DIGEST_B,
            DIGEST_C,
        )


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
