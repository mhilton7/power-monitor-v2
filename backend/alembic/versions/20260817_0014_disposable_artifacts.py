"""Allow safe disposal of rejected rate-candidate working records.

Revision ID: 20260817_0014
Revises: 20260816_0013
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op

revision = "20260817_0014"
down_revision = "20260816_0013"
branch_labels = None
depends_on = None


def _candidate_guard(*, allow_disposable_delete: bool) -> str:
    delete_guard = (
        """
            IF EXISTS (
                SELECT 1 FROM rate_candidate_reviews
                 WHERE candidate_id = OLD.id AND state <> 'rejected'
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'published or reviewed rate candidate provenance is immutable',
                    CONSTRAINT = 'ck_rate_candidates_provenance_immutable';
            END IF;
            RETURN OLD;
        """
        if allow_disposable_delete
        else """
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'rate candidate provenance is immutable',
                CONSTRAINT = 'ck_rate_candidates_provenance_immutable';
        """
    )
    return f"""
        CREATE OR REPLACE FUNCTION public.pm_guard_rate_candidate_evidence()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                {delete_guard}
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


def _review_guard(*, allow_rejected_delete: bool) -> str:
    delete_guard = (
        """
            IF OLD.state <> 'rejected' THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'published or reviewed rate candidate provenance is immutable',
                    CONSTRAINT = 'ck_rate_candidate_reviews_lifecycle_immutable';
            END IF;
            RETURN OLD;
        """
        if allow_rejected_delete
        else """
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'rate candidate review provenance is immutable',
                CONSTRAINT = 'ck_rate_candidate_reviews_lifecycle_immutable';
        """
    )
    return f"""
        CREATE OR REPLACE FUNCTION public.pm_guard_rate_candidate_review_lifecycle()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                {delete_guard}
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


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_candidate_guard(allow_disposable_delete=True))
    op.execute(_review_guard(allow_rejected_delete=True))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_candidate_guard(allow_disposable_delete=False))
    op.execute(_review_guard(allow_rejected_delete=False))
