"""Enforce reading completeness and permanent-loss evidence integrity.

Revision ID: 20260815_0008
Revises: 20260813_0007
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260815_0008"
down_revision = "20260813_0007"
branch_labels = None
depends_on = None

SAMPLE_COUNT_CHECK = (
    "sample_count >= 0 AND expected_sample_count > 0 AND sample_count <= expected_sample_count"
)
LEGACY_SAMPLE_COUNT_CHECK = "sample_count >= 0 AND expected_sample_count > 0"
BILL_ARTIFACT_CHECK = "encrypted_artifact_path IS NULL"


def _checks(table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table)
        if constraint.get("name")
    }


def _replace_sample_count_check(expression: str) -> None:
    checks = _checks("raw_readings")
    with op.batch_alter_table("raw_readings") as batch:
        if "ck_raw_readings_sample_count" in checks:
            batch.drop_constraint(op.f("ck_raw_readings_sample_count"), type_="check")
        batch.create_check_constraint(op.f("ck_raw_readings_sample_count"), expression)


def _replace_bill_artifact_check(expression: str | None) -> None:
    name = "ck_utility_bill_rate_uploads_no_original_artifact"
    checks = _checks("utility_bill_rate_uploads")
    with op.batch_alter_table("utility_bill_rate_uploads") as batch:
        if name in checks:
            batch.drop_constraint(op.f(name), type_="check")
        if expression is not None:
            batch.create_check_constraint(op.f(name), expression)


def _create_postgres_ingestion_guards() -> None:
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION public.pm_guard_raw_reading_loss_overlap()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                PERFORM 1
                  FROM public.devices AS device
                 WHERE device.id = NEW.device_id
                   FOR UPDATE;

                IF EXISTS (
                    SELECT 1
                      FROM public.unavailable_sequence_ranges AS loss
                     WHERE loss.device_id = NEW.device_id
                       AND NEW.sequence BETWEEN loss.first_sequence AND loss.last_sequence
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'raw reading overlaps authenticated permanent-loss evidence',
                        CONSTRAINT = 'ck_raw_readings_no_permanent_loss_overlap';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION public.pm_guard_permanent_loss_overlap()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                PERFORM 1
                  FROM public.devices AS device
                 WHERE device.id = NEW.device_id
                   FOR UPDATE;

                IF EXISTS (
                    SELECT 1
                      FROM public.raw_readings AS reading
                     WHERE reading.device_id = NEW.device_id
                       AND reading.sequence BETWEEN NEW.first_sequence AND NEW.last_sequence
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'permanent-loss evidence overlaps a committed raw reading',
                        CONSTRAINT = 'ck_unavailable_sequence_ranges_no_raw_overlap';
                END IF;

                IF EXISTS (
                    SELECT 1
                     FROM public.unavailable_sequence_ranges AS prior
                     WHERE prior.device_id = NEW.device_id
                       AND prior.id IS DISTINCT FROM NEW.id
                       AND prior.first_sequence <= NEW.last_sequence
                       AND prior.last_sequence >= NEW.first_sequence
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'permanent-loss evidence ranges overlap',
                        CONSTRAINT = 'ck_unavailable_sequence_ranges_no_range_overlap';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS raw_readings_loss_overlap_guard ON public.raw_readings")
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER raw_readings_loss_overlap_guard
            BEFORE INSERT OR UPDATE OF device_id, sequence
            ON public.raw_readings
            FOR EACH ROW
            EXECUTE FUNCTION public.pm_guard_raw_reading_loss_overlap()
            """
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS unavailable_sequence_ranges_overlap_guard "
            "ON public.unavailable_sequence_ranges"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER unavailable_sequence_ranges_overlap_guard
            BEFORE INSERT OR UPDATE OF device_id, first_sequence, last_sequence
            ON public.unavailable_sequence_ranges
            FOR EACH ROW
            EXECUTE FUNCTION public.pm_guard_permanent_loss_overlap()
            """
        )
    )


def _assert_no_existing_postgres_ingestion_conflicts() -> None:
    connection = op.get_bind()
    raw_loss_conflict = connection.scalar(
        sa.text(
            """
            SELECT 1
              FROM public.raw_readings AS reading
              JOIN public.unavailable_sequence_ranges AS loss
                ON loss.device_id = reading.device_id
               AND reading.sequence BETWEEN loss.first_sequence AND loss.last_sequence
             LIMIT 1
            """
        )
    )
    if raw_loss_conflict is not None:
        raise RuntimeError(
            "migration 20260815_0008 cannot continue: existing raw readings overlap "
            "authenticated permanent-loss evidence"
        )

    loss_range_conflict = connection.scalar(
        sa.text(
            """
            SELECT 1
              FROM public.unavailable_sequence_ranges AS earlier
              JOIN public.unavailable_sequence_ranges AS later
                ON later.device_id = earlier.device_id
               AND later.id > earlier.id
               AND earlier.first_sequence <= later.last_sequence
               AND earlier.last_sequence >= later.first_sequence
             LIMIT 1
            """
        )
    )
    if loss_range_conflict is not None:
        raise RuntimeError(
            "migration 20260815_0008 cannot continue: existing authenticated "
            "permanent-loss evidence ranges overlap"
        )


def _assert_no_retained_original_bill_artifacts() -> None:
    retained = op.get_bind().scalar(
        sa.text(
            "SELECT 1 FROM utility_bill_rate_uploads "
            "WHERE encrypted_artifact_path IS NOT NULL LIMIT 1"
        )
    )
    if retained is not None:
        raise RuntimeError(
            "migration 20260815_0008 cannot continue: the database references "
            "retained original bill documents; remove those prohibited artifacts "
            "with a reviewed privacy-preserving procedure before retrying"
        )


def upgrade() -> None:
    postgresql = op.get_bind().dialect.name == "postgresql"
    if postgresql:
        # Hold every preflighted table stable until the constraints and trigger
        # DDL commit. SHARE ROW EXCLUSIVE conflicts with ordinary INSERT/UPDATE/
        # DELETE RowExclusive locks but still permits read-only inspection.
        op.execute(
            sa.text(
                "LOCK TABLE public.raw_readings, public.unavailable_sequence_ranges, "
                "public.utility_bill_rate_uploads "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
        )
    _replace_sample_count_check(SAMPLE_COUNT_CHECK)
    _assert_no_retained_original_bill_artifacts()
    _replace_bill_artifact_check(BILL_ARTIFACT_CHECK)
    if postgresql:
        _assert_no_existing_postgres_ingestion_conflicts()
        _create_postgres_ingestion_guards()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS unavailable_sequence_ranges_overlap_guard "
                "ON public.unavailable_sequence_ranges"
            )
        )
        op.execute(
            sa.text("DROP TRIGGER IF EXISTS raw_readings_loss_overlap_guard ON public.raw_readings")
        )
        op.execute(sa.text("DROP FUNCTION IF EXISTS public.pm_guard_permanent_loss_overlap()"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS public.pm_guard_raw_reading_loss_overlap()"))
    _replace_bill_artifact_check(None)
    _replace_sample_count_check(LEGACY_SAMPLE_COUNT_CHECK)
