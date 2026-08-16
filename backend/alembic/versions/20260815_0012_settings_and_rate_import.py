"""Add scoped settings and typed bill-rate evidence.

Revision ID: 20260815_0012
Revises: 20260815_0011
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260815_0012"
down_revision = "20260815_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    duplicate_email = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT lower(trim(email)) AS normalized_email "
                "FROM users GROUP BY lower(trim(email)) HAVING count(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicate_email is not None:
        raise RuntimeError("migration 0012 refuses case-insensitive duplicate user email addresses")
    op.execute("UPDATE users SET email = lower(trim(email))")

    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("preferences", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )
    op.create_index(
        "uq_users_email_lower",
        "users",
        [sa.text("lower(email)")],
        unique=True,
    )

    with op.batch_alter_table("devices") as batch:
        batch.add_column(sa.Column("location", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("notes", sa.String(length=500), nullable=True))
        batch.add_column(
            sa.Column("display_order", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "include_in_aggregate", sa.Boolean(), nullable=False, server_default=sa.true()
            )
        )
        batch.add_column(
            sa.Column("show_on_dashboard", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.add_column(
            sa.Column("monitoring_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )

    with op.batch_alter_table("utility_bill_rate_extractions") as batch:
        batch.add_column(
            sa.Column(
                "plan_classification",
                sa.String(length=32),
                nullable=False,
                server_default="time_of_use",
            )
        )
        batch.add_column(
            sa.Column(
                "holiday_treatment",
                sa.String(length=40),
                nullable=False,
                server_default="unresolved",
            )
        )
        batch.add_column(sa.Column("billing_period_start", sa.Date(), nullable=True))
        batch.add_column(sa.Column("billing_period_end", sa.Date(), nullable=True))
        batch.add_column(sa.Column("billing_period_days", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("tier_threshold_basis", sa.String(length=500), nullable=True))
        batch.add_column(
            sa.Column("candidate_complete", sa.Boolean(), nullable=False, server_default=sa.true())
        )

    # Existing installations contain only the earlier complete TOU parser
    # output. New DOMESTIC imports explicitly set their semantic fields.
    op.execute(
        "UPDATE utility_bill_rate_extractions "
        "SET plan_classification = 'time_of_use', "
        "holiday_treatment = 'weekend_schedule', candidate_complete = true"
    )
    # Earlier UI code could persist the selector label as ``Home (<uuid>)``.
    # Normalize only that exact UUID-shaped presentation string; the internal
    # Home.id and every relationship remain untouched.
    op.execute(
        "UPDATE homes SET name = 'Home' "
        "WHERE trim(name) = '' OR ("
        "length(trim(name)) = 43 AND substr(trim(name), 1, 6) = 'Home (' "
        "AND substr(trim(name), 15, 1) = '-' "
        "AND substr(trim(name), 20, 1) = '-' "
        "AND substr(trim(name), 25, 1) = '-' "
        "AND substr(trim(name), 30, 1) = '-' "
        "AND substr(trim(name), 43, 1) = ')')"
    )


def downgrade() -> None:
    op.drop_index("uq_users_email_lower", table_name="users")

    with op.batch_alter_table("utility_bill_rate_extractions") as batch:
        batch.drop_column("candidate_complete")
        batch.drop_column("tier_threshold_basis")
        batch.drop_column("billing_period_days")
        batch.drop_column("billing_period_end")
        batch.drop_column("billing_period_start")
        batch.drop_column("holiday_treatment")
        batch.drop_column("plan_classification")

    with op.batch_alter_table("devices") as batch:
        batch.drop_column("monitoring_enabled")
        batch.drop_column("show_on_dashboard")
        batch.drop_column("include_in_aggregate")
        batch.drop_column("display_order")
        batch.drop_column("notes")
        batch.drop_column("location")

    with op.batch_alter_table("users") as batch:
        batch.drop_column("preferences")
