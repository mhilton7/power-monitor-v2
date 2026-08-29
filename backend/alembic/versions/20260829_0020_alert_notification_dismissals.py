"""Add per-user alert notification dismissals.

Revision ID: 20260829_0020
Revises: 20260821_0019
Create Date: 2026-08-29

Dismissals are disposable per-user view state. Alert rows, lifecycle events,
condition state, and evidence remain unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260829_0020"
down_revision = "20260821_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_dismissals",
        sa.Column("alert_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "dismissed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["alerts.id"],
            name="fk_alert_dismissals_alert_id_alerts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_alert_dismissals_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("alert_id", "user_id", name="pk_alert_dismissals"),
    )
    op.create_index(
        "ix_alert_dismissals_user_id",
        "alert_dismissals",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_alert_dismissals_user_id", table_name="alert_dismissals")
    op.drop_table("alert_dismissals")
