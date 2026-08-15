"""Enforce rate workflow identity, lifecycle, and assignment integrity.

Revision ID: 20260815_0011
Revises: 20260815_0010
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260815_0011"
down_revision = "20260815_0010"
branch_labels = None
depends_on = None

REVIEW_STATE_EVIDENCE = """
(state = 'reviewed' AND selected_plan_name IS NOT NULL AND effective_start IS NOT NULL
 AND rate_plan_version_id IS NULL
 AND utility_account_id IS NULL AND published_at IS NULL AND activated_at IS NULL)
OR
(state = 'published' AND selected_plan_name IS NOT NULL AND effective_start IS NOT NULL
 AND rate_plan_version_id IS NOT NULL
 AND utility_account_id IS NULL AND published_at IS NOT NULL AND activated_at IS NULL)
OR
(state = 'activated' AND selected_plan_name IS NOT NULL AND effective_start IS NOT NULL
 AND rate_plan_version_id IS NOT NULL
 AND utility_account_id IS NOT NULL AND published_at IS NOT NULL AND activated_at IS NOT NULL)
OR
(state = 'rejected' AND rate_plan_version_id IS NULL
 AND utility_account_id IS NULL AND published_at IS NULL AND activated_at IS NULL)
"""


def _scalar(statement: str) -> object | None:
    return op.get_bind().execute(sa.text(statement)).scalar()


def _preflight() -> None:
    if (
        _scalar(
            "SELECT 1 FROM rate_plans GROUP BY name, utility_name, rate_class "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
        is not None
    ):
        raise RuntimeError("duplicate natural rate-plan identities require manual repair")
    if (
        _scalar(
            "SELECT 1 FROM rate_assignments a JOIN rate_assignments b "
            "ON a.utility_account_id = b.utility_account_id AND a.id < b.id "
            "AND (b.effective_end IS NULL OR a.effective_start < b.effective_end) "
            "AND (a.effective_end IS NULL OR b.effective_start < a.effective_end) LIMIT 1"
        )
        is not None
    ):
        raise RuntimeError("overlapping rate assignments require manual repair")
    if (
        _scalar(
            "SELECT 1 FROM rate_assignments WHERE effective_end IS NOT NULL "
            "AND effective_end <= effective_start LIMIT 1"
        )
        is not None
    ):
        raise RuntimeError("invalid rate-assignment ranges require manual repair")
    if (
        _scalar(
            "SELECT 1 FROM rate_candidate_reviews WHERE NOT ("  # noqa: S608
            + REVIEW_STATE_EVIDENCE
            + ") LIMIT 1"
        )
        is not None
    ):
        raise RuntimeError("inconsistent rate-candidate review evidence requires manual repair")


def _create_postgres_guards() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.pm_guard_rate_candidate_evidence()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'rate candidate provenance is immutable',
                    CONSTRAINT = 'ck_rate_candidates_provenance_immutable';
            END IF;
            IF NEW.source_revision_id IS DISTINCT FROM OLD.source_revision_id
               OR NEW.normalized_rates::text IS DISTINCT FROM OLD.normalized_rates::text
               OR NEW.diff::text IS DISTINCT FROM OLD.diff::text
               OR NEW.validation_evidence::text IS DISTINCT FROM OLD.validation_evidence::text
               OR NEW.home_id IS DISTINCT FROM OLD.home_id
               OR NEW.canonical_input_sha256 IS DISTINCT FROM OLD.canonical_input_sha256
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'rate candidate provenance is immutable',
                    CONSTRAINT = 'ck_rate_candidates_provenance_immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER IF EXISTS rate_candidates_evidence_immutable ON rate_candidates")
    op.execute(
        "CREATE TRIGGER rate_candidates_evidence_immutable BEFORE UPDATE OR DELETE "
        "ON rate_candidates FOR EACH ROW "
        "EXECUTE FUNCTION public.pm_guard_rate_candidate_evidence()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.pm_guard_rate_candidate_review_lifecycle()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'rate candidate review provenance is immutable',
                    CONSTRAINT = 'ck_rate_candidate_reviews_lifecycle_immutable';
            END IF;
            IF NEW.candidate_id IS DISTINCT FROM OLD.candidate_id
               OR NEW.home_id IS DISTINCT FROM OLD.home_id
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'rate candidate review identity is immutable',
                    CONSTRAINT = 'ck_rate_candidate_reviews_lifecycle_immutable';
            END IF;
            IF OLD.state = 'reviewed' AND NEW.state = 'reviewed' THEN
                IF NEW.rate_plan_version_id IS DISTINCT FROM OLD.rate_plan_version_id
                   OR NEW.utility_account_id IS DISTINCT FROM OLD.utility_account_id
                   OR NEW.published_at IS DISTINCT FROM OLD.published_at
                   OR NEW.activated_at IS DISTINCT FROM OLD.activated_at
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'review linkage cannot change before publication',
                        CONSTRAINT = 'ck_rate_candidate_reviews_lifecycle_immutable';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.state = 'reviewed' AND NEW.state = 'published' THEN
                IF NEW.selected_plan_name IS DISTINCT FROM OLD.selected_plan_name
                   OR NEW.effective_start IS DISTINCT FROM OLD.effective_start
                   OR NEW.effective_end IS DISTINCT FROM OLD.effective_end
                   OR NEW.reviewed_by_user_id IS DISTINCT FROM OLD.reviewed_by_user_id
                   OR NEW.reviewed_at IS DISTINCT FROM OLD.reviewed_at
                   OR NEW.utility_account_id IS DISTINCT FROM OLD.utility_account_id
                   OR NEW.activated_at IS DISTINCT FROM OLD.activated_at
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'review provenance changed during publication',
                        CONSTRAINT = 'ck_rate_candidate_reviews_lifecycle_immutable';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.state = 'published' AND NEW.state = 'activated' THEN
                IF NEW.candidate_id IS DISTINCT FROM OLD.candidate_id
                   OR NEW.home_id IS DISTINCT FROM OLD.home_id
                   OR NEW.selected_plan_name IS DISTINCT FROM OLD.selected_plan_name
                   OR NEW.effective_start IS DISTINCT FROM OLD.effective_start
                   OR NEW.effective_end IS DISTINCT FROM OLD.effective_end
                   OR NEW.reviewed_by_user_id IS DISTINCT FROM OLD.reviewed_by_user_id
                   OR NEW.reviewed_at IS DISTINCT FROM OLD.reviewed_at
                   OR NEW.rate_plan_version_id IS DISTINCT FROM OLD.rate_plan_version_id
                   OR NEW.published_at IS DISTINCT FROM OLD.published_at
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'published review provenance changed during activation',
                        CONSTRAINT = 'ck_rate_candidate_reviews_lifecycle_immutable';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.state = 'reviewed' AND NEW.state = 'rejected' THEN
                IF NEW.candidate_id IS DISTINCT FROM OLD.candidate_id
                   OR NEW.home_id IS DISTINCT FROM OLD.home_id
                   OR NEW.selected_plan_name IS DISTINCT FROM OLD.selected_plan_name
                   OR NEW.effective_start IS DISTINCT FROM OLD.effective_start
                   OR NEW.effective_end IS DISTINCT FROM OLD.effective_end
                   OR NEW.reviewed_by_user_id IS DISTINCT FROM OLD.reviewed_by_user_id
                   OR NEW.reviewed_at IS DISTINCT FROM OLD.reviewed_at
                   OR NEW.rate_plan_version_id IS DISTINCT FROM OLD.rate_plan_version_id
                   OR NEW.utility_account_id IS DISTINCT FROM OLD.utility_account_id
                   OR NEW.published_at IS DISTINCT FROM OLD.published_at
                   OR NEW.activated_at IS DISTINCT FROM OLD.activated_at
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'review provenance changed during rejection',
                        CONSTRAINT = 'ck_rate_candidate_reviews_lifecycle_immutable';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'illegal rate candidate review lifecycle transition',
                CONSTRAINT = 'ck_rate_candidate_reviews_lifecycle_immutable';
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS rate_candidate_reviews_lifecycle_immutable "
        "ON rate_candidate_reviews"
    )
    op.execute(
        "CREATE TRIGGER rate_candidate_reviews_lifecycle_immutable BEFORE UPDATE OR DELETE "
        "ON rate_candidate_reviews FOR EACH ROW "
        "EXECUTE FUNCTION public.pm_guard_rate_candidate_review_lifecycle()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.pm_guard_rate_assignment_overlap()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            PERFORM 1 FROM public.utility_accounts
             WHERE id = NEW.utility_account_id FOR UPDATE;
            IF EXISTS (
                SELECT 1 FROM public.rate_assignments AS existing
                 WHERE existing.utility_account_id = NEW.utility_account_id
                   AND existing.id <> NEW.id
                   AND (NEW.effective_end IS NULL
                        OR existing.effective_start < NEW.effective_end)
                   AND (existing.effective_end IS NULL
                        OR NEW.effective_start < existing.effective_end)
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'rate assignments cannot overlap',
                    CONSTRAINT = 'ck_rate_assignments_no_overlap';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER IF EXISTS rate_assignments_no_overlap ON rate_assignments")
    op.execute(
        "CREATE TRIGGER rate_assignments_no_overlap BEFORE INSERT OR UPDATE "
        "ON rate_assignments FOR EACH ROW "
        "EXECUTE FUNCTION public.pm_guard_rate_assignment_overlap()"
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # Keep the preflight and the constraint/trigger installation in one
        # write-blocking window.  Otherwise a concurrent writer could commit an
        # invalid row after preflight but before its corresponding guard exists.
        op.execute(
            "LOCK TABLE rate_plans, rate_assignments, rate_candidate_reviews, "
            "rate_candidates IN ACCESS EXCLUSIVE MODE"
        )
    _preflight()
    with op.batch_alter_table("rate_candidates") as batch:
        batch.add_column(sa.Column("home_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("canonical_input_sha256", sa.String(length=64), nullable=True))
        batch.create_foreign_key(
            op.f("fk_rate_candidates_home_id_homes"),
            "homes",
            ["home_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            op.f("uq_rate_candidates_home_id"),
            ["home_id", "canonical_input_sha256"],
        )
        batch.create_check_constraint(
            op.f("ck_rate_candidates_manual_identity_pair"),
            "(home_id IS NULL AND canonical_input_sha256 IS NULL) OR "
            "(home_id IS NOT NULL AND canonical_input_sha256 IS NOT NULL)",
        )
    op.create_index(
        op.f("ix_rate_candidates_home_id"),
        "rate_candidates",
        ["home_id"],
        unique=False,
    )
    with op.batch_alter_table("rate_candidate_reviews") as batch:
        batch.alter_column(
            "selected_plan_name",
            existing_type=sa.String(length=120),
            nullable=True,
        )
        batch.alter_column(
            "effective_start",
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
        )
        batch.drop_constraint(
            op.f("fk_rate_candidate_reviews_candidate_id_rate_candidates"),
            type_="foreignkey",
        )
        batch.drop_constraint(
            op.f("fk_rate_candidate_reviews_home_id_homes"),
            type_="foreignkey",
        )
        batch.create_foreign_key(
            op.f("fk_rate_candidate_reviews_candidate_id_rate_candidates"),
            "rate_candidates",
            ["candidate_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            op.f("fk_rate_candidate_reviews_home_id_homes"),
            "homes",
            ["home_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            op.f("ck_rate_candidate_reviews_state_evidence"),
            REVIEW_STATE_EVIDENCE,
        )
    with op.batch_alter_table("rate_plans") as batch:
        batch.create_unique_constraint(
            op.f("uq_rate_plans_name"),
            ["name", "utility_name", "rate_class"],
        )
    with op.batch_alter_table("rate_assignments") as batch:
        batch.create_unique_constraint(
            op.f("uq_rate_assignments_utility_account_id"),
            ["utility_account_id", "effective_start"],
        )
        batch.create_check_constraint(
            op.f("ck_rate_assignments_effective_range"),
            "effective_end IS NULL OR effective_end > effective_start",
        )
    if op.get_bind().dialect.name == "postgresql":
        _create_postgres_guards()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS rate_assignments_no_overlap ON rate_assignments")
        op.execute("DROP FUNCTION IF EXISTS public.pm_guard_rate_assignment_overlap()")
        op.execute(
            "DROP TRIGGER IF EXISTS rate_candidate_reviews_lifecycle_immutable "
            "ON rate_candidate_reviews"
        )
        op.execute("DROP FUNCTION IF EXISTS public.pm_guard_rate_candidate_review_lifecycle()")
        op.execute("DROP TRIGGER IF EXISTS rate_candidates_evidence_immutable ON rate_candidates")
        op.execute("DROP FUNCTION IF EXISTS public.pm_guard_rate_candidate_evidence()")
    with op.batch_alter_table("rate_assignments") as batch:
        batch.drop_constraint(
            op.f("ck_rate_assignments_effective_range"),
            type_="check",
        )
        batch.drop_constraint(
            op.f("uq_rate_assignments_utility_account_id"),
            type_="unique",
        )
    with op.batch_alter_table("rate_plans") as batch:
        batch.drop_constraint(op.f("uq_rate_plans_name"), type_="unique")
    review_check_names = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(
            "rate_candidate_reviews"
        )
    }
    with op.batch_alter_table("rate_candidate_reviews") as batch:
        # SQLite cannot reflect this compound, multi-line CHECK on all supported
        # releases.  In batch mode an unreflected CHECK is discarded by the
        # table recreation, so only request an explicit drop when it is visible.
        if op.f("ck_rate_candidate_reviews_state_evidence") in review_check_names:
            batch.drop_constraint(
                op.f("ck_rate_candidate_reviews_state_evidence"),
                type_="check",
            )
        batch.drop_constraint(
            op.f("fk_rate_candidate_reviews_candidate_id_rate_candidates"),
            type_="foreignkey",
        )
        batch.drop_constraint(
            op.f("fk_rate_candidate_reviews_home_id_homes"),
            type_="foreignkey",
        )
        batch.create_foreign_key(
            op.f("fk_rate_candidate_reviews_candidate_id_rate_candidates"),
            "rate_candidates",
            ["candidate_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.alter_column(
            "effective_start",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch.alter_column(
            "selected_plan_name",
            existing_type=sa.String(length=120),
            nullable=False,
        )
        batch.create_foreign_key(
            op.f("fk_rate_candidate_reviews_home_id_homes"),
            "homes",
            ["home_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.drop_index(op.f("ix_rate_candidates_home_id"), table_name="rate_candidates")
    with op.batch_alter_table("rate_candidates") as batch:
        batch.drop_constraint(op.f("ck_rate_candidates_manual_identity_pair"), type_="check")
        batch.drop_constraint(op.f("uq_rate_candidates_home_id"), type_="unique")
        batch.drop_constraint(op.f("fk_rate_candidates_home_id_homes"), type_="foreignkey")
        batch.drop_column("canonical_input_sha256")
        batch.drop_column("home_id")
    if op.get_bind().dialect.name == "postgresql":
        # Restore the pre-0011 update-only candidate evidence guard.
        op.execute(
            """
            CREATE OR REPLACE FUNCTION public.pm_reject_rate_candidate_evidence_change()
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
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            "CREATE TRIGGER rate_candidates_evidence_immutable BEFORE UPDATE "
            "ON rate_candidates FOR EACH ROW "
            "EXECUTE FUNCTION public.pm_reject_rate_candidate_evidence_change()"
        )
