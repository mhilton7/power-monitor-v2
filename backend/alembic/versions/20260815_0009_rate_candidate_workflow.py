"""Add exact-home rate-candidate review workflow state.

Revision ID: 20260815_0009
Revises: 20260815_0008
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260815_0009"
down_revision = "20260815_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_candidate_reviews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("home_id", sa.String(length=36), nullable=False),
        sa.Column("selected_plan_name", sa.String(length=120), nullable=False),
        sa.Column("effective_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("reviewed_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rate_plan_version_id", sa.String(length=36), nullable=True),
        sa.Column("utility_account_id", sa.String(length=36), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "effective_end IS NULL OR effective_end > effective_start",
            name=op.f("ck_rate_candidate_reviews_effective_range"),
        ),
        sa.CheckConstraint(
            "state IN ('reviewed','published','activated','rejected')",
            name=op.f("ck_rate_candidate_reviews_workflow_state"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["rate_candidates.id"],
            name=op.f("fk_rate_candidate_reviews_candidate_id_rate_candidates"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["home_id"],
            ["homes.id"],
            name=op.f("fk_rate_candidate_reviews_home_id_homes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rate_plan_version_id"],
            ["rate_plan_versions.id"],
            name=op.f("fk_rate_candidate_reviews_rate_plan_version_id_rate_plan_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["users.id"],
            name=op.f("fk_rate_candidate_reviews_reviewed_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["utility_account_id"],
            ["utility_accounts.id"],
            name=op.f("fk_rate_candidate_reviews_utility_account_id_utility_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_candidate_reviews")),
        sa.UniqueConstraint(
            "candidate_id",
            "home_id",
            name=op.f("uq_rate_candidate_reviews_candidate_id"),
        ),
    )
    op.create_index(
        op.f("ix_rate_candidate_reviews_candidate_id"),
        "rate_candidate_reviews",
        ["candidate_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_rate_candidate_reviews_home_id"),
        "rate_candidate_reviews",
        ["home_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_rate_candidate_reviews_home_id"),
        table_name="rate_candidate_reviews",
    )
    op.drop_index(
        op.f("ix_rate_candidate_reviews_candidate_id"),
        table_name="rate_candidate_reviews",
    )
    op.drop_table("rate_candidate_reviews")
