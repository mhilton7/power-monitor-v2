"""Add immutable rate children and selected-cost provenance.

Revision ID: 20260813_0004
Revises: 20260813_0003
Create Date: 2026-08-13
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa

from alembic import op

revision = "20260813_0004"
down_revision = "20260813_0003"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "rate_holidays" not in tables:
        op.create_table(
            "rate_holidays",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("rate_plan_version_id", sa.String(length=36), nullable=False),
            sa.Column("local_date", sa.Date(), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.ForeignKeyConstraint(
                ["rate_plan_version_id"], ["rate_plan_versions.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("rate_plan_version_id", "local_date"),
        )
        op.create_index(
            "ix_rate_holidays_rate_plan_version_id",
            "rate_holidays",
            ["rate_plan_version_id"],
        )
    if "interval_cost_selections" not in tables:
        op.create_table(
            "interval_cost_selections",
            sa.Column("normalized_interval_id", sa.String(length=36), nullable=False),
            sa.Column("interval_cost_id", sa.String(length=36), nullable=False),
            sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("selection_reason", sa.String(length=80), nullable=False),
            sa.ForeignKeyConstraint(
                ["interval_cost_id"], ["interval_costs.id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["normalized_interval_id"], ["normalized_intervals.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("normalized_interval_id"),
            sa.UniqueConstraint("interval_cost_id"),
        )
    billing_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("billing_estimates")
    }
    added_rate_version = "rate_plan_version_id" not in billing_columns
    added_input_sha = "input_sha256" not in billing_columns
    billing_additions = (
        sa.Column("rate_plan_version_id", sa.String(length=36), nullable=True),
        sa.Column(
            "estimate_kind",
            sa.String(length=40),
            nullable=False,
            server_default="billing_cycle_to_date",
        ),
        sa.Column("scope_kind", sa.String(length=32), nullable=False, server_default="energy_only"),
        sa.Column(
            "scope_id", sa.String(length=80), nullable=False, server_default="legacy-unscoped"
        ),
        sa.Column("member_device_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("energy_cost_microdollars", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("fixed_charge_microdollars", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("credit_microdollars", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("input_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "calculated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    for column in billing_additions:
        if column.name not in billing_columns:
            op.add_column("billing_estimates", column)
    op.execute(
        """
        UPDATE billing_estimates
        SET rate_plan_version_id = cost_runs.rate_plan_version_id
        FROM cost_runs
        WHERE billing_estimates.cost_run_id = cost_runs.id
          AND billing_estimates.rate_plan_version_id IS NULL
        """
    )
    connection = op.get_bind()
    for estimate_id, input_sha in connection.execute(
        sa.text("SELECT id, input_sha256 FROM billing_estimates")
    ):
        if input_sha is None:
            connection.execute(
                sa.text("UPDATE billing_estimates SET input_sha256=:digest WHERE id=:estimate_id"),
                {
                    "digest": hashlib.sha256(
                        f"legacy-billing-estimate:{estimate_id}".encode()
                    ).hexdigest(),
                    "estimate_id": estimate_id,
                },
            )
    if added_rate_version or added_input_sha:
        with op.batch_alter_table("billing_estimates") as batch:
            if added_rate_version:
                batch.alter_column(
                    "rate_plan_version_id",
                    existing_type=sa.String(length=36),
                    nullable=False,
                )
                batch.create_foreign_key(
                    "fk_billing_estimates_rate_plan_version_id",
                    "rate_plan_versions",
                    ["rate_plan_version_id"],
                    ["id"],
                    ondelete="RESTRICT",
                )
            if added_input_sha:
                batch.alter_column(
                    "input_sha256",
                    existing_type=sa.String(length=64),
                    nullable=False,
                )
                batch.create_unique_constraint(
                    "uq_billing_estimates_input_sha256",
                    ["input_sha256"],
                )
    if added_rate_version:
        op.create_index(
            "ix_billing_estimates_rate_plan_version_id",
            "billing_estimates",
            ["rate_plan_version_id"],
        )
    if "billing_estimate_selections" not in tables:
        op.create_table(
            "billing_estimate_selections",
            sa.Column("utility_account_id", sa.String(length=36), nullable=False),
            sa.Column("estimate_kind", sa.String(length=40), nullable=False),
            sa.Column("scope_kind", sa.String(length=32), nullable=False),
            sa.Column("scope_id", sa.String(length=80), nullable=False),
            sa.Column("billing_estimate_id", sa.String(length=36), nullable=False),
            sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["billing_estimate_id"], ["billing_estimates.id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["utility_account_id"], ["utility_accounts.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint(
                "utility_account_id", "estimate_kind", "scope_kind", "scope_id"
            ),
            sa.UniqueConstraint("billing_estimate_id"),
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION pm_reject_published_rate_child_change()
            RETURNS trigger AS $$
            DECLARE parent_id text;
            DECLARE parent_state text;
            BEGIN
              parent_id := CASE WHEN TG_OP = 'DELETE'
                                THEN OLD.rate_plan_version_id
                                ELSE NEW.rate_plan_version_id END;
              SELECT state INTO parent_state FROM rate_plan_versions WHERE id = parent_id;
              IF parent_state = 'published' THEN
                RAISE EXCEPTION 'children of published rate versions are immutable';
              END IF;
              RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        for table in ("rate_periods", "rate_holidays"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_published_parent_immutable ON {table}")
            op.execute(
                f"CREATE TRIGGER {table}_published_parent_immutable "
                f"BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION pm_reject_published_rate_child_change()"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS pm_reject_published_rate_child_change() CASCADE")
    tables = _tables()
    if "billing_estimate_selections" in tables:
        op.drop_table("billing_estimate_selections")
    billing_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("billing_estimates")
    }
    if "rate_plan_version_id" in billing_columns:
        op.drop_index("ix_billing_estimates_rate_plan_version_id", table_name="billing_estimates")
    with op.batch_alter_table("billing_estimates") as batch:
        if "input_sha256" in billing_columns:
            batch.drop_constraint("uq_billing_estimates_input_sha256", type_="unique")
        if "rate_plan_version_id" in billing_columns:
            batch.drop_constraint(
                "fk_billing_estimates_rate_plan_version_id",
                type_="foreignkey",
            )
        for column in (
            "calculated_at",
            "input_sha256",
            "credit_microdollars",
            "fixed_charge_microdollars",
            "energy_cost_microdollars",
            "member_device_ids",
            "scope_id",
            "scope_kind",
            "estimate_kind",
            "rate_plan_version_id",
        ):
            if column in billing_columns:
                batch.drop_column(column)
    if "interval_cost_selections" in tables:
        op.drop_table("interval_cost_selections")
    if "rate_holidays" in tables:
        op.drop_index("ix_rate_holidays_rate_plan_version_id", table_name="rate_holidays")
        op.drop_table("rate_holidays")
