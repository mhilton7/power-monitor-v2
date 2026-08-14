"""Store the minimum compatible boot ABI for OTA releases.

Revision ID: 20260813_0002
Revises: 20260813_0001
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260813_0002"
down_revision = "20260813_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("firmware_releases")}
    if "minimum_boot_version" not in columns:
        op.add_column(
            "firmware_releases",
            sa.Column(
                "minimum_boot_version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("firmware_releases")}
    if "minimum_boot_version" in columns:
        op.drop_column("firmware_releases", "minimum_boot_version")
