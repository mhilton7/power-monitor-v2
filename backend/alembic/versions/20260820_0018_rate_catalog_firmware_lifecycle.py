"""Add the SCE catalog and explicit firmware lifecycle state.

Revision ID: 20260820_0018
Revises: 20260818_0017
Create Date: 2026-08-20

The migration is additive.  Accepted telemetry, normalized History, published
rate versions, OTA attempts, and audit evidence are not rewritten or removed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "20260820_0018"
down_revision = "20260818_0017"
branch_labels = None
depends_on = None


RELEASE_STATES = "'draft','validating','available','current','archived','rejected','deleted'"
CATALOG_STATES = "'parsed','requires_parser','excluded'"
PLAN_TYPES = (
    "'flat','tiered','seasonal_tiered','time_of_use','seasonal_time_of_use',"
    "'time_of_use_with_baseline_credit','critical_peak_pricing','dynamic_hourly','unknown'"
)


def _lowercase_hex_64_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"{column_name} IS NULL OR (length({column_name}) = 64 AND length({remainder}) = 0)"


def upgrade() -> None:
    with op.batch_alter_table("firmware_releases") as batch:
        batch.add_column(
            sa.Column(
                "lifecycle_state",
                sa.String(24),
                nullable=False,
                server_default="available",
            )
        )
        batch.add_column(
            sa.Column("rollback_pinned", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column(
                "archived_by_user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column(
                "deleted_by_user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column("artifact_deleted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("firmware_build_id", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
        batch.create_check_constraint(
            "ck_firmware_releases_lifecycle_state",
            f"lifecycle_state IN ({RELEASE_STATES})",
        )
        batch.create_check_constraint(
            "ck_firmware_releases_lifecycle_evidence",
            "(lifecycle_state <> 'archived' OR archived_at IS NOT NULL) AND "
            "(lifecycle_state <> 'deleted' OR deleted_at IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_firmware_releases_firmware_build_id",
            _lowercase_hex_64_check("firmware_build_id"),
        )
    op.execute(
        "UPDATE firmware_releases SET lifecycle_state = 'deleted', "
        "artifact_deleted_at = created_at, deleted_at = created_at "
        "WHERE image_path = ''"
    )
    op.execute(
        "UPDATE firmware_releases SET lifecycle_state = 'current' WHERE id = ("
        "SELECT id FROM firmware_releases WHERE image_path <> '' "
        "ORDER BY created_at DESC, id DESC LIMIT 1)"
    )
    op.execute(
        "UPDATE firmware_releases SET rollback_pinned = true WHERE id = ("
        "SELECT id FROM firmware_releases WHERE image_path <> '' "
        "AND lifecycle_state <> 'current' ORDER BY created_at DESC, id DESC LIMIT 1)"
    )
    connection = op.get_bind()
    current_release_id = connection.scalar(
        sa.text("SELECT id FROM firmware_releases WHERE lifecycle_state = 'current' LIMIT 1")
    )
    rollback_release_id = connection.scalar(
        sa.text("SELECT id FROM firmware_releases WHERE rollback_pinned = true LIMIT 1")
    )
    audit_events = sa.table(
        "audit_events",
        sa.column("id", sa.String()),
        sa.column("actor_user_id", sa.String()),
        sa.column("event_code", sa.String()),
        sa.column("target_type", sa.String()),
        sa.column("target_id", sa.String()),
        sa.column("correlation_id", sa.String()),
        sa.column("details", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    # Second precision matches SQLite CURRENT_TIMESTAMP and keeps the audit
    # boundary comparable across SQLite and PostgreSQL.
    inferred_at = datetime.now(UTC).replace(microsecond=0)
    inferred_events: list[dict[str, object]] = [
        {
            "id": str(uuid.uuid4()),
            "actor_user_id": None,
            "event_code": "SCHEMA_0018_MIGRATED",
            "target_type": "schema_revision",
            "target_id": revision,
            "correlation_id": "alembic-20260820-0018",
            "details": {"automated_migration": True},
            "created_at": inferred_at,
        }
    ]
    if current_release_id is not None:
        inferred_events.append(
            {
                "id": str(uuid.uuid4()),
                "actor_user_id": None,
                "event_code": "FIRMWARE_CURRENT_RELEASE_MIGRATED",
                "target_type": "firmware_release",
                "target_id": str(current_release_id),
                "correlation_id": "alembic-20260820-0018",
                "details": {
                    "selection": "newest release with a retained artifact by created_at then id",
                    "automated_migration": True,
                },
                "created_at": inferred_at,
            }
        )
    if rollback_release_id is not None:
        inferred_events.append(
            {
                "id": str(uuid.uuid4()),
                "actor_user_id": None,
                "event_code": "FIRMWARE_ROLLBACK_RELEASE_MIGRATED",
                "target_type": "firmware_release",
                "target_id": str(rollback_release_id),
                "correlation_id": "alembic-20260820-0018",
                "details": {
                    "selection": "next-newest retained artifact after the current release",
                    "automated_migration": True,
                },
                "created_at": inferred_at,
            }
        )
    if inferred_events:
        connection.execute(sa.insert(audit_events), inferred_events)
    op.create_index(
        "ix_firmware_releases_lifecycle_state",
        "firmware_releases",
        ["lifecycle_state"],
    )
    op.create_index(
        "uq_firmware_releases_one_current",
        "firmware_releases",
        ["lifecycle_state"],
        unique=True,
        postgresql_where=sa.text("lifecycle_state = 'current'"),
        sqlite_where=sa.text("lifecycle_state = 'current'"),
    )

    with op.batch_alter_table("firmware_deployment_batches") as batch:
        batch.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column(
                "archived_by_user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "troubleshooting_hold",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column(
                "deleted_by_user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            )
        )
    op.create_index(
        "ix_firmware_deployment_batches_archived_at",
        "firmware_deployment_batches",
        ["archived_at"],
    )

    op.create_table(
        "firmware_lifecycle_settings",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column("deployment_retention_days", sa.Integer(), nullable=True, server_default="365"),
        sa.Column(
            "updated_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "deployment_retention_days IS NULL OR deployment_retention_days IN (90,180,365)",
            name="ck_firmware_lifecycle_retention_days",
        ),
    )
    op.execute(
        "INSERT INTO firmware_lifecycle_settings (id, deployment_retention_days) "
        "VALUES ('global', 365)"
    )

    with op.batch_alter_table("rate_plans") as batch:
        batch.add_column(
            sa.Column("utility_code", sa.String(20), nullable=False, server_default="SCE")
        )
        batch.add_column(sa.Column("official_schedule_code", sa.String(80), nullable=True))
        batch.add_column(sa.Column("public_plan_name", sa.String(160), nullable=True))
        batch.add_column(sa.Column("canonical_name", sa.String(160), nullable=True))
        batch.add_column(
            sa.Column("plan_type", sa.String(48), nullable=False, server_default="unknown")
        )
        batch.add_column(
            sa.Column("enrollment_status", sa.String(40), nullable=False, server_default="unknown")
        )
        batch.add_column(sa.Column("eligibility", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch.add_column(sa.Column("currency", sa.String(3), nullable=False, server_default="USD"))
        batch.add_column(
            sa.Column("energy_unit", sa.String(12), nullable=False, server_default="kWh")
        )
        batch.create_check_constraint("ck_rate_plans_plan_type", f"plan_type IN ({PLAN_TYPES})")

    with op.batch_alter_table("rate_plan_versions") as batch:
        batch.add_column(sa.Column("source_version", sa.String(120), nullable=True))
        batch.add_column(
            sa.Column(
                "holiday_treatment",
                sa.String(40),
                nullable=False,
                server_default="unresolved",
            )
        )
        batch.add_column(
            sa.Column("season_definitions", sa.JSON(), nullable=False, server_default="[]")
        )
        batch.add_column(sa.Column("fixed_charges", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(
            sa.Column("price_components", sa.JSON(), nullable=False, server_default="[]")
        )
        batch.add_column(
            sa.Column("eligibility_evidence", sa.JSON(), nullable=False, server_default="[]")
        )
        batch.add_column(
            sa.Column("minimum_charge", sa.Numeric(18, 8), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("meter_charge", sa.Numeric(18, 8), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("other_fixed_charge", sa.Numeric(18, 8), nullable=False, server_default="0")
        )

    with op.batch_alter_table("rate_candidate_reviews") as batch:
        batch.add_column(sa.Column("tier_threshold_rule", sa.JSON(), nullable=True))

    with op.batch_alter_table("rate_periods") as batch:
        batch.add_column(
            sa.Column("rate_components", sa.JSON(), nullable=False, server_default="[]")
        )
        batch.add_column(
            sa.Column(
                "baseline_credit_per_kwh",
                sa.Numeric(18, 8),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column("boundary_inclusive", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.add_column(sa.Column("threshold_basis", sa.String(80), nullable=True))
        batch.add_column(sa.Column("threshold_value", sa.Numeric(18, 8), nullable=True))
        batch.add_column(sa.Column("source_label", sa.String(160), nullable=True))

    op.create_table(
        "utility_account_tier_thresholds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "utility_account_id",
            sa.String(36),
            sa.ForeignKey("utility_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "rate_plan_id",
            sa.String(36),
            sa.ForeignKey("rate_plans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("season", sa.String(30), nullable=False),
        sa.Column("kwh_per_day", sa.Numeric(18, 8), nullable=False),
        sa.Column("source_allowance_kwh", sa.Numeric(18, 8), nullable=False),
        sa.Column("source_billing_days", sa.Integer(), nullable=False),
        sa.Column(
            "tier1_boundary_inclusive",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("source_label", sa.String(160), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("effective_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "utility_account_id",
            "rate_plan_id",
            "season",
            "effective_start",
            name="uq_utility_account_tier_thresholds_account_plan_season_start",
        ),
        sa.CheckConstraint(
            "kwh_per_day > 0",
            name="ck_utility_account_tier_thresholds_positive_kwh_per_day",
        ),
        sa.CheckConstraint(
            "source_allowance_kwh > 0",
            name="ck_utility_account_tier_thresholds_positive_source_allowance",
        ),
        sa.CheckConstraint(
            "source_billing_days >= 1 AND source_billing_days <= 62",
            name="ck_utility_account_tier_thresholds_source_billing_days",
        ),
        sa.CheckConstraint(
            "effective_end IS NULL OR effective_end > effective_start",
            name="ck_utility_account_tier_thresholds_effective_range",
        ),
        sa.CheckConstraint(
            "source_kind IN ('candidate_review','bill_rate_import')",
            name="ck_utility_account_tier_thresholds_source_kind",
        ),
        sa.CheckConstraint(
            _lowercase_hex_64_check("source_artifact_sha256"),
            name="ck_utility_account_tier_thresholds_source_artifact_sha256",
        ),
    )
    op.create_index(
        "ix_utility_account_tier_thresholds_utility_account_id",
        "utility_account_tier_thresholds",
        ["utility_account_id"],
    )
    op.create_index(
        "ix_utility_account_tier_thresholds_rate_plan_id",
        "utility_account_tier_thresholds",
        ["rate_plan_id"],
    )

    op.create_table(
        "rate_dated_prices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "rate_plan_version_id",
            sa.String(36),
            sa.ForeignKey("rate_plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price_per_kwh", sa.Numeric(18, 8), nullable=False),
        sa.Column("delivery_per_kwh", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("generation_per_kwh", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("rate_components", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_label", sa.String(160), nullable=False),
        sa.UniqueConstraint(
            "rate_plan_version_id",
            "start_utc",
            name="uq_rate_dated_prices_rate_plan_version_id",
        ),
        sa.CheckConstraint("end_utc > start_utc", name="ck_rate_dated_prices_interval_order"),
    )
    op.create_index(
        "ix_rate_dated_prices_rate_plan_version_id",
        "rate_dated_prices",
        ["rate_plan_version_id"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER rate_dated_prices_published_parent_immutable "
            "BEFORE INSERT OR UPDATE OR DELETE ON rate_dated_prices "
            "FOR EACH ROW EXECUTE FUNCTION pm_reject_published_rate_child_change()"
        )

    op.create_table(
        "sce_catalog_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "source_revision_id",
            sa.String(36),
            sa.ForeignKey("rate_source_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_url", sa.String(500), nullable=False),
        sa.Column("source_level", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("official_schedule_code", sa.String(80), nullable=True),
        sa.Column("public_plan_name", sa.String(160), nullable=False),
        sa.Column("canonical_name", sa.String(160), nullable=False),
        sa.Column("plan_type", sa.String(48), nullable=False, server_default="unknown"),
        sa.Column("enrollment_status", sa.String(40), nullable=False, server_default="unknown"),
        sa.Column("eligibility", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("discovery_state", sa.String(24), nullable=False),
        sa.Column("exclusion_reason", sa.String(300), nullable=True),
        sa.Column("normalized_plan", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "source_revision_id",
            "canonical_name",
            name="uq_sce_catalog_revision_canonical_name",
        ),
        sa.CheckConstraint(
            f"discovery_state IN ({CATALOG_STATES})",
            name="ck_sce_catalog_discovery_state",
        ),
        sa.CheckConstraint(f"plan_type IN ({PLAN_TYPES})", name="ck_sce_catalog_plan_type"),
        sa.CheckConstraint(
            "(discovery_state <> 'excluded') OR exclusion_reason IS NOT NULL",
            name="ck_sce_catalog_exclusion_reason",
        ),
        sa.CheckConstraint("source_level BETWEEN 1 AND 4", name="ck_sce_catalog_source_level"),
    )
    op.create_index(
        "ix_sce_catalog_entries_source_revision_id",
        "sce_catalog_entries",
        ["source_revision_id"],
    )
    op.create_index(
        "ix_sce_catalog_entries_discovery_state",
        "sce_catalog_entries",
        ["discovery_state"],
    )


def downgrade() -> None:
    # The catalog is derived official-source evidence.  Refuse to discard it
    # implicitly; an operator must export/remove derived catalog rows before a
    # downgrade.  Accepted readings and published rates are never touched.
    connection = op.get_bind()
    dated_price_rows = int(
        connection.scalar(sa.text("SELECT COUNT(*) FROM rate_dated_prices")) or 0
    )
    if dated_price_rows:
        raise RuntimeError(
            "revision 0018 downgrade requires immutable dated rate prices to be retained"
        )
    account_threshold_rows = int(
        connection.scalar(sa.text("SELECT COUNT(*) FROM utility_account_tier_thresholds")) or 0
    )
    if account_threshold_rows:
        raise RuntimeError(
            "revision 0018 downgrade requires account tier-threshold evidence to be retained"
        )
    catalog_rows = int(connection.scalar(sa.text("SELECT COUNT(*) FROM sce_catalog_entries")) or 0)
    if catalog_rows:
        raise RuntimeError(
            "revision 0018 downgrade requires sce_catalog_entries to be explicitly exported "
            "and removed; accepted History and published rate versions remain untouched"
        )

    migration_marker_at = connection.scalar(
        sa.text(
            "SELECT MAX(created_at) FROM audit_events "
            "WHERE event_code = 'SCHEMA_0018_MIGRATED' "
            "AND target_id = '20260820_0018'"
        )
    )
    if migration_marker_at is None:
        raise RuntimeError("revision 0018 downgrade refuses after its audit boundary was removed")
    audit_action_query = (
        sa.text(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE datetime(created_at) >= datetime(:marker_at) "
            "AND (event_code LIKE 'FIRMWARE_%' OR event_code LIKE 'RATE_%') "
            "AND event_code NOT IN "
            "('FIRMWARE_CURRENT_RELEASE_MIGRATED', "
            "'FIRMWARE_ROLLBACK_RELEASE_MIGRATED')"
        )
        if connection.dialect.name == "sqlite"
        else sa.text(
            "SELECT COUNT(*) FROM audit_events WHERE created_at >= :marker_at "
            "AND (event_code LIKE 'FIRMWARE_%' OR event_code LIKE 'RATE_%') "
            "AND event_code NOT IN "
            "('FIRMWARE_CURRENT_RELEASE_MIGRATED', "
            "'FIRMWARE_ROLLBACK_RELEASE_MIGRATED')"
        )
    )
    post_migration_actions = int(
        connection.scalar(
            audit_action_query,
            {"marker_at": migration_marker_at},
        )
        or 0
    )
    if post_migration_actions:
        raise RuntimeError(
            "revision 0018 downgrade refuses to discard post-migration lifecycle or rate audit"
        )

    rate_evidence_queries = {
        "official rate-plan metadata": """
            SELECT COUNT(*) FROM rate_plans
            WHERE utility_code <> 'SCE'
               OR official_schedule_code IS NOT NULL
               OR public_plan_name IS NOT NULL
               OR canonical_name IS NOT NULL
               OR plan_type <> 'unknown'
               OR enrollment_status <> 'unknown'
               OR CAST(eligibility AS TEXT) NOT IN ('[]', 'null')
               OR description IS NOT NULL
               OR currency <> 'USD'
               OR energy_unit <> 'kWh'
        """,
        "immutable rate-version evidence": """
            SELECT COUNT(*) FROM rate_plan_versions
            WHERE source_version IS NOT NULL
               OR holiday_treatment <> 'unresolved'
               OR CAST(season_definitions AS TEXT) NOT IN ('[]', 'null')
               OR CAST(fixed_charges AS TEXT) NOT IN ('[]', 'null')
               OR CAST(price_components AS TEXT) NOT IN ('[]', 'null')
               OR CAST(eligibility_evidence AS TEXT) NOT IN ('[]', 'null')
               OR minimum_charge <> 0
               OR meter_charge <> 0
               OR other_fixed_charge <> 0
        """,
        "reviewed tier-threshold evidence": """
            SELECT COUNT(*) FROM rate_candidate_reviews
            WHERE tier_threshold_rule IS NOT NULL
        """,
        "executable rate-period evidence": """
            SELECT COUNT(*) FROM rate_periods
            WHERE CAST(rate_components AS TEXT) NOT IN ('[]', 'null')
               OR baseline_credit_per_kwh <> 0
               OR boundary_inclusive <> true
               OR threshold_basis IS NOT NULL
               OR threshold_value IS NOT NULL
               OR source_label IS NOT NULL
        """,
    }
    for evidence_name, query in rate_evidence_queries.items():
        if int(connection.scalar(sa.text(query)) or 0):
            raise RuntimeError(f"revision 0018 downgrade refuses to discard {evidence_name}")

    lifecycle_setting_rows = (
        connection.execute(
            sa.text(
                "SELECT id, deployment_retention_days, updated_by_user_id "
                "FROM firmware_lifecycle_settings"
            )
        )
        .mappings()
        .all()
    )
    if len(lifecycle_setting_rows) != 1 or not (
        lifecycle_setting_rows[0]["id"] == "global"
        and lifecycle_setting_rows[0]["deployment_retention_days"] == 365
        and lifecycle_setting_rows[0]["updated_by_user_id"] is None
    ):
        raise RuntimeError("revision 0018 downgrade refuses to discard firmware lifecycle settings")

    deployment_evidence_rows = int(
        connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM firmware_deployment_batches "
                "WHERE archived_at IS NOT NULL OR archived_by_user_id IS NOT NULL "
                "OR troubleshooting_hold = true OR deleted_at IS NOT NULL "
                "OR deleted_by_user_id IS NOT NULL"
            )
        )
        or 0
    )
    if deployment_evidence_rows:
        raise RuntimeError(
            "revision 0018 downgrade refuses to discard firmware deployment lifecycle evidence"
        )

    release_rows = (
        connection.execute(
            sa.text(
                "SELECT id, image_path, created_at, lifecycle_state, rollback_pinned, "
                "archived_at, archived_by_user_id, deleted_at, deleted_by_user_id, "
                "artifact_deleted_at, firmware_build_id FROM firmware_releases"
            )
        )
        .mappings()
        .all()
    )
    retained_rows = sorted(
        (row for row in release_rows if row["image_path"] != ""),
        key=lambda row: (str(row["created_at"]), str(row["id"])),
        reverse=True,
    )
    inferred_current_id = retained_rows[0]["id"] if retained_rows else None
    inferred_rollback_id = retained_rows[1]["id"] if len(retained_rows) > 1 else None
    for row in release_rows:
        if row["firmware_build_id"] is not None:
            raise RuntimeError(
                "revision 0018 downgrade refuses to discard exact firmware build identity"
            )
        if row["image_path"] == "":
            is_legacy_inference = (
                row["lifecycle_state"] == "deleted"
                and not bool(row["rollback_pinned"])
                and row["archived_at"] is None
                and row["archived_by_user_id"] is None
                and row["deleted_by_user_id"] is None
                and row["deleted_at"] == row["created_at"]
                and row["artifact_deleted_at"] == row["created_at"]
            )
        else:
            expected_state = "current" if row["id"] == inferred_current_id else "available"
            is_legacy_inference = (
                row["lifecycle_state"] == expected_state
                and bool(row["rollback_pinned"]) == (row["id"] == inferred_rollback_id)
                and row["archived_at"] is None
                and row["archived_by_user_id"] is None
                and row["deleted_at"] is None
                and row["deleted_by_user_id"] is None
                and row["artifact_deleted_at"] is None
            )
        if not is_legacy_inference:
            raise RuntimeError(
                "revision 0018 downgrade refuses to discard firmware release lifecycle evidence"
            )

    op.drop_index("ix_sce_catalog_entries_discovery_state", table_name="sce_catalog_entries")
    op.drop_index("ix_sce_catalog_entries_source_revision_id", table_name="sce_catalog_entries")
    op.drop_table("sce_catalog_entries")

    op.drop_index(
        "ix_rate_dated_prices_rate_plan_version_id",
        table_name="rate_dated_prices",
    )
    op.drop_table("rate_dated_prices")

    op.drop_index(
        "ix_utility_account_tier_thresholds_rate_plan_id",
        table_name="utility_account_tier_thresholds",
    )
    op.drop_index(
        "ix_utility_account_tier_thresholds_utility_account_id",
        table_name="utility_account_tier_thresholds",
    )
    op.drop_table("utility_account_tier_thresholds")

    with op.batch_alter_table("rate_periods") as batch:
        batch.drop_column("source_label")
        batch.drop_column("threshold_value")
        batch.drop_column("threshold_basis")
        batch.drop_column("boundary_inclusive")
        batch.drop_column("baseline_credit_per_kwh")
        batch.drop_column("rate_components")

    with op.batch_alter_table("rate_plan_versions") as batch:
        batch.drop_column("other_fixed_charge")
        batch.drop_column("meter_charge")
        batch.drop_column("minimum_charge")
        batch.drop_column("eligibility_evidence")
        batch.drop_column("price_components")
        batch.drop_column("fixed_charges")
        batch.drop_column("season_definitions")
        batch.drop_column("holiday_treatment")
        batch.drop_column("source_version")

    with op.batch_alter_table("rate_candidate_reviews") as batch:
        batch.drop_column("tier_threshold_rule")

    with op.batch_alter_table("rate_plans") as batch:
        batch.drop_column("energy_unit")
        batch.drop_column("currency")
        batch.drop_column("description")
        batch.drop_column("eligibility")
        batch.drop_column("enrollment_status")
        batch.drop_constraint("ck_rate_plans_plan_type", type_="check")
        batch.drop_column("plan_type")
        batch.drop_column("canonical_name")
        batch.drop_column("public_plan_name")
        batch.drop_column("official_schedule_code")
        batch.drop_column("utility_code")

    op.drop_table("firmware_lifecycle_settings")
    op.drop_index(
        "ix_firmware_deployment_batches_archived_at",
        table_name="firmware_deployment_batches",
    )
    with op.batch_alter_table("firmware_deployment_batches") as batch:
        batch.drop_column("deleted_by_user_id")
        batch.drop_column("deleted_at")
        batch.drop_column("troubleshooting_hold")
        batch.drop_column("archived_by_user_id")
        batch.drop_column("archived_at")

    op.drop_index("uq_firmware_releases_one_current", table_name="firmware_releases")
    op.drop_index("ix_firmware_releases_lifecycle_state", table_name="firmware_releases")
    with op.batch_alter_table("firmware_releases") as batch:
        batch.drop_constraint("ck_firmware_releases_firmware_build_id", type_="check")
        batch.drop_constraint("ck_firmware_releases_lifecycle_evidence", type_="check")
        batch.drop_constraint("ck_firmware_releases_lifecycle_state", type_="check")
        batch.drop_column("firmware_build_id")
        batch.drop_column("updated_at")
        batch.drop_column("artifact_deleted_at")
        batch.drop_column("deleted_by_user_id")
        batch.drop_column("deleted_at")
        batch.drop_column("archived_by_user_id")
        batch.drop_column("archived_at")
        batch.drop_column("rollback_pinned")
        batch.drop_column("lifecycle_state")
