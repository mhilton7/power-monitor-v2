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
