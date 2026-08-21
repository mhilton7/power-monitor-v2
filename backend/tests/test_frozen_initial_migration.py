from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from backend.app.config import get_settings
from backend.app.models import (
    Base,
    Home,
    RatePlan,
    User,
    UtilityAccount,
    UtilityAccountTierThreshold,
)
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]
INITIAL_REVISION = "20260813_0001"
INITIAL_MIGRATION = ROOT / "backend" / "alembic" / "versions" / "20260813_0001_initial_v2.py"
INITIAL_TABLES = frozenset(
    {
        "alert_condition_states",
        "alert_events",
        "alert_maintenance_windows",
        "alerts",
        "application_logs",
        "audit_events",
        "backup_runs",
        "billing_cycles",
        "billing_estimates",
        "calculation_runs",
        "circuits",
        "cost_runs",
        "device_capabilities",
        "device_command_attempts",
        "device_commands",
        "device_credentials",
        "device_events",
        "device_heartbeats",
        "device_nonces",
        "devices",
        "enrollment_tokens",
        "firmware_deployments",
        "firmware_releases",
        "homes",
        "interval_costs",
        "mfa_credentials",
        "normalized_intervals",
        "notification_settings",
        "permissions",
        "rate_assignments",
        "rate_candidates",
        "rate_periods",
        "rate_plan_versions",
        "rate_plans",
        "rate_source_artifacts",
        "rate_source_revisions",
        "rate_sources",
        "rate_sync_runs",
        "raw_readings",
        "restore_tests",
        "role_permissions",
        "roles",
        "rollups",
        "sessions",
        "unavailable_sequence_ranges",
        "user_home_scopes",
        "user_roles",
        "users",
        "utility_accounts",
        "utility_bill_rate_corrections",
        "utility_bill_rate_extractions",
        "utility_bill_rate_uploads",
    }
)
INITIAL_COLUMNS = {
    "application_logs": {
        "id",
        "event_code",
        "level",
        "correlation_id",
        "device_id",
        "command_id",
        "sync_id",
        "details",
        "created_at",
    },
    "billing_estimates": {
        "id",
        "utility_account_id",
        "cost_run_id",
        "scope_start_utc",
        "scope_end_utc",
        "sensor_energy_mwh",
        "total_microdollars",
        "completeness",
        "missing_intervals",
    },
    "device_credentials": {
        "id",
        "device_id",
        "encrypted_secret",
        "fingerprint",
        "key_version",
        "created_at",
        "revoked_at",
    },
    "firmware_releases": {
        "id",
        "semantic_version",
        "build_number",
        "project_name",
        "target_chip",
        "board_profile",
        "minimum_protocol",
        "minimum_config_version",
        "image_size",
        "sha256",
        "image_path",
        "release_notes",
        "manifest_signature",
        "candidate",
        "created_at",
    },
    "rate_candidates": {
        "id",
        "source_revision_id",
        "normalized_rates",
        "diff",
        "state",
        "reviewed_by_user_id",
        "reviewed_at",
    },
    "rate_source_artifacts": {
        "id",
        "revision_id",
        "storage_path",
        "media_type",
        "byte_count",
    },
    "rate_sources": {
        "id",
        "name",
        "source_type",
        "https_url",
        "enabled",
        "check_interval_hours",
        "last_checked_at",
    },
    "rate_sync_runs": {
        "id",
        "source_id",
        "state",
        "event_code",
        "started_at",
        "completed_at",
        "revision_id",
    },
    "utility_bill_rate_uploads": {
        "id",
        "artifact_sha256",
        "encrypted_artifact_path",
        "byte_count",
        "page_count",
        "media_type",
        "state",
        "uploaded_by_user_id",
        "created_at",
        "artifact_deleted_at",
    },
}


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _table_calls(function: ast.FunctionDef, operation: str) -> frozenset[str]:
    names: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != operation or not node.args:
            continue
        name = node.args[0]
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            names.add(name.value)
    return frozenset(names)


def _config() -> Config:
    config = Config(str(ROOT / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    return config


def _sqlite_url(path: Path, *, async_driver: bool) -> str:
    driver = "+aiosqlite" if async_driver else ""
    return f"sqlite{driver}:///{path.resolve().as_posix()}"


def test_initial_revision_contains_only_explicit_frozen_operations() -> None:
    source = INITIAL_MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "backend.app.models" not in imported_modules
    assert "create_all" not in called_attributes
    assert "drop_all" not in called_attributes
    assert _table_calls(_function(tree, "upgrade"), "create_table") == INITIAL_TABLES
    assert _table_calls(_function(tree, "downgrade"), "drop_table") == INITIAL_TABLES


def test_later_orm_metadata_cannot_change_initial_revision_or_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ROOT / ".test-runtime" / f"frozen-initial-{uuid.uuid4().hex}.sqlite3"
    async_url = _sqlite_url(database, async_driver=True)
    sync_url = _sqlite_url(database, async_driver=False)
    future_table = sa.Table(
        "future_model_change_must_not_backfill_0001",
        Base.metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )

    monkeypatch.setenv("PM_ENV", "test")
    monkeypatch.setenv("PM_DATABASE_URL", async_url)
    get_settings.cache_clear()
    try:
        config = _config()
        command.upgrade(config, INITIAL_REVISION)
        sync_engine = sa.create_engine(sync_url)
        try:
            with sync_engine.connect() as connection:
                inspector = sa.inspect(connection)
                tables = set(inspector.get_table_names())
                assert tables == INITIAL_TABLES | {"alembic_version"}
                assert future_table.name not in tables
                for table, expected_columns in INITIAL_COLUMNS.items():
                    assert {column["name"] for column in inspector.get_columns(table)} == (
                        expected_columns
                    )
                upload_uniques = {
                    tuple(constraint["column_names"])
                    for constraint in inspector.get_unique_constraints("utility_bill_rate_uploads")
                }
                assert upload_uniques == {("artifact_sha256",)}
        finally:
            sync_engine.dispose()

        command.upgrade(config, "head")
        sync_engine = sa.create_engine(sync_url)
        try:
            with sync_engine.connect() as connection:
                inspector = sa.inspect(connection)
                tables = set(inspector.get_table_names())
                assert future_table.name not in tables
                assert "rate_dated_prices" in tables
                assert {
                    column["name"] for column in inspector.get_columns("rate_dated_prices")
                } == {
                    "id",
                    "rate_plan_version_id",
                    "start_utc",
                    "end_utc",
                    "price_per_kwh",
                    "delivery_per_kwh",
                    "generation_per_kwh",
                    "rate_components",
                    "source_label",
                }
                assert "tier_threshold_rule" in {
                    column["name"] for column in inspector.get_columns("rate_candidate_reviews")
                }
                assert "firmware_build_id" in {
                    column["name"] for column in inspector.get_columns("firmware_releases")
                }
        finally:
            sync_engine.dispose()

        Base.metadata.remove(future_table)
        command.check(config)
        command.downgrade(config, "base")
        sync_engine = sa.create_engine(sync_url)
        try:
            with sync_engine.connect() as connection:
                assert set(sa.inspect(connection).get_table_names()) == {"alembic_version"}
        finally:
            sync_engine.dispose()
        command.upgrade(config, "head")
        command.check(config)
    finally:
        if future_table.name in Base.metadata.tables:
            Base.metadata.remove(future_table)
        get_settings.cache_clear()
        database.unlink(missing_ok=True)


def test_upgrade_refuses_legacy_original_bill_document_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ROOT / ".test-runtime" / f"bill-retention-preflight-{uuid.uuid4().hex}.sqlite3"
    async_url = _sqlite_url(database, async_driver=True)
    sync_url = _sqlite_url(database, async_driver=False)
    monkeypatch.setenv("PM_ENV", "test")
    monkeypatch.setenv("PM_DATABASE_URL", async_url)
    get_settings.cache_clear()
    config = _config()
    try:
        command.upgrade(config, "20260813_0007")
        engine = sa.create_engine(sync_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO utility_bill_rate_uploads "
                        "(id, home_id, artifact_sha256, encrypted_artifact_path, "
                        "byte_count, page_count, media_type, state, uploaded_by_user_id, "
                        "created_at) VALUES "
                        "(:id, :home_id, :digest, :path, 128, 1, 'application/pdf', "
                        "'parsed_rate_only', :user_id, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "home_id": str(uuid.uuid4()),
                        "digest": "a" * 64,
                        "path": "/legacy/prohibited-original.pdf.enc",
                        "user_id": str(uuid.uuid4()),
                    },
                )
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="retained original bill documents"):
            command.upgrade(config, "head")

        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    "20260813_0007"
                )
                assert (
                    connection.scalar(
                        sa.text("SELECT encrypted_artifact_path FROM utility_bill_rate_uploads")
                    )
                    == "/legacy/prohibited-original.pdf.enc"
                )
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()
        database.unlink(missing_ok=True)


def test_permanent_loss_immutability_revision_preserves_existing_rows_on_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ROOT / ".test-runtime" / f"loss-immutability-{uuid.uuid4().hex}.sqlite3"
    async_url = _sqlite_url(database, async_driver=True)
    sync_url = _sqlite_url(database, async_driver=False)
    monkeypatch.setenv("PM_ENV", "test")
    monkeypatch.setenv("PM_DATABASE_URL", async_url)
    get_settings.cache_clear()
    config = _config()
    home_id = str(uuid.uuid4())
    device_id = str(uuid.uuid4())
    loss_id = str(uuid.uuid4())
    try:
        command.upgrade(config, "20260815_0008")
        engine = sa.create_engine(sync_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO homes (id, name, timezone, created_at) VALUES "
                        "(:id, 'Migration home', 'America/Los_Angeles', CURRENT_TIMESTAMP)"
                    ),
                    {"id": home_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO devices (id, home_id, friendly_name, protocol_id, "
                        "pzem_variant, ct_rating_a, measurement_scope, state, contiguous_ack, "
                        "maximum_sequence, reset_generation, created_at) VALUES "
                        "(:id, :home_id, 'Migration sensor', 'pm-protocol/1.0.0', "
                        "'pzem004t-v4-classic-candidate', 100, 'energy_only', 'enrolled', "
                        "2, 2, 0, CURRENT_TIMESTAMP)"
                    ),
                    {"id": device_id, "home_id": home_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO unavailable_sequence_ranges "
                        "(id, device_id, first_sequence, last_sequence, reason_code, "
                        "evidence_sha256, authenticated_at) VALUES "
                        "(:id, :device_id, 1, 2, 'storage_failure', :digest, "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {"id": loss_id, "device_id": device_id, "digest": "e" * 64},
                )
        finally:
            engine.dispose()

        command.upgrade(config, "20260815_0010")
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    "20260815_0010"
                )
                assert connection.execute(
                    sa.text(
                        "SELECT first_sequence, last_sequence, reason_code, evidence_sha256 "
                        "FROM unavailable_sequence_ranges WHERE id = :id"
                    ),
                    {"id": loss_id},
                ).one() == (1, 2, "storage_failure", "e" * 64)
                assert connection.execute(
                    sa.text("SELECT contiguous_ack, maximum_sequence FROM devices WHERE id = :id"),
                    {"id": device_id},
                ).one() == (2, 2)
        finally:
            engine.dispose()

        command.downgrade(config, "20260815_0009")
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    "20260815_0009"
                )
                assert connection.execute(
                    sa.text(
                        "SELECT first_sequence, last_sequence, reason_code, evidence_sha256 "
                        "FROM unavailable_sequence_ranges WHERE id = :id"
                    ),
                    {"id": loss_id},
                ).one() == (1, 2, "storage_failure", "e" * 64)
                assert connection.execute(
                    sa.text("SELECT contiguous_ack, maximum_sequence FROM devices WHERE id = :id"),
                    {"id": device_id},
                ).one() == (2, 2)
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()
        database.unlink(missing_ok=True)


def test_settings_revision_normalizes_email_adds_fields_and_downgrades_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ROOT / ".test-runtime" / f"settings-migration-{uuid.uuid4().hex}.sqlite3"
    async_url = _sqlite_url(database, async_driver=True)
    sync_url = _sqlite_url(database, async_driver=False)
    monkeypatch.setenv("PM_ENV", "test")
    monkeypatch.setenv("PM_DATABASE_URL", async_url)
    get_settings.cache_clear()
    config = _config()
    user_id = str(uuid.uuid4())
    home_id = str(uuid.uuid4())
    try:
        command.upgrade(config, "20260815_0011")
        engine = sa.create_engine(sync_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO users "
                        "(id, email, display_name, password_hash, enabled, created_at, updated_at) "
                        "VALUES (:id, '  Mixed-Case@Example.COM  ', 'Owner', 'hash', true, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"id": user_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO homes (id, name, timezone, created_at) "
                        "VALUES (:id, :name, 'America/Los_Angeles', CURRENT_TIMESTAMP)"
                    ),
                    {"id": home_id, "name": f"Home ({home_id})"},
                )
        finally:
            engine.dispose()

        command.upgrade(config, "head")
        engine = sa.create_engine(sync_url)
        try:
            with engine.begin() as connection:
                inspector = sa.inspect(connection)
                assert (
                    connection.scalar(
                        sa.text("SELECT email FROM users WHERE id = :id"), {"id": user_id}
                    )
                    == "mixed-case@example.com"
                )
                assert "preferences" in {
                    column["name"] for column in inspector.get_columns("users")
                }
                assert (
                    connection.scalar(
                        sa.text("SELECT name FROM homes WHERE id = :id"), {"id": home_id}
                    )
                    == "Home"
                )
                assert {
                    "location",
                    "notes",
                    "display_order",
                    "include_in_aggregate",
                    "show_on_dashboard",
                    "monitoring_enabled",
                }.issubset({column["name"] for column in inspector.get_columns("devices")})
                assert {"storage_bytes_total", "storage_bytes_free"}.issubset(
                    {column["name"] for column in inspector.get_columns("device_heartbeats")}
                )
                assert {
                    "plan_classification",
                    "holiday_treatment",
                    "billing_period_start",
                    "billing_period_end",
                    "billing_period_days",
                    "tier_threshold_basis",
                    "candidate_complete",
                }.issubset(
                    {
                        column["name"]
                        for column in inspector.get_columns("utility_bill_rate_extractions")
                    }
                )
                with pytest.raises(sa.exc.IntegrityError):
                    connection.execute(
                        sa.text(
                            "INSERT INTO users "
                            "(id, email, display_name, password_hash, enabled, "
                            "preferences, created_at, updated_at) "
                            "VALUES (:id, 'MIXED-CASE@EXAMPLE.COM', 'Duplicate', 'hash', true, "
                            "'{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                        ),
                        {"id": str(uuid.uuid4())},
                    )
        finally:
            engine.dispose()

        command.downgrade(config, "20260815_0011")
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection:
                inspector = sa.inspect(connection)
                assert "preferences" not in {
                    column["name"] for column in inspector.get_columns("users")
                }
                assert "location" not in {
                    column["name"] for column in inspector.get_columns("devices")
                }
                assert "candidate_complete" not in {
                    column["name"]
                    for column in inspector.get_columns("utility_bill_rate_extractions")
                }
                assert "storage_bytes_total" not in {
                    column["name"] for column in inspector.get_columns("device_heartbeats")
                }
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()
        database.unlink(missing_ok=True)


def test_settings_revision_refuses_case_insensitive_duplicate_emails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ROOT / ".test-runtime" / f"settings-email-preflight-{uuid.uuid4().hex}.sqlite3"
    async_url = _sqlite_url(database, async_driver=True)
    sync_url = _sqlite_url(database, async_driver=False)
    monkeypatch.setenv("PM_ENV", "test")
    monkeypatch.setenv("PM_DATABASE_URL", async_url)
    get_settings.cache_clear()
    config = _config()
    try:
        command.upgrade(config, "20260815_0011")
        engine = sa.create_engine(sync_url)
        try:
            with engine.begin() as connection:
                for email in ("duplicate@example.com", "DUPLICATE@EXAMPLE.COM"):
                    connection.execute(
                        sa.text(
                            "INSERT INTO users "
                            "(id, email, display_name, password_hash, enabled, "
                            "created_at, updated_at) VALUES "
                            "(:id, :email, 'User', 'hash', true, CURRENT_TIMESTAMP, "
                            "CURRENT_TIMESTAMP)"
                        ),
                        {"id": str(uuid.uuid4()), "email": email},
                    )
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="case-insensitive duplicate user email"):
            command.upgrade(config, "head")
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    "20260815_0011"
                )
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()
        database.unlink(missing_ok=True)


def test_sce_ota_revision_backfills_populated_legacy_deployment_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ROOT / ".test-runtime" / f"sce-ota-migration-{uuid.uuid4().hex}.sqlite3"
    async_url = _sqlite_url(database, async_driver=True)
    sync_url = _sqlite_url(database, async_driver=False)
    monkeypatch.setenv("PM_ENV", "test")
    monkeypatch.setenv("PM_DATABASE_URL", async_url)
    get_settings.cache_clear()
    config = _config()
    home_id = str(uuid.uuid4())
    device_id = str(uuid.uuid4())
    release_id = str(uuid.uuid4())
    deployment_id = str(uuid.uuid4())
    try:
        command.upgrade(config, "20260817_0014")
        engine = sa.create_engine(sync_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO homes (id, name, timezone, created_at) VALUES "
                        "(:id, 'Migration home', 'America/Los_Angeles', CURRENT_TIMESTAMP)"
                    ),
                    {"id": home_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO devices (id, home_id, friendly_name, protocol_id, "
                        "pzem_variant, ct_rating_a, measurement_scope, state, firmware_version, "
                        "contiguous_ack, maximum_sequence, reset_generation, created_at) VALUES "
                        "(:id, :home_id, 'Legacy OTA sensor', 'pm-protocol/1.0.0', "
                        "'pzem004t-v4-classic-candidate', 100, 'energy_only', 'online', "
                        "'0.1.0-rc.12', 0, 0, 0, CURRENT_TIMESTAMP)"
                    ),
                    {"id": device_id, "home_id": home_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO firmware_releases (id, semantic_version, build_number, "
                        "project_name, target_chip, board_profile, minimum_protocol, "
                        "minimum_config_version, minimum_boot_version, image_size, sha256, "
                        "image_path, release_notes, manifest_signature, candidate, created_at) "
                        "VALUES (:id, '0.1.0-rc.13', '13', 'power-monitor-sensor-headless', "
                        "'esp32s3', 'esp32-s3-reference/1', 'pm-protocol/1.0.0', 1, 1, 1024, "
                        ":digest, 'firmware.bin', 'legacy fixture', :signature, true, "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {"id": release_id, "digest": "a" * 64, "signature": "b" * 64},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO firmware_deployments (id, firmware_release_id, device_id, "
                        "state, progress_percent, evidence, created_at) VALUES "
                        "(:id, :release_id, :device_id, 'validating', 90, '{}', "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {
                        "id": deployment_id,
                        "release_id": release_id,
                        "device_id": device_id,
                    },
                )
        finally:
            engine.dispose()

        command.upgrade(config, "head")
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    sa.text(
                        "SELECT d.device_id, d.state, d.progress_percent, d.attempt, "
                        "d.batch_id, b.rollout, b.state, b.created_by_user_id "
                        "FROM firmware_deployments d JOIN firmware_deployment_batches b "
                        "ON b.id = d.batch_id WHERE d.id = :id"
                    ),
                    {"id": deployment_id},
                ).one()
                assert row[0:4] == (device_id, "validating", 90, 1)
                assert row[4] is not None
                assert row[5:] == ("legacy", "in_progress", None)
        finally:
            engine.dispose()

        command.downgrade(config, "20260817_0014")
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection:
                assert connection.execute(
                    sa.text(
                        "SELECT device_id, state, progress_percent FROM firmware_deployments "
                        "WHERE id = :id"
                    ),
                    {"id": deployment_id},
                ).one() == (device_id, "validating", 90)
                assert "firmware_deployment_batches" not in sa.inspect(connection).get_table_names()
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()
        database.unlink(missing_ok=True)


def test_service_branch_revision_designates_only_unambiguous_populated_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ROOT / ".test-runtime" / f"service-branch-migration-{uuid.uuid4().hex}.sqlite3"
    async_url = _sqlite_url(database, async_driver=True)
    sync_url = _sqlite_url(database, async_driver=False)
    monkeypatch.setenv("PM_ENV", "test")
    monkeypatch.setenv("PM_DATABASE_URL", async_url)
    get_settings.cache_clear()
    config = _config()
    home_ids = (str(uuid.uuid4()), str(uuid.uuid4()))
    account_ids = (str(uuid.uuid4()), str(uuid.uuid4()))
    circuit_ids = tuple(str(uuid.uuid4()) for _ in range(3))
    device_ids = tuple(str(uuid.uuid4()) for _ in range(6))
    try:
        command.upgrade(config, "20260817_0015")
        engine = sa.create_engine(sync_url)
        try:
            with engine.begin() as connection:
                for index, home_id in enumerate(home_ids):
                    connection.execute(
                        sa.text(
                            "INSERT INTO homes (id, name, timezone, created_at) VALUES "
                            "(:id, :name, 'America/Los_Angeles', CURRENT_TIMESTAMP)"
                        ),
                        {"id": home_id, "name": f"Migration home {index}"},
                    )
                    connection.execute(
                        sa.text(
                            "INSERT INTO utility_accounts "
                            "(id, home_id, utility_name, timezone, billing_day, cost_scope) VALUES "
                            "(:id, :home_id, 'SCE', 'America/Los_Angeles', 22, 'energy_only')"
                        ),
                        {"id": account_ids[index], "home_id": home_id},
                    )
                for index, circuit_id in enumerate(circuit_ids):
                    connection.execute(
                        sa.text(
                            "INSERT INTO circuits "
                            "(id, home_id, name, aggregate_mode) VALUES "
                            "(:id, :home_id, :name, 'verified_sum')"
                        ),
                        {
                            "id": circuit_id,
                            "home_id": home_ids[0] if index == 0 else home_ids[1],
                            "name": f"Legacy aggregate {index}",
                        },
                    )
                for index, device_id in enumerate(device_ids):
                    circuit_index = 0 if index < 2 else 1 if index < 4 else 2
                    connection.execute(
                        sa.text(
                            "INSERT INTO devices "
                            "(id, home_id, circuit_id, friendly_name, protocol_id, pzem_variant, "
                            "ct_rating_a, measurement_scope, state, contiguous_ack, "
                            "maximum_sequence, reset_generation, include_in_aggregate, "
                            "created_at) VALUES "
                            "(:id, :home_id, :circuit_id, :name, 'pm-protocol/1.0.0', "
                            "'pzem004t-v4-classic-candidate', 100, 'energy_only', 'online', "
                            "0, 0, 0, true, CURRENT_TIMESTAMP)"
                        ),
                        {
                            "id": device_id,
                            "home_id": home_ids[0] if index < 2 else home_ids[1],
                            "circuit_id": circuit_ids[circuit_index],
                            "name": f"Sensor {index}",
                        },
                    )
        finally:
            engine.dispose()

        command.upgrade(config, "head")
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection:
                designated = connection.execute(
                    sa.text(
                        "SELECT id, name, purpose, is_home_total, aggregate_mode, "
                        "non_overlapping_confirmed, is_billing_source FROM circuits "
                        "WHERE home_id = :home_id"
                    ),
                    {"home_id": home_ids[0]},
                ).one()
                assert designated == (
                    circuit_ids[0],
                    "Main service",
                    "whole_home_total",
                    True,
                    "verified_sum",
                    True,
                    True,
                )
                ambiguous = connection.execute(
                    sa.text(
                        "SELECT name, is_home_total, non_overlapping_confirmed FROM circuits "
                        "WHERE home_id = :home_id ORDER BY name"
                    ),
                    {"home_id": home_ids[1]},
                ).all()
                assert [tuple(row) for row in ambiguous] == [
                    ("Legacy aggregate 1", False, True),
                    ("Legacy aggregate 2", False, True),
                ]
                device_membership = connection.execute(
                    sa.text("SELECT id, circuit_id, measurement_scope FROM devices ORDER BY id")
                ).all()
                assert [tuple(row) for row in device_membership] == sorted(
                    (
                        device_ids[index],
                        circuit_ids[0 if index < 2 else 1 if index < 4 else 2],
                        "full_account" if index < 2 else "energy_only",
                    )
                    for index in range(6)
                )
                account_scopes = connection.execute(
                    sa.text("SELECT id, cost_scope FROM utility_accounts ORDER BY id")
                ).all()
                assert [tuple(row) for row in account_scopes] == sorted(
                    ((account_ids[0], "full_account"), (account_ids[1], "energy_only"))
                )
                circuit_columns = {
                    item["name"] for item in sa.inspect(connection).get_columns("circuits")
                }
                assert {
                    "description",
                    "purpose",
                    "is_home_total",
                    "non_overlapping_confirmed",
                    "created_at",
                    "updated_at",
                    "is_billing_source",
                }.issubset(circuit_columns)
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT count(*) FROM home_telemetry_settings WHERE home_id = :home_id"
                        ),
                        {"home_id": home_ids[0]},
                    )
                    == 1
                )
        finally:
            engine.dispose()

        command.downgrade(config, "20260817_0015")
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection:
                circuit_columns = {
                    item["name"] for item in sa.inspect(connection).get_columns("circuits")
                }
                assert "is_home_total" not in circuit_columns
                assert connection.scalar(sa.text("SELECT count(*) FROM devices")) == 6
                assert connection.scalar(sa.text("SELECT count(*) FROM circuits")) == 3
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()
        database.unlink(missing_ok=True)


def test_stateless_revision_refuses_lossy_downgrade_after_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ROOT / ".test-runtime" / f"stateless-downgrade-{uuid.uuid4().hex}.sqlite3"
    async_url = _sqlite_url(database, async_driver=True)
    sync_url = _sqlite_url(database, async_driver=False)
    monkeypatch.setenv("PM_ENV", "test")
    monkeypatch.setenv("PM_DATABASE_URL", async_url)
    get_settings.cache_clear()
    config = _config()
    home_id = str(uuid.uuid4())
    device_id = str(uuid.uuid4())
    sample_id = str(uuid.uuid4())
    try:
        command.upgrade(config, "20260817_0016")
        engine = sa.create_engine(sync_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO homes (id, name, timezone, created_at) VALUES "
                        "(:id, 'Stateless migration home', 'America/Los_Angeles', "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {"id": home_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO devices "
                        "(id, home_id, friendly_name, protocol_id, pzem_variant, ct_rating_a, "
                        "measurement_scope, state, contiguous_ack, maximum_sequence, "
                        "reset_generation, include_in_aggregate, created_at) VALUES "
                        "(:id, :home_id, 'Stateless sensor', 'pm-protocol/1.0.0', "
                        "'pzem004t-v4-classic-candidate', 100, 'energy_only', 'online', "
                        "0, 0, 0, false, CURRENT_TIMESTAMP)"
                    ),
                    {"id": device_id, "home_id": home_id},
                )
        finally:
            engine.dispose()

        command.upgrade(config, "20260818_0017")
        engine = sa.create_engine(sync_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO stateless_telemetry_samples "
                        "(id, device_id, boot_id, sample_sequence, telemetry_protocol, "
                        "sampled_at, received_at, effective_at, sensor_time_trusted, uptime_ms, "
                        "voltage_v, current_a, active_power_w, frequency_hz, power_factor, "
                        "pzem_energy_wh, pzem_status, firmware_version, firmware_build_id, "
                        "time_status, wifi_rssi, payload_sha256) VALUES "
                        "(:id, :device_id, :boot_id, 1, 'pm-telemetry/2.0.0', "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, true, 1000, "
                        "240, 1, 200, 60, 0.9, 1000, 'ok', '0.1.0-rc.17', 'elf-sha', "
                        "'trusted', -50, :digest)"
                    ),
                    {
                        "id": sample_id,
                        "device_id": device_id,
                        "boot_id": str(uuid.uuid4()),
                        "digest": "f" * 64,
                    },
                )
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="refusing downgrade"):
            command.downgrade(config, "20260817_0016")
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    "20260818_0017"
                )
                assert (
                    connection.scalar(sa.text("SELECT count(*) FROM stateless_telemetry_samples"))
                    == 1
                )
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()
        database.unlink(missing_ok=True)


def test_catalog_firmware_lifecycle_revision_backfills_without_deleting_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ROOT / ".test-runtime" / f"catalog-lifecycle-{uuid.uuid4().hex}.sqlite3"
    async_url = _sqlite_url(database, async_driver=True)
    sync_url = _sqlite_url(database, async_driver=False)
    monkeypatch.setenv("PM_ENV", "test")
    monkeypatch.setenv("PM_DATABASE_URL", async_url)
    get_settings.cache_clear()
    config = _config()
    available_id = str(uuid.uuid4())
    rollback_id = str(uuid.uuid4())
    removed_id = str(uuid.uuid4())
    try:
        command.upgrade(config, "20260818_0017")
        engine = sa.create_engine(sync_url)
        try:
            with engine.begin() as connection:
                for release_id, version, image_path, digest, created_at in (
                    (
                        available_id,
                        "0.1.0-rc.200",
                        "/data/firmware/available.bin",
                        "a" * 64,
                        "2026-08-20 12:00:00",
                    ),
                    (
                        rollback_id,
                        "0.1.0-rc.199",
                        "/data/firmware/rollback.bin",
                        "c" * 64,
                        "2026-08-19 12:00:00",
                    ),
                    (removed_id, "0.1.0-rc.198", "", "b" * 64, "2026-08-18 12:00:00"),
                ):
                    connection.execute(
                        sa.text(
                            "INSERT INTO firmware_releases "
                            "(id, semantic_version, build_number, project_name, target_chip, "
                            "board_profile, minimum_boot_version, minimum_protocol, "
                            "minimum_config_version, image_size, sha256, image_path, "
                            "release_notes, manifest_signature, candidate, created_at) VALUES "
                            "(:id, :version, '200', 'power-monitor-sensor-headless', "
                            "'esp32s3', 'reference', 1, 'pm-protocol/1.0.0', 1, 1024, "
                            ":digest, :image_path, 'rate-free fixture', 'signature', true, "
                            ":created_at)"
                        ),
                        {
                            "id": release_id,
                            "version": version,
                            "digest": digest,
                            "image_path": image_path,
                            "created_at": created_at,
                        },
                    )
        finally:
            engine.dispose()

        command.upgrade(config, "head")
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT COUNT(*) FROM firmware_releases")) == 3
                rows = {
                    row.id: row
                    for row in connection.execute(
                        sa.text(
                            "SELECT id, lifecycle_state, rollback_pinned, "
                            "artifact_deleted_at, deleted_at "
                            "FROM firmware_releases"
                        )
                    )
                }
                assert rows[available_id].lifecycle_state == "current"
                assert not bool(rows[available_id].rollback_pinned)
                assert rows[available_id].artifact_deleted_at is None
                assert rows[rollback_id].lifecycle_state == "available"
                assert bool(rows[rollback_id].rollback_pinned)
                assert rows[removed_id].lifecycle_state == "deleted"
                assert rows[removed_id].artifact_deleted_at is not None
                assert rows[removed_id].deleted_at is not None
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT deployment_retention_days FROM firmware_lifecycle_settings "
                            "WHERE id = 'global'"
                        )
                    )
                    == 365
                )
                assert "sce_catalog_entries" in sa.inspect(connection).get_table_names()
                assert "utility_account_tier_thresholds" in sa.inspect(connection).get_table_names()
                assert {
                    "utility_account_id",
                    "rate_plan_id",
                    "season",
                    "kwh_per_day",
                    "source_allowance_kwh",
                    "source_billing_days",
                    "effective_start",
                    "effective_end",
                    "source_artifact_sha256",
                } <= {
                    column["name"]
                    for column in sa.inspect(connection).get_columns(
                        "utility_account_tier_thresholds"
                    )
                }
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT COUNT(*) FROM audit_events WHERE event_code IN "
                            "('FIRMWARE_CURRENT_RELEASE_MIGRATED', "
                            "'FIRMWARE_ROLLBACK_RELEASE_MIGRATED')"
                        )
                    )
                    == 2
                )
                assert connection.scalar(sa.text("SELECT COUNT(*) FROM rate_plan_versions")) == 0
                assert connection.scalar(sa.text("SELECT COUNT(*) FROM normalized_intervals")) == 0
                with pytest.raises(sa.exc.IntegrityError):
                    connection.execute(
                        sa.text(
                            "UPDATE firmware_releases SET lifecycle_state = 'current' "
                            "WHERE id = :release_id"
                        ),
                        {"release_id": rollback_id},
                    )
        finally:
            engine.dispose()

        command.downgrade(config, "20260818_0017")
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT COUNT(*) FROM firmware_releases")) == 3
                assert "lifecycle_state" not in {
                    column["name"]
                    for column in sa.inspect(connection).get_columns("firmware_releases")
                }
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()
        database.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("mutation_sql", "parameters", "error_fragment"),
    (
        (
            "UPDATE firmware_lifecycle_settings SET deployment_retention_days = 90 "
            "WHERE id = 'global'",
            {},
            "firmware lifecycle settings",
        ),
        (
            "INSERT INTO audit_events "
            "(id, event_code, target_type, target_id, correlation_id, details, created_at) "
            "VALUES (:id, 'FIRMWARE_DEPLOYMENT_RETENTION_UPDATED', "
            "'firmware_lifecycle_settings', 'global', 'restored-default-fixture', '{}', "
            "CURRENT_TIMESTAMP)",
            {"id": "restored-default-audit"},
            "post-migration lifecycle or rate audit",
        ),
        (
            "INSERT INTO rate_plans "
            "(id, name, utility_name, rate_class, official_schedule_code, created_at) "
            "VALUES (:id, 'Published evidence', 'SCE', 'residential', 'D', "
            "CURRENT_TIMESTAMP)",
            {"id": "rate-plan-evidence"},
            "official rate-plan metadata",
        ),
        (
            "INSERT INTO firmware_releases "
            "(id, semantic_version, build_number, project_name, target_chip, board_profile, "
            "minimum_boot_version, minimum_protocol, minimum_config_version, image_size, "
            "sha256, firmware_build_id, image_path, release_notes, manifest_signature, "
            "candidate, created_at, updated_at) VALUES "
            "(:id, '0.1.0-rc.222', '222', 'power-monitor-sensor-headless', 'esp32s3', "
            "'reference', 1, 'pm-protocol/1.0.0', 1, 1024, :sha256, :build_id, "
            "'/data/firmware/rc222.bin', 'fixture', 'signature', true, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            {
                "id": "firmware-build-evidence",
                "sha256": "b" * 64,
                "build_id": "a" * 64,
            },
            "exact firmware build identity",
        ),
    ),
)
def test_catalog_lifecycle_revision_refuses_lossy_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    mutation_sql: str,
    parameters: dict[str, object],
    error_fragment: str,
) -> None:
    database = ROOT / ".test-runtime" / f"catalog-lossy-{uuid.uuid4().hex}.sqlite3"
    async_url = _sqlite_url(database, async_driver=True)
    sync_url = _sqlite_url(database, async_driver=False)
    monkeypatch.setenv("PM_ENV", "test")
    monkeypatch.setenv("PM_DATABASE_URL", async_url)
    get_settings.cache_clear()
    config = _config()
    try:
        command.upgrade(config, "head")
        engine = sa.create_engine(sync_url)
        try:
            with engine.begin() as connection:
                connection.execute(sa.text(mutation_sql), parameters)
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match=error_fragment):
            command.downgrade(config, "20260818_0017")
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    "20260820_0018"
                )
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()
        database.unlink(missing_ok=True)


def test_catalog_lifecycle_revision_rejects_nonhex_firmware_build_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ROOT / ".test-runtime" / f"catalog-build-check-{uuid.uuid4().hex}.sqlite3"
    async_url = _sqlite_url(database, async_driver=True)
    sync_url = _sqlite_url(database, async_driver=False)
    monkeypatch.setenv("PM_ENV", "test")
    monkeypatch.setenv("PM_DATABASE_URL", async_url)
    get_settings.cache_clear()
    config = _config()
    try:
        command.upgrade(config, "head")
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as connection, pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    sa.text(
                        "INSERT INTO firmware_releases "
                        "(id, semantic_version, build_number, project_name, target_chip, "
                        "board_profile, minimum_boot_version, minimum_protocol, "
                        "minimum_config_version, image_size, sha256, firmware_build_id, "
                        "image_path, release_notes, manifest_signature, candidate, "
                        "created_at, updated_at) VALUES "
                        "('invalid-build', '0.1.0-rc.223', '223', "
                        "'power-monitor-sensor-headless', 'esp32s3', 'reference', 1, "
                        "'pm-protocol/1.0.0', 1, 1024, :sha256, :build_id, "
                        "'/data/firmware/rc223.bin', 'fixture', 'signature', true, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"sha256": "c" * 64, "build_id": "g" * 64},
                )
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()
        database.unlink(missing_ok=True)


def test_catalog_lifecycle_revision_refuses_account_tier_threshold_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ROOT / ".test-runtime" / f"account-tier-lossy-{uuid.uuid4().hex}.sqlite3"
    async_url = _sqlite_url(database, async_driver=True)
    sync_url = _sqlite_url(database, async_driver=False)
    monkeypatch.setenv("PM_ENV", "test")
    monkeypatch.setenv("PM_DATABASE_URL", async_url)
    get_settings.cache_clear()
    config = _config()
    try:
        command.upgrade(config, "head")
        engine = sa.create_engine(sync_url)
        try:
            with Session(engine) as session:
                user = User(
                    email="tier-migration@example.com",
                    display_name="Tier migration fixture",
                    password_hash="not-a-real-credential",
                )
                home = Home(name="Tier migration home")
                session.add_all((user, home))
                session.flush()
                account = UtilityAccount(home_id=home.id)
                plan = RatePlan(
                    name="DOMESTIC migration fixture",
                    utility_name="Southern California Edison",
                    rate_class="residential_tiered",
                )
                session.add_all((account, plan))
                session.flush()
                session.add(
                    UtilityAccountTierThreshold(
                        utility_account_id=account.id,
                        rate_plan_id=plan.id,
                        season="summer",
                        kwh_per_day=Decimal("19.3"),
                        source_allowance_kwh=Decimal("579"),
                        source_billing_days=30,
                        tier1_boundary_inclusive=True,
                        source_label="migration fixture evidence",
                        source_kind="candidate_review",
                        source_artifact_sha256="a" * 64,
                        effective_start=datetime(2026, 6, 1, 7, tzinfo=UTC),
                        created_by_user_id=user.id,
                    )
                )
                session.commit()
        finally:
            engine.dispose()
        with pytest.raises(RuntimeError, match="account tier-threshold evidence"):
            command.downgrade(config, "20260818_0017")
    finally:
        get_settings.cache_clear()
        database.unlink(missing_ok=True)
