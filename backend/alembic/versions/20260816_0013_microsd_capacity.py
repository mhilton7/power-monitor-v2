"""Persist authenticated microSD capacity evidence.

Revision ID: 20260816_0013
Revises: 20260815_0012
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260816_0013"
down_revision = "20260815_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("device_heartbeats") as batch:
        batch.add_column(sa.Column("storage_bytes_total", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("storage_bytes_free", sa.BigInteger(), nullable=True))
        batch.create_check_constraint(
            "ck_device_heartbeats_storage_capacity_pair",
            "(storage_bytes_total IS NULL AND storage_bytes_free IS NULL) OR "
            "(storage_bytes_total > 0 AND storage_bytes_free >= 0 "
            "AND storage_bytes_free <= storage_bytes_total)",
        )


def downgrade() -> None:
    with op.batch_alter_table("device_heartbeats") as batch:
        batch.drop_constraint("ck_device_heartbeats_storage_capacity_pair", type_="check")
        batch.drop_column("storage_bytes_free")
        batch.drop_column("storage_bytes_total")
