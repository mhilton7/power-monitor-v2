"""Add database-backed login throttling state.

Revision ID: 20260813_0003
Revises: 20260813_0002
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260813_0003"
down_revision = "20260813_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("login_throttles"):
        return
    op.create_table(
        "login_throttles",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "failure_count >= 0",
            name=op.f("ck_login_throttles_failure_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "scope IN ('principal','source')",
            name=op.f("ck_login_throttles_scope"),
        ),
        sa.PrimaryKeyConstraint("scope", "key_hash", name=op.f("pk_login_throttles")),
    )
    op.create_index(
        op.f("ix_login_throttles_locked_until"),
        "login_throttles",
        ["locked_until"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("login_throttles"):
        op.drop_table("login_throttles")
