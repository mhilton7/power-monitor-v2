from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from backend.app.config import get_settings
from backend.app.models import Base

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
                tables = set(sa.inspect(connection).get_table_names())
                assert future_table.name not in tables
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
