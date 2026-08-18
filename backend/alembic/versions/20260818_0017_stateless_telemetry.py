"""Add independently accepted stateless telemetry and server History settings.

Revision ID: 20260818_0017
Revises: 20260817_0016
Create Date: 2026-08-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260818_0017"
down_revision = "20260817_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    legacy_without_raw = connection.scalar(
        sa.text("SELECT COUNT(*) FROM normalized_intervals WHERE raw_reading_id IS NULL")
    )
    if int(legacy_without_raw or 0) != 0:
        raise RuntimeError(
            "revision 0017 requires every legacy normalized interval to retain raw evidence"
        )
    with op.batch_alter_table("circuits") as batch:
        batch.add_column(
            sa.Column(
                "is_billing_source",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    op.execute("UPDATE circuits SET is_billing_source = true WHERE is_home_total = true")
    with op.batch_alter_table("circuits") as batch:
        batch.create_check_constraint(
            "ck_circuits_billing_source_home_total",
            "is_billing_source = false OR is_home_total = true",
        )
    op.create_index(
        "uq_circuits_one_billing_source",
        "circuits",
        ["home_id"],
        unique=True,
        postgresql_where=sa.text("is_billing_source = true"),
        sqlite_where=sa.text("is_billing_source = 1"),
    )

    with op.batch_alter_table("normalized_intervals") as batch:
        batch.alter_column(
            "raw_reading_id",
            existing_type=sa.String(36),
            nullable=True,
        )
        batch.alter_column(
            "energy_mwh",
            existing_type=sa.BigInteger(),
            nullable=True,
        )
        batch.add_column(
            sa.Column(
                "source_kind",
                sa.String(24),
                nullable=False,
                server_default="legacy_durable",
            )
        )
        batch.add_column(sa.Column("minimum_power_mw", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("maximum_power_mw", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("ending_voltage_mv", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("ending_current_ma", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("average_frequency_mhz", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("average_power_factor_milli", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("received_sample_count", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column("expected_sample_count", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column("gap_status", sa.String(24), nullable=False, server_default="complete")
        )
        batch.add_column(
            sa.Column("finalized", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.add_column(sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            "ck_normalized_intervals_source_kind",
            "source_kind IN ('legacy_durable','stateless_v2')",
        )
        batch.create_check_constraint(
            "ck_normalized_intervals_source_identity",
            "(source_kind = 'legacy_durable' AND raw_reading_id IS NOT NULL) OR "
            "(source_kind = 'stateless_v2' AND raw_reading_id IS NULL)",
        )
        batch.create_check_constraint(
            "ck_normalized_intervals_server_sample_count",
            "received_sample_count >= 0 AND expected_sample_count > 0 "
            "AND received_sample_count <= expected_sample_count",
        )
        batch.create_check_constraint(
            "ck_normalized_intervals_gap_status",
            "gap_status IN ('complete','partial','connection_gap')",
        )
    op.create_index(
        "uq_normalized_intervals_stateless_bucket",
        "normalized_intervals",
        ["device_id", "start_utc"],
        unique=True,
        postgresql_where=sa.text("source_kind = 'stateless_v2'"),
        sqlite_where=sa.text("source_kind = 'stateless_v2'"),
    )

    op.create_table(
        "home_telemetry_settings",
        sa.Column("home_id", sa.String(36), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("telemetry_interval_seconds", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("history_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("retention_days", sa.Integer(), nullable=True, server_default="365"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("updated_by_user_id", sa.String(36), nullable=True),
        sa.CheckConstraint(
            "config_version > 0", name="ck_home_telemetry_settings_config_version_positive"
        ),
        sa.CheckConstraint(
            "telemetry_interval_seconds IN (2,5,10,15,30,60)",
            name="ck_home_telemetry_settings_telemetry_interval",
        ),
        sa.CheckConstraint(
            "history_interval_seconds IN (15,30,60,300,900)",
            name="ck_home_telemetry_settings_history_interval",
        ),
        sa.CheckConstraint(
            "retention_days IS NULL OR retention_days IN (30,90,180,365)",
            name="ck_home_telemetry_settings_retention_days",
        ),
        sa.ForeignKeyConstraint(["home_id"], ["homes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("home_id"),
    )
    op.execute(
        "INSERT INTO home_telemetry_settings "
        "(home_id, config_version, telemetry_interval_seconds, history_interval_seconds, "
        "retention_days, updated_at) "
        "SELECT id, 1, 5, 60, 365, CURRENT_TIMESTAMP FROM homes"
    )

    op.create_table(
        "stateless_telemetry_samples",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("boot_id", sa.String(36), nullable=False),
        sa.Column("sample_sequence", sa.BigInteger(), nullable=False),
        sa.Column("telemetry_protocol", sa.String(40), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sensor_time_trusted", sa.Boolean(), nullable=False),
        sa.Column("uptime_ms", sa.BigInteger(), nullable=False),
        sa.Column("voltage_v", sa.Numeric(12, 3), nullable=True),
        sa.Column("current_a", sa.Numeric(12, 4), nullable=True),
        sa.Column("active_power_w", sa.Numeric(14, 3), nullable=True),
        sa.Column("frequency_hz", sa.Numeric(8, 3), nullable=True),
        sa.Column("power_factor", sa.Numeric(6, 4), nullable=True),
        sa.Column("pzem_energy_wh", sa.BigInteger(), nullable=True),
        sa.Column("pzem_status", sa.String(40), nullable=False),
        sa.Column("firmware_version", sa.String(80), nullable=False),
        sa.Column("firmware_build_id", sa.String(128), nullable=False),
        sa.Column("time_status", sa.String(20), nullable=False),
        sa.Column("wifi_rssi", sa.Integer(), nullable=True),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "sample_sequence > 0", name="ck_stateless_telemetry_samples_sample_sequence_positive"
        ),
        sa.CheckConstraint(
            "uptime_ms >= 0", name="ck_stateless_telemetry_samples_uptime_nonnegative"
        ),
        sa.CheckConstraint(
            "pzem_energy_wh IS NULL OR pzem_energy_wh >= 0",
            name="ck_stateless_telemetry_samples_energy_nonnegative",
        ),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "device_id",
            "boot_id",
            "sample_sequence",
            name="uq_stateless_telemetry_samples_device_id",
        ),
    )
    op.create_index(
        "ix_stateless_telemetry_samples_device_id",
        "stateless_telemetry_samples",
        ["device_id"],
    )
    op.create_index(
        "ix_stateless_telemetry_samples_received_at",
        "stateless_telemetry_samples",
        ["received_at"],
    )
    op.create_index(
        "ix_stateless_telemetry_samples_effective_at",
        "stateless_telemetry_samples",
        ["effective_at"],
    )

    op.create_table(
        "device_telemetry_states",
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("latest_sample_id", sa.String(36), nullable=False),
        sa.Column("latest_server_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest_sensor_sampled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sensor_time_trusted", sa.Boolean(), nullable=False),
        sa.Column("timestamp_source", sa.String(12), nullable=False),
        sa.Column("telemetry_protocol", sa.String(40), nullable=False),
        sa.Column("firmware_version", sa.String(80), nullable=False),
        sa.Column("firmware_build_id", sa.String(128), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "timestamp_source IN ('sensor','server')",
            name="ck_device_telemetry_states_timestamp_source",
        ),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["latest_sample_id"], ["stateless_telemetry_samples.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("device_id"),
        sa.UniqueConstraint("latest_sample_id"),
    )
    op.create_index(
        "ix_device_telemetry_states_latest_server_received_at",
        "device_telemetry_states",
        ["latest_server_received_at"],
    )

    op.create_table(
        "telemetry_cutovers",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("old_protocol", sa.String(40), nullable=False),
        sa.Column("new_protocol", sa.String(40), nullable=False),
        sa.Column("cutover_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_sample_id", sa.String(36), nullable=False),
        sa.Column("firmware_version", sa.String(80), nullable=False),
        sa.Column("firmware_build_id", sa.String(128), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["first_sample_id"], ["stateless_telemetry_samples.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id"),
        sa.UniqueConstraint("first_sample_id"),
    )
    op.create_table(
        "telemetry_energy_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("home_id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("sample_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("gap_start_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gap_end_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prior_energy_wh", sa.BigInteger(), nullable=True),
        sa.Column("current_energy_wh", sa.BigInteger(), nullable=True),
        sa.Column("recovered_energy_mwh", sa.BigInteger(), nullable=True),
        sa.Column("billing_status", sa.String(32), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "event_type IN ('connection_gap_recovered','connection_gap_unresolved',"
            "'counter_reset')",
            name="ck_telemetry_energy_events_event_type",
        ),
        sa.CheckConstraint(
            "billing_status IN ('included','unresolved','excluded')",
            name="ck_telemetry_energy_events_billing_status",
        ),
        sa.CheckConstraint(
            "recovered_energy_mwh IS NULL OR recovered_energy_mwh >= 0",
            name="ck_telemetry_energy_events_recovered_energy_nonnegative",
        ),
        sa.ForeignKeyConstraint(["home_id"], ["homes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["sample_id"], ["stateless_telemetry_samples.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sample_id", "event_type"),
    )
    op.create_index("ix_telemetry_energy_events_home_id", "telemetry_energy_events", ["home_id"])
    op.create_index(
        "ix_telemetry_energy_events_device_id", "telemetry_energy_events", ["device_id"]
    )
    op.create_index(
        "ix_telemetry_energy_events_sample_id", "telemetry_energy_events", ["sample_id"]
    )
    op.create_index(
        "ix_telemetry_energy_events_gap_start_utc", "telemetry_energy_events", ["gap_start_utc"]
    )
    op.create_index(
        "ix_telemetry_energy_events_gap_end_utc", "telemetry_energy_events", ["gap_end_utc"]
    )

    op.create_table(
        "billing_cycle_adjustments",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("utility_account_id", sa.String(36), nullable=False),
        sa.Column("cycle_start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("energy_mwh", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(60), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "energy_mwh >= 0", name="ck_billing_cycle_adjustments_energy_nonnegative"
        ),
        sa.CheckConstraint(
            "reason IN ('verified_cycle_to_date_seed','gap_allocation')",
            name="ck_billing_cycle_adjustments_reason",
        ),
        sa.ForeignKeyConstraint(
            ["utility_account_id"], ["utility_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "utility_account_id",
            "cycle_start_utc",
            "reason",
            name="uq_billing_cycle_adjustment_once",
        ),
    )
    op.create_index(
        "ix_billing_cycle_adjustments_utility_account_id",
        "billing_cycle_adjustments",
        ["utility_account_id"],
    )
    op.create_index(
        "ix_billing_cycle_adjustments_cycle_start_utc",
        "billing_cycle_adjustments",
        ["cycle_start_utc"],
    )
    if connection.dialect.name == "postgresql":
        for table in (
            "stateless_telemetry_samples",
            "telemetry_cutovers",
            "telemetry_energy_events",
            "billing_cycle_adjustments",
        ):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER {table}_immutable "
                    f"BEFORE UPDATE OR DELETE ON public.{table} "
                    "FOR EACH ROW EXECUTE FUNCTION public.pm_reject_immutable_change()"
                )
            )


def downgrade() -> None:
    connection = op.get_bind()
    accepted_samples = connection.scalar(
        sa.text("SELECT COUNT(*) FROM stateless_telemetry_samples")
    )
    stateless_buckets = connection.scalar(
        sa.text("SELECT COUNT(*) FROM normalized_intervals WHERE source_kind = 'stateless_v2'")
    )
    cutovers = connection.scalar(sa.text("SELECT COUNT(*) FROM telemetry_cutovers"))
    device_states = connection.scalar(sa.text("SELECT COUNT(*) FROM device_telemetry_states"))
    energy_events = connection.scalar(sa.text("SELECT COUNT(*) FROM telemetry_energy_events"))
    billing_adjustments = connection.scalar(
        sa.text("SELECT COUNT(*) FROM billing_cycle_adjustments")
    )
    changed_settings = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM home_telemetry_settings "
            "WHERE config_version <> 1 OR telemetry_interval_seconds <> 5 "
            "OR history_interval_seconds <> 60 OR retention_days <> 365 "
            "OR retention_days IS NULL OR updated_by_user_id IS NOT NULL"
        )
    )
    if any(
        int(value or 0) != 0
        for value in (
            accepted_samples,
            stateless_buckets,
            cutovers,
            device_states,
            energy_events,
            billing_adjustments,
            changed_settings,
        )
    ):
        raise RuntimeError(
            "refusing downgrade: accepted stateless telemetry, cutover History, billing "
            "adjustments, or changed telemetry settings must be preserved"
        )
    op.drop_table("billing_cycle_adjustments")
    op.drop_table("telemetry_energy_events")
    op.drop_table("telemetry_cutovers")
    op.drop_table("device_telemetry_states")
    op.drop_table("stateless_telemetry_samples")
    op.drop_table("home_telemetry_settings")

    op.drop_index("uq_normalized_intervals_stateless_bucket", table_name="normalized_intervals")
    with op.batch_alter_table("normalized_intervals") as batch:
        batch.drop_constraint("ck_normalized_intervals_gap_status", type_="check")
        batch.drop_constraint("ck_normalized_intervals_server_sample_count", type_="check")
        batch.drop_constraint("ck_normalized_intervals_source_identity", type_="check")
        batch.drop_constraint("ck_normalized_intervals_source_kind", type_="check")
        batch.drop_column("last_received_at")
        batch.drop_column("finalized")
        batch.drop_column("gap_status")
        batch.drop_column("expected_sample_count")
        batch.drop_column("received_sample_count")
        batch.drop_column("average_power_factor_milli")
        batch.drop_column("average_frequency_mhz")
        batch.drop_column("ending_current_ma")
        batch.drop_column("ending_voltage_mv")
        batch.drop_column("maximum_power_mw")
        batch.drop_column("minimum_power_mw")
        batch.drop_column("source_kind")
        batch.alter_column(
            "raw_reading_id",
            existing_type=sa.String(36),
            nullable=False,
        )
        batch.alter_column(
            "energy_mwh",
            existing_type=sa.BigInteger(),
            nullable=False,
        )

    op.drop_index("uq_circuits_one_billing_source", table_name="circuits")
    with op.batch_alter_table("circuits") as batch:
        batch.drop_constraint("ck_circuits_billing_source_home_total", type_="check")
        batch.drop_column("is_billing_source")
