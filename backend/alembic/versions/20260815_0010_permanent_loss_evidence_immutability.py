"""Make authenticated permanent-loss evidence immutable.

Revision ID: 20260815_0010
Revises: 20260815_0009
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260815_0010"
down_revision = "20260815_0009"
branch_labels = None
depends_on = None

IMMUTABILITY_CONSTRAINT = "ck_unavailable_sequence_ranges_immutable"
IMMUTABILITY_MESSAGE = "authenticated permanent-loss evidence is immutable"


def _assert_postgres_raw_immutability_guard() -> None:
    definition = op.get_bind().scalar(
        sa.text(
            "SELECT pg_get_triggerdef(oid) FROM pg_catalog.pg_trigger "
            "WHERE tgrelid = 'public.raw_readings'::regclass "
            "AND tgname = 'raw_readings_immutable' "
            "AND tgenabled = 'O' AND NOT tgisinternal"
        )
    )
    normalized = " ".join(str(definition or "").lower().split())
    if (
        "before delete or update on public.raw_readings" not in normalized
        or "execute function pm_reject_immutable_change()" not in normalized
    ):
        raise RuntimeError(
            "migration 20260815_0010 cannot continue: the required raw-reading "
            "UPDATE/DELETE immutability trigger is missing, disabled, or unexpected"
        )


def _create_postgres_immutability_guard() -> None:
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION public.pm_reject_permanent_loss_evidence_change()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = '{IMMUTABILITY_MESSAGE}',
                    SCHEMA = 'public',
                    TABLE = 'unavailable_sequence_ranges',
                    CONSTRAINT = '{IMMUTABILITY_CONSTRAINT}';
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS unavailable_sequence_ranges_immutable "
            "ON public.unavailable_sequence_ranges"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER unavailable_sequence_ranges_immutable
            BEFORE UPDATE OR DELETE
            ON public.unavailable_sequence_ranges
            FOR EACH ROW
            EXECUTE FUNCTION public.pm_reject_permanent_loss_evidence_change()
            """
        )
    )


def _make_raw_overlap_guard_insert_only() -> None:
    # Raw readings already have an unconditional immutable UPDATE/DELETE
    # trigger. Keep the cross-evidence guard on INSERT, where it can add the
    # more specific overlap diagnostic without competing with immutability.
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS raw_readings_loss_overlap_guard ON public.raw_readings")
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER raw_readings_loss_overlap_guard
            BEFORE INSERT
            ON public.raw_readings
            FOR EACH ROW
            EXECUTE FUNCTION public.pm_guard_raw_reading_loss_overlap()
            """
        )
    )


def _restore_raw_overlap_guard_from_0008() -> None:
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


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    # Serialize trigger installation with all writers. PostgreSQL holds this
    # lock until the migration transaction commits, so a concurrent mutation
    # cannot pass through a gap between migration steps.
    op.execute(
        sa.text(
            "LOCK TABLE public.raw_readings, public.unavailable_sequence_ranges "
            "IN SHARE ROW EXCLUSIVE MODE"
        )
    )
    _assert_postgres_raw_immutability_guard()
    _make_raw_overlap_guard_insert_only()
    _create_postgres_immutability_guard()


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        sa.text(
            "LOCK TABLE public.raw_readings, public.unavailable_sequence_ranges "
            "IN SHARE ROW EXCLUSIVE MODE"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS unavailable_sequence_ranges_immutable "
            "ON public.unavailable_sequence_ranges"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS public.pm_reject_permanent_loss_evidence_change()"))
    _restore_raw_overlap_guard_from_0008()
