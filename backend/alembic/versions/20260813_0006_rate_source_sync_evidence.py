"""Harden official-rate source evidence and candidate provenance.

Revision ID: 20260813_0006
Revises: 20260813_0005
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260813_0006"
down_revision = "20260813_0005"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _unique_column_sets(table: str) -> set[frozenset[str]]:
    return {
        frozenset(constraint.get("column_names") or ())
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table)
    }


def _indexes(table: str) -> set[str]:
    return {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table) if index["name"]
    }


def _checks(table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table)
        if constraint["name"]
    }


def upgrade() -> None:
    source_columns = _columns("rate_sources")
    with op.batch_alter_table("rate_sources") as batch:
        if "current_etag" not in source_columns:
            batch.add_column(sa.Column("current_etag", sa.String(length=300), nullable=True))
        if "current_last_modified" not in source_columns:
            batch.add_column(
                sa.Column("current_last_modified", sa.String(length=200), nullable=True)
            )
        source_uniques = _unique_column_sets("rate_sources")
        if frozenset({"https_url"}) not in source_uniques:
            batch.create_unique_constraint(
                op.f("uq_rate_sources_https_url"),
                ["https_url"],
            )
        if "ck_rate_sources_positive_check_interval" not in _checks("rate_sources"):
            batch.create_check_constraint(
                op.f("ck_rate_sources_positive_check_interval"),
                "check_interval_hours >= 1",
            )

    run_columns = _columns("rate_sync_runs")
    added_correlation = "correlation_id" not in run_columns
    added_requested_url = "requested_url" not in run_columns
    with op.batch_alter_table("rate_sync_runs") as batch:
        if added_correlation:
            batch.add_column(sa.Column("correlation_id", sa.String(length=80), nullable=True))
        if added_requested_url:
            batch.add_column(sa.Column("requested_url", sa.String(length=500), nullable=True))
        if "final_url" not in run_columns:
            batch.add_column(sa.Column("final_url", sa.String(length=500), nullable=True))
        if "http_status" not in run_columns:
            batch.add_column(sa.Column("http_status", sa.Integer(), nullable=True))
        if "response_bytes" not in run_columns:
            batch.add_column(sa.Column("response_bytes", sa.BigInteger(), nullable=True))
        if "error_code" not in run_columns:
            batch.add_column(sa.Column("error_code", sa.String(length=100), nullable=True))
        if "evidence" not in run_columns:
            batch.add_column(sa.Column("evidence", sa.JSON(), nullable=True))
    if added_correlation:
        op.execute(sa.text("UPDATE rate_sync_runs SET correlation_id = id"))
    if added_requested_url:
        op.execute(
            sa.text(
                """
                UPDATE rate_sync_runs
                   SET requested_url = COALESCE(
                       (SELECT rate_sources.https_url
                          FROM rate_sources
                         WHERE rate_sources.id = rate_sync_runs.source_id),
                       'https://www.sce.com/'
                   )
                """
            )
        )
    if "evidence" not in run_columns:
        op.execute(sa.text("UPDATE rate_sync_runs SET evidence = '{}'"))
    if added_correlation or added_requested_url or "evidence" not in run_columns:
        with op.batch_alter_table("rate_sync_runs") as batch:
            if added_correlation:
                batch.alter_column(
                    "correlation_id",
                    existing_type=sa.String(length=80),
                    nullable=False,
                )
            if added_requested_url:
                batch.alter_column(
                    "requested_url",
                    existing_type=sa.String(length=500),
                    nullable=False,
                )
            if "evidence" not in run_columns:
                batch.alter_column("evidence", existing_type=sa.JSON(), nullable=False)
    if "ix_rate_sync_runs_correlation_id" not in _indexes("rate_sync_runs"):
        op.create_index(
            op.f("ix_rate_sync_runs_correlation_id"),
            "rate_sync_runs",
            ["correlation_id"],
            unique=False,
        )

    candidate_columns = _columns("rate_candidates")
    added_validation = "validation_evidence" not in candidate_columns
    added_created = "created_at" not in candidate_columns
    with op.batch_alter_table("rate_candidates") as batch:
        if added_validation:
            batch.add_column(sa.Column("validation_evidence", sa.JSON(), nullable=True))
        if added_created:
            batch.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
        candidate_uniques = _unique_column_sets("rate_candidates")
        if frozenset({"source_revision_id"}) not in candidate_uniques:
            batch.create_unique_constraint(
                op.f("uq_rate_candidates_source_revision_id"),
                ["source_revision_id"],
            )
    if added_validation:
        op.execute(sa.text("UPDATE rate_candidates SET validation_evidence = '{}'"))
    if added_created:
        op.execute(sa.text("UPDATE rate_candidates SET created_at = CURRENT_TIMESTAMP"))
    if added_validation or added_created:
        with op.batch_alter_table("rate_candidates") as batch:
            if added_validation:
                batch.alter_column("validation_evidence", existing_type=sa.JSON(), nullable=False)
            if added_created:
                batch.alter_column(
                    "created_at",
                    existing_type=sa.DateTime(timezone=True),
                    nullable=False,
                )

    artifact_uniques = _unique_column_sets("rate_source_artifacts")
    if frozenset({"revision_id"}) not in artifact_uniques:
        with op.batch_alter_table("rate_source_artifacts") as batch:
            batch.create_unique_constraint(
                op.f("uq_rate_source_artifacts_revision_id"),
                ["revision_id"],
            )

    if op.get_bind().dialect.name == "postgresql":
        for table in ("rate_source_revisions", "rate_source_artifacts"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION pm_reject_immutable_change()"
            )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION pm_reject_rate_candidate_evidence_change()
            RETURNS trigger AS $$
            BEGIN
              IF NEW.source_revision_id IS DISTINCT FROM OLD.source_revision_id
                 OR NEW.normalized_rates::text IS DISTINCT FROM OLD.normalized_rates::text
                 OR NEW.diff::text IS DISTINCT FROM OLD.diff::text
                 OR NEW.validation_evidence::text IS DISTINCT FROM OLD.validation_evidence::text
              THEN
                RAISE EXCEPTION 'rate candidate evidence is immutable';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute("DROP TRIGGER IF EXISTS rate_candidates_evidence_immutable ON rate_candidates")
        op.execute(
            "CREATE TRIGGER rate_candidates_evidence_immutable BEFORE UPDATE "
            "ON rate_candidates FOR EACH ROW "
            "EXECUTE FUNCTION pm_reject_rate_candidate_evidence_change()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS rate_candidates_evidence_immutable ON rate_candidates")
        op.execute("DROP FUNCTION IF EXISTS pm_reject_rate_candidate_evidence_change()")
        for table in ("rate_source_artifacts", "rate_source_revisions"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")

    artifact_uniques = _unique_column_sets("rate_source_artifacts")
    if frozenset({"revision_id"}) in artifact_uniques:
        with op.batch_alter_table("rate_source_artifacts") as batch:
            batch.drop_constraint(
                op.f("uq_rate_source_artifacts_revision_id"),
                type_="unique",
            )

    candidate_columns = _columns("rate_candidates")
    with op.batch_alter_table("rate_candidates") as batch:
        if frozenset({"source_revision_id"}) in _unique_column_sets("rate_candidates"):
            batch.drop_constraint(
                op.f("uq_rate_candidates_source_revision_id"),
                type_="unique",
            )
        if "created_at" in candidate_columns:
            batch.drop_column("created_at")
        if "validation_evidence" in candidate_columns:
            batch.drop_column("validation_evidence")

    if "ix_rate_sync_runs_correlation_id" in _indexes("rate_sync_runs"):
        op.drop_index(
            op.f("ix_rate_sync_runs_correlation_id"),
            table_name="rate_sync_runs",
        )
    run_columns = _columns("rate_sync_runs")
    with op.batch_alter_table("rate_sync_runs") as batch:
        for column in (
            "evidence",
            "error_code",
            "response_bytes",
            "http_status",
            "final_url",
            "requested_url",
            "correlation_id",
        ):
            if column in run_columns:
                batch.drop_column(column)

    source_columns = _columns("rate_sources")
    with op.batch_alter_table("rate_sources") as batch:
        if "ck_rate_sources_positive_check_interval" in _checks("rate_sources"):
            batch.drop_constraint(
                op.f("ck_rate_sources_positive_check_interval"),
                type_="check",
            )
        if frozenset({"https_url"}) in _unique_column_sets("rate_sources"):
            batch.drop_constraint(op.f("uq_rate_sources_https_url"), type_="unique")
        if "current_last_modified" in source_columns:
            batch.drop_column("current_last_modified")
        if "current_etag" in source_columns:
            batch.drop_column("current_etag")
