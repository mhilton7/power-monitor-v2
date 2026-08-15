from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import InvalidRequest, NotFound, RateWorkflowConflict
from ..models import (
    AuditEvent,
    Device,
    IntervalCostSelection,
    NormalizedInterval,
    RateAssignment,
    RateCandidate,
    RateCandidateReview,
    RatePeriod,
    RatePlan,
    RatePlanVersion,
    RateSource,
    RateSourceRevision,
    RateSyncRun,
    UtilityAccount,
    aware_utc,
)
from ..schemas.api import ManualRateCandidateRequest, RateCandidateReviewRequest
from .sce_rate_parser import CANDIDATE_SCHEMA

MANUAL_PARSER_VERSION = "manual-rate-entry-v1"
_DECIMAL_QUANTUM = Decimal("0.00000001")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(_DECIMAL_QUANTUM), "f")


def _manual_normalized_rates(payload: ManualRateCandidateRequest) -> dict[str, Any]:
    return {
        "schema": CANDIDATE_SCHEMA,
        "utility_name": "Southern California Edison",
        "timezone": "America/Los_Angeles",
        "currency": "USD",
        "season_definitions": {
            "summer": {"start_month": 6, "end_month": 9},
            "winter": {"start_month": 10, "end_month": 5},
        },
        "holiday_rule": "administrator_entered_schedule",
        "effective_start": payload.effective_start.astimezone(UTC).isoformat(),
        "effective_end": (
            payload.effective_end.astimezone(UTC).isoformat()
            if payload.effective_end is not None
            else None
        ),
        "effective_date_confirmation_required": True,
        "plans": [
            {
                "rate_plan_name": payload.rate_plan_name,
                "rate_class": payload.rate_class,
                "pricing_model": "time_of_use",
                "daily_fixed_charge": _decimal_text(payload.daily_fixed_charge),
                "monthly_fixed_charge": _decimal_text(payload.monthly_fixed_charge),
                "baseline_credit_per_kwh": _decimal_text(payload.baseline_credit_per_kwh),
                "rate_components": "administrator_entered_combined_price",
                "periods": [
                    {
                        "season": period.season,
                        "day_type": period.day_type,
                        "name": period.period_name,
                        "start_minute": period.start_minute,
                        "end_minute": period.end_minute,
                        "price_per_kwh": _decimal_text(period.price_per_kwh),
                        "currency": "USD",
                        "unit": "kWh",
                        "tier_min_kwh": None,
                        "tier_max_kwh": None,
                    }
                    for period in payload.periods
                ],
            }
        ],
    }


def _canonical_manual_evidence(
    payload: ManualRateCandidateRequest, normalized_rates: dict[str, Any]
) -> tuple[bytes, str]:
    evidence = {
        "origin": "manual_administrator_entry",
        "source_title": payload.source_title,
        "tariff_identifier": payload.tariff_identifier,
        "source_url": payload.source_url,
        "administrator_attests_official_source": True,
        "normalized_rates": normalized_rates,
    }
    encoded = json.dumps(
        evidence,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return encoded, hashlib.sha256(encoded).hexdigest()


async def _existing_manual_candidate(
    session: AsyncSession,
    *,
    home_id: str,
    digest: str,
) -> tuple[RateCandidate, RateSourceRevision, RateSource, RateSyncRun] | None:
    row = (
        await session.execute(
            select(RateCandidate, RateSourceRevision, RateSource, RateSyncRun)
            .join(
                RateSourceRevision,
                RateSourceRevision.id == RateCandidate.source_revision_id,
            )
            .join(RateSource, RateSource.id == RateSourceRevision.source_id)
            .join(RateSyncRun, RateSyncRun.revision_id == RateSourceRevision.id)
            .where(
                RateCandidate.home_id == home_id,
                RateCandidate.canonical_input_sha256 == digest,
                RateSyncRun.home_id == home_id,
            )
            .order_by(RateSyncRun.started_at.desc(), RateSyncRun.id.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1], row[2], row[3]


async def create_manual_rate_candidate(
    session: AsyncSession,
    *,
    payload: ManualRateCandidateRequest,
    home_id: str,
    actor_user_id: str,
    correlation_id: str,
) -> tuple[RateCandidate, RateSourceRevision, RateSource, RateSyncRun, bool]:
    normalized = _manual_normalized_rates(payload)
    canonical, digest = _canonical_manual_evidence(payload, normalized)
    existing = await _existing_manual_candidate(session, home_id=home_id, digest=digest)
    if existing is not None:
        return existing[0], existing[1], existing[2], existing[3], False

    now = _utc_now()
    try:
        async with session.begin_nested():
            source = RateSource(
                name=payload.source_title,
                source_type="manual_administrator_entry",
                https_url=None,
                enabled=False,
                check_interval_hours=168,
                last_checked_at=now,
            )
            session.add(source)
            await session.flush()
            revision = RateSourceRevision(
                source_id=source.id,
                artifact_sha256=digest,
                parser_version=MANUAL_PARSER_VERSION,
                retrieved_at=now,
            )
            session.add(revision)
            await session.flush()
            candidate = RateCandidate(
                source_revision_id=revision.id,
                home_id=home_id,
                canonical_input_sha256=digest,
                normalized_rates=normalized,
                diff={
                    "previous_candidate_id": None,
                    "before": None,
                    "after": normalized,
                    "changes": [],
                    "change_count": 0,
                },
                validation_evidence={
                    "origin": "manual_administrator_entry",
                    "parser_version": MANUAL_PARSER_VERSION,
                    "schema": CANDIDATE_SCHEMA,
                    "coverage": "complete",
                    "price_unit": "USD/kWh",
                    "source_title": payload.source_title,
                    "tariff_identifier": payload.tariff_identifier,
                    "source_url": payload.source_url,
                    "canonical_input_sha256": digest,
                    "canonical_input_bytes": len(canonical),
                    "effective_date": "administrator_review_required",
                    "provenance_confirmation": "administrator_attested_official_source",
                },
                state="review_required",
            )
            session.add(candidate)
            await session.flush()
            run = RateSyncRun(
                source_id=source.id,
                home_id=home_id,
                state="review_required",
                event_code="RATE_MANUAL_CANDIDATE_CREATED",
                started_at=now,
                completed_at=now,
                revision_id=revision.id,
                correlation_id=correlation_id[:80],
                requested_url=payload.source_url or "manual-entry:no-network-fetch",
                final_url=payload.source_url,
                response_bytes=len(canonical),
                evidence={
                    "initiator": "user",
                    "origin": "manual_administrator_entry",
                    "network_fetch_performed": False,
                    "candidate_id": candidate.id,
                    "result": "review_required",
                    "event_code": "RATE_MANUAL_CANDIDATE_CREATED",
                    "canonical_input_sha256": digest,
                },
            )
            session.add(run)
            await session.flush()
            session.add(
                AuditEvent(
                    actor_user_id=actor_user_id,
                    event_code="RATE_MANUAL_CANDIDATE_CREATED",
                    target_type="rate_candidate",
                    target_id=candidate.id,
                    correlation_id=correlation_id,
                    details={
                        "home_id": home_id,
                        "source_revision_id": revision.id,
                        "canonical_input_sha256": digest,
                    },
                )
            )
            await session.flush()
    except IntegrityError:
        existing = await _existing_manual_candidate(session, home_id=home_id, digest=digest)
        if existing is None:
            raise
        return existing[0], existing[1], existing[2], existing[3], False
    return candidate, revision, source, run, True


def selected_candidate_plan(candidate: RateCandidate, plan_name: str) -> dict[str, Any]:
    normalized = candidate.normalized_rates
    if not isinstance(normalized, dict) or normalized.get("schema") != CANDIDATE_SCHEMA:
        raise RateWorkflowConflict("rate candidate schema is not publishable")
    plans = normalized.get("plans")
    if not isinstance(plans, list):
        raise RateWorkflowConflict("rate candidate has no validated plans")
    selected = [
        plan for plan in plans if isinstance(plan, dict) and plan.get("rate_plan_name") == plan_name
    ]
    if len(selected) != 1:
        raise InvalidRequest("selected rate plan does not exist in this candidate")
    return cast(dict[str, Any], selected[0])


async def locked_rate_plan_and_next_version(
    session: AsyncSession,
    *,
    name: str,
    utility_name: str,
    rate_class: str,
) -> tuple[RatePlan, int]:
    """Return the natural-key plan under lock and reserve its next version number."""

    plan = await session.scalar(
        select(RatePlan)
        .where(
            RatePlan.name == name,
            RatePlan.utility_name == utility_name,
            RatePlan.rate_class == rate_class,
        )
        .with_for_update()
    )
    if plan is None:
        candidate = RatePlan(name=name, utility_name=utility_name, rate_class=rate_class)
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            plan = candidate
        except IntegrityError:
            plan = await session.scalar(
                select(RatePlan)
                .where(
                    RatePlan.name == name,
                    RatePlan.utility_name == utility_name,
                    RatePlan.rate_class == rate_class,
                )
                .with_for_update()
            )
            if plan is None:
                raise
    version_number = (
        int(
            await session.scalar(
                select(func.max(RatePlanVersion.version)).where(
                    RatePlanVersion.rate_plan_id == plan.id
                )
            )
            or 0
        )
        + 1
    )
    return plan, version_number


async def review_rate_candidate(
    session: AsyncSession,
    *,
    candidate: RateCandidate,
    home_id: str,
    payload: RateCandidateReviewRequest,
    actor_user_id: str,
    correlation_id: str,
) -> RateCandidateReview:
    selected_candidate_plan(candidate, payload.selected_plan_name)
    review = await session.scalar(
        select(RateCandidateReview)
        .where(
            RateCandidateReview.candidate_id == candidate.id,
            RateCandidateReview.home_id == home_id,
        )
        .with_for_update()
    )
    now = _utc_now()
    if review is None:
        review = RateCandidateReview(
            candidate_id=candidate.id,
            home_id=home_id,
            selected_plan_name=payload.selected_plan_name,
            effective_start=payload.effective_start.astimezone(UTC),
            effective_end=(
                payload.effective_end.astimezone(UTC) if payload.effective_end is not None else None
            ),
            state="reviewed",
            reviewed_by_user_id=actor_user_id,
            reviewed_at=now,
        )
        session.add(review)
    elif review.state == "reviewed":
        review.selected_plan_name = payload.selected_plan_name
        review.effective_start = payload.effective_start.astimezone(UTC)
        review.effective_end = (
            payload.effective_end.astimezone(UTC) if payload.effective_end is not None else None
        )
        review.reviewed_by_user_id = actor_user_id
        review.reviewed_at = now
    else:
        raise RateWorkflowConflict("only an unpublished candidate review can be changed")
    await session.flush()
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            event_code="RATE_CANDIDATE_REVIEWED",
            target_type="rate_candidate_review",
            target_id=review.id,
            correlation_id=correlation_id,
            details={
                "candidate_id": candidate.id,
                "home_id": home_id,
                "selected_plan_name": payload.selected_plan_name,
                "administrator_confirmed_effective_date": True,
                "administrator_confirmed_provenance": True,
            },
        )
    )
    return review


async def reject_rate_candidate(
    session: AsyncSession,
    *,
    candidate: RateCandidate,
    home_id: str,
    actor_user_id: str,
    correlation_id: str,
) -> RateCandidateReview:
    review = await session.scalar(
        select(RateCandidateReview)
        .where(
            RateCandidateReview.candidate_id == candidate.id,
            RateCandidateReview.home_id == home_id,
        )
        .with_for_update()
    )
    now = _utc_now()
    if review is None:
        review = RateCandidateReview(
            candidate_id=candidate.id,
            home_id=home_id,
            selected_plan_name=None,
            effective_start=None,
            effective_end=None,
            state="rejected",
            reviewed_by_user_id=actor_user_id,
            reviewed_at=now,
        )
        session.add(review)
    elif review.state == "reviewed":
        review.state = "rejected"
    else:
        raise RateWorkflowConflict("only an unpublished candidate can be rejected")
    await session.flush()
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            event_code="RATE_CANDIDATE_REJECTED",
            target_type="rate_candidate_review",
            target_id=review.id,
            correlation_id=correlation_id,
            details={"candidate_id": candidate.id, "home_id": home_id},
        )
    )
    return review


def _bounded_decimal(value: object, *, field: str, minimum: Decimal, maximum: Decimal) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RateWorkflowConflict(f"candidate {field} is not an exact decimal") from exc
    if not parsed.is_finite() or parsed < minimum or parsed > maximum:
        raise RateWorkflowConflict(f"candidate {field} is outside safe bounds")
    exponent = parsed.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -8:
        raise RateWorkflowConflict(f"candidate {field} exceeds eight decimal places")
    return parsed


def _expanded_periods(plan: dict[str, Any]) -> list[dict[str, Any]]:
    raw_periods = plan.get("periods")
    if not isinstance(raw_periods, list) or not raw_periods or len(raw_periods) > 200:
        raise RateWorkflowConflict("candidate periods are incomplete")
    expanded: list[dict[str, Any]] = []
    for raw in raw_periods:
        if not isinstance(raw, dict):
            raise RateWorkflowConflict("candidate period is malformed")
        season = raw.get("season")
        day_type = raw.get("day_type")
        if season not in {"summer", "winter", "all"}:
            raise RateWorkflowConflict("candidate season is unsupported")
        day_types = {
            "weekend_holiday": ("weekend", "holiday"),
            "all_days": ("all",),
        }.get(str(day_type), (str(day_type),))
        if not set(day_types) <= {"weekday", "weekend", "holiday", "all"}:
            raise RateWorkflowConflict("candidate day type is unsupported")
        name = raw.get("name")
        start = raw.get("start_minute")
        end = raw.get("end_minute")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 40
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end > 1440
            or end <= start
        ):
            raise RateWorkflowConflict("candidate period boundaries are invalid")
        price = _bounded_decimal(
            raw.get("price_per_kwh"),
            field="period price",
            minimum=Decimal("0.00000001"),
            maximum=Decimal("5"),
        )
        for expanded_day_type in day_types:
            expanded.append(
                {
                    "season": season,
                    "day_type": expanded_day_type,
                    "name": name,
                    "start_minute": start,
                    "end_minute": end,
                    "price_per_kwh": price,
                }
            )
    return expanded


def _validate_period_coverage(periods: list[dict[str, Any]]) -> None:
    for season in ("summer", "winter"):
        for day_type in ("weekday", "weekend", "holiday"):
            eligible = [
                period
                for period in periods
                if period["season"] in (season, "all") and period["day_type"] in (day_type, "all")
            ]
            if not eligible:
                raise RateWorkflowConflict("candidate schedule does not cover every day")
            specificity = max(
                int(period["season"] == season) + int(period["day_type"] == day_type)
                for period in eligible
            )
            selected = sorted(
                (
                    period
                    for period in eligible
                    if int(period["season"] == season) + int(period["day_type"] == day_type)
                    == specificity
                ),
                key=lambda period: (period["start_minute"], period["end_minute"]),
            )
            if (
                selected[0]["start_minute"] != 0
                or selected[-1]["end_minute"] != 1440
                or any(
                    first["end_minute"] != second["start_minute"]
                    for first, second in pairwise(selected)
                )
            ):
                raise RateWorkflowConflict("candidate schedule has a gap or overlap")


async def publish_rate_candidate(
    session: AsyncSession,
    *,
    candidate: RateCandidate,
    review: RateCandidateReview,
    actor_user_id: str,
    correlation_id: str,
) -> tuple[RatePlan, RatePlanVersion]:
    if review.state != "reviewed":
        raise RateWorkflowConflict("candidate review is not ready to publish")
    if review.selected_plan_name is None or review.effective_start is None:
        raise RateWorkflowConflict("candidate review evidence is incomplete")
    plan_data = selected_candidate_plan(candidate, review.selected_plan_name)
    normalized = candidate.normalized_rates
    utility_name = normalized.get("utility_name")
    timezone = normalized.get("timezone")
    currency = normalized.get("currency")
    rate_class = plan_data.get("rate_class")
    pricing_model = plan_data.get("pricing_model")
    if (
        utility_name != "Southern California Edison"
        or timezone != "America/Los_Angeles"
        or currency != "USD"
        or not isinstance(rate_class, str)
        or not rate_class
        or len(rate_class) > 80
        or pricing_model not in {"time_of_use", "time_of_use_plus_baseline_credit"}
    ):
        raise RateWorkflowConflict("candidate plan metadata is not publishable")
    periods = _expanded_periods(plan_data)
    _validate_period_coverage(periods)
    daily = _bounded_decimal(
        plan_data.get("daily_fixed_charge", "0"),
        field="daily fixed charge",
        minimum=Decimal("0"),
        maximum=Decimal("20"),
    )
    monthly = _bounded_decimal(
        plan_data.get("monthly_fixed_charge", "0"),
        field="monthly fixed charge",
        minimum=Decimal("0"),
        maximum=Decimal("500"),
    )
    baseline = _bounded_decimal(
        plan_data.get("baseline_credit_per_kwh", "0"),
        field="baseline credit",
        minimum=Decimal("0"),
        maximum=Decimal("1"),
    )
    plan_name = review.selected_plan_name
    plan, version_number = await locked_rate_plan_and_next_version(
        session,
        name=plan_name,
        utility_name=utility_name,
        rate_class=rate_class,
    )
    revision = await session.get(RateSourceRevision, candidate.source_revision_id)
    if revision is None:
        raise RateWorkflowConflict("candidate provenance revision is missing")
    now = _utc_now()
    version = RatePlanVersion(
        rate_plan_id=plan.id,
        version=version_number,
        effective_start=review.effective_start,
        effective_end=review.effective_end,
        timezone="America/Los_Angeles",
        pricing_model=pricing_model,
        daily_fixed_charge=daily,
        monthly_fixed_charge=monthly,
        baseline_credit_per_kwh=baseline,
        source_hash=revision.artifact_sha256,
        algorithm_version="cost-v1",
        state="draft",
        published_by_user_id=actor_user_id,
        published_at=now,
    )
    session.add(version)
    await session.flush()
    for period in periods:
        session.add(
            RatePeriod(
                rate_plan_version_id=version.id,
                season=period["season"],
                day_type=period["day_type"],
                period_name=period["name"],
                start_minute=period["start_minute"],
                end_minute=period["end_minute"],
                price_per_kwh=period["price_per_kwh"],
            )
        )
    await session.flush()
    version.state = "published"
    review.state = "published"
    review.rate_plan_version_id = version.id
    review.published_at = now
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            event_code="RATE_VERSION_PUBLISHED_FROM_CANDIDATE",
            target_type="rate_plan_version",
            target_id=version.id,
            correlation_id=correlation_id,
            details={
                "candidate_id": candidate.id,
                "review_id": review.id,
                "home_id": review.home_id,
                "source_artifact_sha256": revision.artifact_sha256,
            },
        )
    )
    return plan, version


async def replace_rate_assignment(
    session: AsyncSession,
    *,
    account: UtilityAccount,
    version: RatePlanVersion,
    actor_user_id: str,
) -> tuple[RateAssignment, bool]:
    """Insert a non-overlapping assignment while preserving later scheduled versions."""

    locked_account = await session.scalar(
        select(UtilityAccount).where(UtilityAccount.id == account.id).with_for_update()
    )
    if locked_account is None:
        raise NotFound("utility account does not exist")
    assignments = (
        await session.scalars(
            select(RateAssignment)
            .where(RateAssignment.utility_account_id == locked_account.id)
            .order_by(RateAssignment.effective_start, RateAssignment.id)
            .with_for_update()
        )
    ).all()
    version_start = aware_utc(version.effective_start)
    version_end = aware_utc(version.effective_end) if version.effective_end is not None else None
    exact = next(
        (
            assignment
            for assignment in assignments
            if aware_utc(assignment.effective_start) == version_start
        ),
        None,
    )
    if exact is not None:
        if exact.rate_plan_version_id == version.id:
            return exact, False
        raise RateWorkflowConflict("another rate version already starts at this exact instant")
    for assignment in assignments:
        assignment_start = aware_utc(assignment.effective_start)
        assignment_end = (
            aware_utc(assignment.effective_end) if assignment.effective_end is not None else None
        )
        if assignment_start < version_start and (
            assignment_end is None or assignment_end > version_start
        ):
            assignment.effective_end = version_start
    future_starts = sorted(
        aware_utc(assignment.effective_start)
        for assignment in assignments
        if aware_utc(assignment.effective_start) > version_start
    )
    effective_end = version_end
    if future_starts and (effective_end is None or effective_end > future_starts[0]):
        effective_end = future_starts[0]
    assignment = RateAssignment(
        utility_account_id=locked_account.id,
        rate_plan_version_id=version.id,
        effective_start=version_start,
        effective_end=effective_end,
        assigned_by_user_id=actor_user_id,
    )
    session.add(assignment)
    await session.flush()
    return assignment, True


async def activate_rate_candidate(
    session: AsyncSession,
    *,
    candidate: RateCandidate,
    review: RateCandidateReview,
    utility_account_id: str,
    actor_user_id: str,
    correlation_id: str,
) -> RateAssignment:
    if review.state != "published" or review.rate_plan_version_id is None:
        raise RateWorkflowConflict("candidate version is not ready to activate")
    account = await session.scalar(
        select(UtilityAccount)
        .where(
            UtilityAccount.id == utility_account_id,
            UtilityAccount.home_id == review.home_id,
        )
        .with_for_update()
    )
    if account is None:
        raise NotFound("utility account does not exist")
    version = await session.get(RatePlanVersion, review.rate_plan_version_id)
    if version is None or version.state != "published":
        raise RateWorkflowConflict("published rate-plan version is missing")
    assignment, _created = await replace_rate_assignment(
        session,
        account=account,
        version=version,
        actor_user_id=actor_user_id,
    )
    affected_conditions = [
        Device.home_id == account.home_id,
        NormalizedInterval.start_utc >= version.effective_start,
    ]
    if assignment.effective_end is not None:
        affected_conditions.append(NormalizedInterval.end_utc <= assignment.effective_end)
    affected_intervals = (
        select(NormalizedInterval.id)
        .join(Device, Device.id == NormalizedInterval.device_id)
        .where(*affected_conditions)
    )
    await session.execute(
        delete(IntervalCostSelection).where(
            IntervalCostSelection.normalized_interval_id.in_(affected_intervals)
        )
    )
    review.state = "activated"
    review.utility_account_id = account.id
    review.activated_at = _utc_now()
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            event_code="RATE_VERSION_ACTIVATED_FROM_CANDIDATE",
            target_type="rate_assignment",
            target_id=assignment.id,
            correlation_id=correlation_id,
            details={
                "candidate_id": candidate.id,
                "review_id": review.id,
                "home_id": review.home_id,
                "rate_plan_version_id": version.id,
                "utility_account_id": account.id,
            },
        )
    )
    return assignment


async def exact_home_candidate(
    session: AsyncSession,
    *,
    candidate_id: str,
    home_id: str,
    for_update: bool = False,
) -> tuple[RateCandidate, RateSourceRevision, RateSource]:
    statement = (
        select(RateCandidate, RateSourceRevision, RateSource)
        .join(
            RateSourceRevision,
            RateSourceRevision.id == RateCandidate.source_revision_id,
        )
        .join(RateSource, RateSource.id == RateSourceRevision.source_id)
        .where(
            RateCandidate.id == candidate_id,
            select(RateSyncRun.id)
            .where(
                RateSyncRun.home_id == home_id,
                RateSyncRun.revision_id == RateSourceRevision.id,
            )
            .exists(),
        )
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).first()
    if row is None:
        raise NotFound("rate candidate does not exist")
    return row[0], row[1], row[2]


def safe_review(review: RateCandidateReview | None) -> dict[str, object]:
    if review is None:
        return {"state": "review_required"}
    return {
        "id": review.id,
        "state": review.state,
        "selected_plan_name": review.selected_plan_name,
        "effective_start": review.effective_start,
        "effective_end": review.effective_end,
        "reviewed_at": review.reviewed_at,
        "published_at": review.published_at,
        "activated_at": review.activated_at,
        "rate_plan_version_id": review.rate_plan_version_id,
        "utility_account_id": review.utility_account_id,
    }


__all__ = [
    "activate_rate_candidate",
    "create_manual_rate_candidate",
    "exact_home_candidate",
    "locked_rate_plan_and_next_version",
    "publish_rate_candidate",
    "reject_rate_candidate",
    "replace_rate_assignment",
    "review_rate_candidate",
    "safe_review",
    "selected_candidate_plan",
]
