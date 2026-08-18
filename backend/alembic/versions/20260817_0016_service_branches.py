"""Generalize verified circuits into named service branches.

Revision ID: 20260817_0016
Revises: 20260817_0015
Create Date: 2026-08-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260817_0016"
down_revision = "20260817_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("circuits") as batch:
        batch.add_column(sa.Column("description", sa.String(500), nullable=True))
        batch.add_column(
            sa.Column(
                "purpose",
                sa.String(40),
                nullable=False,
                server_default="electrical_section",
            )
        )
        batch.add_column(
            sa.Column("is_home_total", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column(
                "non_overlapping_confirmed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        batch.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )

    # A legacy verified_sum circuit could only be created after the operator
    # supplied the exact non-overlap confirmation phrase. Preserve that fact.
    op.execute(
        "UPDATE circuits SET non_overlapping_confirmed = true WHERE aggregate_mode = 'verified_sum'"
    )

    # Designate only an unambiguous existing safe aggregate. This reads current
    # immutable sensor membership and never depends on mutable display names.
    op.execute(
        """
        WITH eligible AS (
            SELECT c.id, c.home_id
              FROM circuits AS c
              JOIN devices AS d ON d.circuit_id = c.id
             WHERE c.aggregate_mode = 'verified_sum'
               AND d.include_in_aggregate = true
               AND d.revoked_at IS NULL
             GROUP BY c.id, c.home_id
            HAVING COUNT(d.id) >= 2
        ), unambiguous AS (
            SELECT home_id, MIN(id) AS circuit_id
              FROM eligible
             GROUP BY home_id
            HAVING COUNT(id) = 1
        )
        UPDATE circuits
           SET name = 'Main service',
               purpose = 'whole_home_total',
               is_home_total = true,
               non_overlapping_confirmed = true,
               updated_at = CURRENT_TIMESTAMP
         WHERE id IN (SELECT circuit_id FROM unambiguous)
        """
    )
    op.execute(
        """
        UPDATE devices
           SET measurement_scope = 'full_account'
         WHERE include_in_aggregate = true
           AND circuit_id IN (SELECT id FROM circuits WHERE is_home_total = true)
        """
    )
    op.execute(
        """
        UPDATE utility_accounts
           SET cost_scope = 'full_account'
         WHERE home_id IN (SELECT home_id FROM circuits WHERE is_home_total = true)
        """
    )

    with op.batch_alter_table("circuits") as batch:
        batch.create_check_constraint(
            "ck_circuits_purpose",
            "purpose IN ('electrical_section','whole_home_total')",
        )
        batch.create_check_constraint(
            "ck_circuits_home_total_verified",
            "is_home_total = false OR "
            "(purpose = 'whole_home_total' AND aggregate_mode = 'verified_sum' "
            "AND non_overlapping_confirmed = true)",
        )
    op.create_index(
        "uq_circuits_one_home_total",
        "circuits",
        ["home_id"],
        unique=True,
        postgresql_where=sa.text("is_home_total = true"),
        sqlite_where=sa.text("is_home_total = 1"),
    )


def downgrade() -> None:
    op.drop_index("uq_circuits_one_home_total", table_name="circuits")
    with op.batch_alter_table("circuits") as batch:
        batch.drop_constraint("ck_circuits_home_total_verified", type_="check")
        batch.drop_constraint("ck_circuits_purpose", type_="check")
        batch.drop_column("updated_at")
        batch.drop_column("created_at")
        batch.drop_column("non_overlapping_confirmed")
        batch.drop_column("is_home_total")
        batch.drop_column("purpose")
        batch.drop_column("description")
