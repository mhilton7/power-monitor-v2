"""Add Settings-owned billing calculation configuration.

Revision ID: 20260821_0019
Revises: 20260820_0018
Create Date: 2026-08-21

This migration is additive. Existing utility accounts, rate assignments,
published versions, bill drafts, source evidence, and History remain intact.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260821_0019"
down_revision = "20260820_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("utility_accounts") as batch:
        batch.add_column(sa.Column("currency", sa.String(3), nullable=False, server_default="USD"))
        batch.add_column(
            sa.Column(
                "generation_service_kind",
                sa.String(24),
                nullable=False,
                server_default="sce_generation",
            )
        )
        batch.add_column(sa.Column("baseline_region", sa.String(80), nullable=True))
        batch.add_column(sa.Column("summer_baseline_kwh_per_day", sa.Numeric(12, 4), nullable=True))
        batch.add_column(sa.Column("winter_baseline_kwh_per_day", sa.Numeric(12, 4), nullable=True))
        batch.add_column(
            sa.Column("all_electric", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("medical_baseline", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column(
                "heat_pump_allocation", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(
            sa.Column(
                "estimate_high_coverage",
                sa.Numeric(7, 6),
                nullable=False,
                server_default="0.99",
            )
        )
        batch.add_column(
            sa.Column(
                "estimate_min_coverage",
                sa.Numeric(7, 6),
                nullable=False,
                server_default="0.95",
            )
        )
        batch.add_column(
            sa.Column(
                "max_estimatable_gap_seconds",
                sa.Integer(),
                nullable=False,
                server_default="900",
            )
        )
        batch.add_column(
            sa.Column(
                "projection_minimum_hours",
                sa.Integer(),
                nullable=False,
                server_default="24",
            )
        )
        batch.create_check_constraint(
            "ck_utility_accounts_currency",
            "currency = upper(currency) AND length(currency) = 3",
        )
        batch.create_check_constraint(
            "ck_utility_accounts_generation_service_kind",
            "generation_service_kind IN ('sce_generation','cca','direct_access','unknown')",
        )
        batch.create_check_constraint(
            "ck_utility_accounts_summer_baseline_nonnegative",
            "summer_baseline_kwh_per_day IS NULL OR summer_baseline_kwh_per_day >= 0",
        )
        batch.create_check_constraint(
            "ck_utility_accounts_winter_baseline_nonnegative",
            "winter_baseline_kwh_per_day IS NULL OR winter_baseline_kwh_per_day >= 0",
        )
        batch.create_check_constraint(
            "ck_utility_accounts_estimate_coverage_order",
            "estimate_min_coverage >= 0 AND estimate_high_coverage <= 1 "
            "AND estimate_min_coverage <= estimate_high_coverage",
        )
        batch.create_check_constraint(
            "ck_utility_accounts_max_estimatable_gap",
            "max_estimatable_gap_seconds >= 60 AND max_estimatable_gap_seconds <= 86400",
        )
        batch.create_check_constraint(
            "ck_utility_accounts_projection_minimum_hours",
            "projection_minimum_hours >= 1 AND projection_minimum_hours <= 720",
        )
    op.execute(
        "UPDATE utility_accounts SET generation_service_kind = 'cca' "
        "WHERE cca_provider IS NOT NULL AND trim(cca_provider) <> ''"
    )


def downgrade() -> None:
    connection = op.get_bind()
    customized_accounts = int(
        connection.scalar(
            sa.text(
                "SELECT count(*) FROM utility_accounts WHERE "
                "currency <> 'USD' OR "
                "generation_service_kind <> CASE "
                "WHEN cca_provider IS NOT NULL AND trim(cca_provider) <> '' THEN 'cca' "
                "ELSE 'sce_generation' END OR "
                "baseline_region IS NOT NULL OR "
                "summer_baseline_kwh_per_day IS NOT NULL OR "
                "winter_baseline_kwh_per_day IS NOT NULL OR "
                "all_electric <> false OR medical_baseline <> false OR "
                "heat_pump_allocation <> false OR "
                "estimate_high_coverage <> 0.99 OR estimate_min_coverage <> 0.95 OR "
                "max_estimatable_gap_seconds <> 900 OR projection_minimum_hours <> 24"
            )
        )
        or 0
    )
    if customized_accounts:
        raise RuntimeError(
            "refusing to downgrade: Settings-owned billing configuration would be lost"
        )
    with op.batch_alter_table("utility_accounts") as batch:
        batch.drop_constraint("ck_utility_accounts_projection_minimum_hours", type_="check")
        batch.drop_constraint("ck_utility_accounts_max_estimatable_gap", type_="check")
        batch.drop_constraint("ck_utility_accounts_estimate_coverage_order", type_="check")
        batch.drop_constraint("ck_utility_accounts_winter_baseline_nonnegative", type_="check")
        batch.drop_constraint("ck_utility_accounts_summer_baseline_nonnegative", type_="check")
        batch.drop_constraint("ck_utility_accounts_generation_service_kind", type_="check")
        batch.drop_constraint("ck_utility_accounts_currency", type_="check")
        for column in (
            "projection_minimum_hours",
            "max_estimatable_gap_seconds",
            "estimate_min_coverage",
            "estimate_high_coverage",
            "heat_pump_allocation",
            "medical_baseline",
            "all_electric",
            "winter_baseline_kwh_per_day",
            "summer_baseline_kwh_per_day",
            "baseline_region",
            "generation_service_kind",
            "currency",
        ):
            batch.drop_column(column)
