"""Add a bounded, auditable two-key device credential rotation lifecycle.

Revision ID: 20260813_0007
Revises: 20260813_0006
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260813_0007"
down_revision = "20260813_0006"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("device_credentials")
    }


def _constraints(kind: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    values = (
        inspector.get_unique_constraints("device_credentials")
        if kind == "unique"
        else inspector.get_check_constraints("device_credentials")
    )
    return {value["name"] for value in values if value.get("name")}


def _foreign_keys() -> set[str]:
    return {
        value["name"]
        for value in sa.inspect(op.get_bind()).get_foreign_keys("device_credentials")
        if value.get("name")
    }


def _indexes() -> set[str]:
    return {
        value["name"]
        for value in sa.inspect(op.get_bind()).get_indexes("device_credentials")
        if value.get("name")
    }


def upgrade() -> None:
    existing = _columns()
    additions: tuple[sa.Column[object], ...] = (
        sa.Column("state", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("rotation_id", sa.String(length=36), nullable=True),
        sa.Column("overlap_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prepare_command_id", sa.String(length=36), nullable=True),
        sa.Column("commit_command_id", sa.String(length=36), nullable=True),
        sa.Column("cancel_command_id", sa.String(length=36), nullable=True),
        sa.Column("initiated_by_user_id", sa.String(length=36), nullable=True),
    )
    with op.batch_alter_table("device_credentials") as batch:
        for column in additions:
            if column.name not in existing:
                batch.add_column(column)

    op.execute(
        sa.text(
            "UPDATE device_credentials SET state = 'active', "
            "activated_at = COALESCE(activated_at, created_at) "
            "WHERE state IS NULL OR activated_at IS NULL"
        )
    )

    foreign_keys = _foreign_keys()
    uniques = _constraints("unique")
    checks = _constraints("check")
    with op.batch_alter_table("device_credentials") as batch:
        for name, local, remote, ondelete in (
            (
                "fk_device_credentials_prepare_command_id_device_commands",
                "prepare_command_id",
                "device_commands.id",
                "SET NULL",
            ),
            (
                "fk_device_credentials_commit_command_id_device_commands",
                "commit_command_id",
                "device_commands.id",
                "SET NULL",
            ),
            (
                "fk_device_credentials_cancel_command_id_device_commands",
                "cancel_command_id",
                "device_commands.id",
                "SET NULL",
            ),
            (
                "fk_device_credentials_initiated_by_user_id_users",
                "initiated_by_user_id",
                "users.id",
                "SET NULL",
            ),
        ):
            if name not in foreign_keys:
                remote_table, remote_column = remote.split(".", 1)
                batch.create_foreign_key(
                    op.f(name),
                    remote_table,
                    [local],
                    [remote_column],
                    ondelete=ondelete,
                )
        for name, columns in (
            ("uq_device_credentials_rotation_id", ["rotation_id"]),
            ("uq_device_credentials_prepare_command_id", ["prepare_command_id"]),
            ("uq_device_credentials_commit_command_id", ["commit_command_id"]),
            ("uq_device_credentials_cancel_command_id", ["cancel_command_id"]),
            ("uq_device_credentials_device_id", ["device_id", "key_version"]),
        ):
            if name not in uniques:
                batch.create_unique_constraint(op.f(name), columns)
        if "ck_device_credentials_state" not in checks:
            batch.create_check_constraint(
                op.f("ck_device_credentials_state"),
                "state IN ('active','pending','prepared','retiring','revoked')",
            )
    if "ix_device_credentials_state" not in _indexes():
        op.create_index("ix_device_credentials_state", "device_credentials", ["state"])


def downgrade() -> None:
    indexes = _indexes()
    if "ix_device_credentials_state" in indexes:
        op.drop_index("ix_device_credentials_state", table_name="device_credentials")
    foreign_keys = _foreign_keys()
    uniques = _constraints("unique")
    checks = _constraints("check")
    with op.batch_alter_table("device_credentials") as batch:
        if "ck_device_credentials_state" in checks:
            batch.drop_constraint(op.f("ck_device_credentials_state"), type_="check")
        for name in (
            "uq_device_credentials_device_id",
            "uq_device_credentials_cancel_command_id",
            "uq_device_credentials_commit_command_id",
            "uq_device_credentials_prepare_command_id",
            "uq_device_credentials_rotation_id",
        ):
            if name in uniques:
                batch.drop_constraint(op.f(name), type_="unique")
        for name in (
            "fk_device_credentials_initiated_by_user_id_users",
            "fk_device_credentials_cancel_command_id_device_commands",
            "fk_device_credentials_commit_command_id_device_commands",
            "fk_device_credentials_prepare_command_id_device_commands",
        ):
            if name in foreign_keys:
                batch.drop_constraint(op.f(name), type_="foreignkey")
        existing = _columns()
        for column in (
            "initiated_by_user_id",
            "cancel_command_id",
            "commit_command_id",
            "prepare_command_id",
            "activated_at",
            "prepared_at",
            "overlap_expires_at",
            "rotation_id",
            "state",
        ):
            if column in existing:
                batch.drop_column(column)
