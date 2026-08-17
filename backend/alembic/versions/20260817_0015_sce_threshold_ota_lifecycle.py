"""Add structured SCE thresholds and recoverable per-sensor OTA batches.

Revision ID: 20260817_0015
Revises: 20260817_0014
Create Date: 2026-08-17
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "20260817_0015"
down_revision = "20260817_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("utility_bill_rate_extractions", sa.Column("tier_threshold_rule", sa.JSON()))
    op.add_column("rate_plan_versions", sa.Column("tier_threshold_kwh_per_day", sa.Numeric(18, 8)))
    op.add_column("rate_plan_versions", sa.Column("tier_threshold_season", sa.String(20)))
    op.add_column("rate_plan_versions", sa.Column("tier_threshold_source_kwh", sa.Numeric(18, 8)))
    op.add_column("rate_plan_versions", sa.Column("tier_threshold_source_days", sa.Integer()))
    op.add_column(
        "rate_plan_versions",
        sa.Column("tier1_boundary_inclusive", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    op.create_table(
        "firmware_deployment_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "firmware_release_id",
            sa.String(36),
            sa.ForeignKey("firmware_releases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rollout", sa.String(20), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="queued"),
        sa.Column(
            "created_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "rollout IN ('immediate','staged','retry','legacy')",
            name="ck_firmware_batch_rollout",
        ),
        sa.CheckConstraint(
            "state IN ('queued','in_progress','partial','succeeded','failed','cancelled','expired')",
            name="ck_firmware_batch_state",
        ),
    )
    op.create_index(
        "ix_firmware_deployment_batches_firmware_release_id",
        "firmware_deployment_batches",
        ["firmware_release_id"],
    )
    with op.batch_alter_table("firmware_deployments") as batch_op:
        batch_op.add_column(sa.Column("batch_id", sa.String(36)))
        batch_op.create_foreign_key(
            "fk_firmware_deployments_batch_id",
            "firmware_deployment_batches",
            ["batch_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index("ix_firmware_deployments_batch_id", ["batch_id"])
        batch_op.add_column(
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(sa.Column("error_code", sa.String(80)))
        batch_op.add_column(sa.Column("error_message", sa.String(500)))
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.execute("UPDATE firmware_deployments SET updated_at = created_at WHERE updated_at IS NULL")
    with op.batch_alter_table("firmware_deployments") as batch_op:
        batch_op.alter_column("updated_at", nullable=False)

    bind = op.get_bind()
    deployments = sa.table(
        "firmware_deployments",
        sa.column("id", sa.String(36)),
        sa.column("batch_id", sa.String(36)),
        sa.column("firmware_release_id", sa.String(36)),
        sa.column("state", sa.String(32)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("completed_at", sa.DateTime(timezone=True)),
    )
    batches = sa.table(
        "firmware_deployment_batches",
        sa.column("id", sa.String(36)),
        sa.column("firmware_release_id", sa.String(36)),
        sa.column("rollout", sa.String(20)),
        sa.column("state", sa.String(24)),
        sa.column("created_by_user_id", sa.String(36)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("completed_at", sa.DateTime(timezone=True)),
    )
    legacy_rows = bind.execute(
        sa.select(
            deployments.c.id,
            deployments.c.firmware_release_id,
            deployments.c.state,
            deployments.c.created_at,
            deployments.c.updated_at,
            deployments.c.completed_at,
        ).where(deployments.c.batch_id.is_(None))
    ).all()
    for row in legacy_rows:
        batch_id = str(uuid4())
        if row.state in {"staged", "queued", "downloading", "rebooting", "validating"}:
            batch_state = "in_progress"
            batch_completed_at = None
        elif row.state == "succeeded":
            batch_state = "succeeded"
            batch_completed_at = row.completed_at or row.updated_at
        elif row.state == "cancelled":
            batch_state = "cancelled"
            batch_completed_at = row.completed_at or row.updated_at
        else:
            batch_state = "failed"
            batch_completed_at = row.completed_at or row.updated_at
        bind.execute(
            batches.insert().values(
                id=batch_id,
                firmware_release_id=row.firmware_release_id,
                rollout="legacy",
                state=batch_state,
                created_by_user_id=None,
                created_at=row.created_at,
                updated_at=row.updated_at,
                completed_at=batch_completed_at,
            )
        )
        bind.execute(
            deployments.update()
            .where(deployments.c.id == row.id)
            .values(batch_id=batch_id)
        )
    with op.batch_alter_table("firmware_deployments") as batch_op:
        batch_op.alter_column("batch_id", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("firmware_deployments") as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("error_message")
        batch_op.drop_column("error_code")
        batch_op.drop_column("attempt")
        batch_op.drop_index("ix_firmware_deployments_batch_id")
        batch_op.drop_constraint("fk_firmware_deployments_batch_id", type_="foreignkey")
        batch_op.drop_column("batch_id")
    op.drop_index(
        "ix_firmware_deployment_batches_firmware_release_id",
        table_name="firmware_deployment_batches",
    )
    op.drop_table("firmware_deployment_batches")
    op.drop_column("rate_plan_versions", "tier1_boundary_inclusive")
    op.drop_column("rate_plan_versions", "tier_threshold_source_days")
    op.drop_column("rate_plan_versions", "tier_threshold_source_kwh")
    op.drop_column("rate_plan_versions", "tier_threshold_season")
    op.drop_column("rate_plan_versions", "tier_threshold_kwh_per_day")
    op.drop_column("utility_bill_rate_extractions", "tier_threshold_rule")
