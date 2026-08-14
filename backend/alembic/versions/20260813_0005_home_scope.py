"""Bind operational and bill-rate records to an authorized home.

Revision ID: 20260813_0005
Revises: 20260813_0004
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260813_0005"
down_revision = "20260813_0004"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table) if index["name"]
    }


def _foreign_keys(table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_foreign_keys(table)
        if constraint["name"]
    }


def _add_nullable_home_scope(table: str) -> None:
    if "home_id" in _columns(table):
        return
    with op.batch_alter_table(table) as batch:
        batch.add_column(sa.Column("home_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            f"fk_{table}_home_id_homes",
            "homes",
            ["home_id"],
            ["id"],
            ondelete="CASCADE",
        )
    index_name = f"ix_{table}_home_id"
    if index_name not in _indexes(table):
        op.create_index(index_name, table, ["home_id"], unique=False)


def upgrade() -> None:
    _add_nullable_home_scope("rate_sync_runs")
    _add_nullable_home_scope("application_logs")

    upload_table = "utility_bill_rate_uploads"
    added_upload_home = "home_id" not in _columns(upload_table)
    if added_upload_home:
        with op.batch_alter_table(upload_table) as batch:
            batch.add_column(sa.Column("home_id", sa.String(length=36), nullable=True))
            batch.create_foreign_key(
                "fk_utility_bill_rate_uploads_home_id_homes",
                "homes",
                ["home_id"],
                ["id"],
                ondelete="CASCADE",
            )
        # A legacy draft inherits its uploader's home only when that mapping is
        # unambiguous. Multi-home and unscoped uploaders fail closed instead of
        # assigning sensitive lineage to a guessed home.
        op.execute(
            sa.text(
                """
                UPDATE utility_bill_rate_uploads
                   SET home_id = (
                       SELECT MIN(user_home_scopes.home_id)
                         FROM user_home_scopes
                        WHERE user_home_scopes.user_id =
                              utility_bill_rate_uploads.uploaded_by_user_id
                   )
                 WHERE home_id IS NULL
                   AND 1 = (
                       SELECT COUNT(*)
                         FROM user_home_scopes
                        WHERE user_home_scopes.user_id =
                              utility_bill_rate_uploads.uploaded_by_user_id
                   )
                """
            )
        )
        unowned = op.get_bind().scalar(
            sa.text("SELECT COUNT(*) FROM utility_bill_rate_uploads WHERE home_id IS NULL")
        )
        if int(unowned or 0):
            raise RuntimeError(
                "bill-rate uploads with no uploader home scope must be assigned before migration"
            )

    unique_constraints = sa.inspect(op.get_bind()).get_unique_constraints(upload_table)
    old_global_names = [
        constraint["name"]
        for constraint in unique_constraints
        if constraint.get("column_names") == ["artifact_sha256"] and constraint["name"]
    ]
    has_home_artifact_unique = any(
        set(constraint.get("column_names") or ()) == {"home_id", "artifact_sha256"}
        for constraint in unique_constraints
    )
    if added_upload_home or old_global_names or not has_home_artifact_unique:
        with op.batch_alter_table(upload_table) as batch:
            if added_upload_home:
                batch.alter_column("home_id", existing_type=sa.String(length=36), nullable=False)
            for constraint_name in old_global_names:
                batch.drop_constraint(constraint_name, type_="unique")
            if not has_home_artifact_unique:
                batch.create_unique_constraint(
                    "uq_bill_rate_upload_home_artifact",
                    ["home_id", "artifact_sha256"],
                )
    if "ix_utility_bill_rate_uploads_home_id" not in _indexes(upload_table):
        op.create_index(
            "ix_utility_bill_rate_uploads_home_id",
            upload_table,
            ["home_id"],
            unique=False,
        )


def _drop_home_scope(table: str) -> None:
    if "home_id" not in _columns(table):
        return
    index_name = f"ix_{table}_home_id"
    if index_name in _indexes(table):
        op.drop_index(index_name, table_name=table)
    foreign_key_name = f"fk_{table}_home_id_homes"
    with op.batch_alter_table(table) as batch:
        if foreign_key_name in _foreign_keys(table):
            batch.drop_constraint(foreign_key_name, type_="foreignkey")
        batch.drop_column("home_id")


def downgrade() -> None:
    upload_table = "utility_bill_rate_uploads"
    if "home_id" in _columns(upload_table):
        duplicates = op.get_bind().scalar(
            sa.text(
                """
                SELECT COUNT(*) FROM (
                    SELECT artifact_sha256
                      FROM utility_bill_rate_uploads
                     GROUP BY artifact_sha256
                    HAVING COUNT(*) > 1
                ) AS duplicate_hashes
                """
            )
        )
        if int(duplicates or 0):
            raise RuntimeError(
                "cannot downgrade while the same artifact hash is owned by multiple homes"
            )
        if "ix_utility_bill_rate_uploads_home_id" in _indexes(upload_table):
            op.drop_index("ix_utility_bill_rate_uploads_home_id", table_name=upload_table)
        unique_constraints = sa.inspect(op.get_bind()).get_unique_constraints(upload_table)
        home_unique_names = [
            constraint["name"]
            for constraint in unique_constraints
            if set(constraint.get("column_names") or ()) == {"home_id", "artifact_sha256"}
            and constraint["name"]
        ]
        with op.batch_alter_table(upload_table) as batch:
            for constraint_name in home_unique_names:
                batch.drop_constraint(constraint_name, type_="unique")
            batch.create_unique_constraint(
                "uq_utility_bill_rate_uploads_artifact_sha256", ["artifact_sha256"]
            )
            if "fk_utility_bill_rate_uploads_home_id_homes" in _foreign_keys(upload_table):
                batch.drop_constraint(
                    "fk_utility_bill_rate_uploads_home_id_homes", type_="foreignkey"
                )
            batch.drop_column("home_id")

    _drop_home_scope("application_logs")
    _drop_home_scope("rate_sync_runs")
