from __future__ import annotations

import hashlib
import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

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
    RateDatedPrice,
    RateHoliday,
    RatePeriod,
    RatePlan,
    RatePlanVersion,
    RateSource,
    RateSourceRevision,
    RateSyncRun,
    SceCatalogEntry,
    UtilityAccount,
    UtilityAccountTierThreshold,
    aware_utc,
)
from ..schemas.api import ManualRateCandidateRequest, RateCandidateReviewRequest
from .cost_engine import season_from_storage
from .sce_rate_parser import CANDIDATE_SCHEMA

MANUAL_PARSER_VERSION = "manual-rate-entry-v1"
_DECIMAL_QUANTUM = Decimal("0.00000001")
_ACCOUNT_TIER_MARKER = Decimal("1")


@dataclass(frozen=True)
class AccountCycleTierThreshold:
    total_kwh: Decimal
    tier1_boundary_inclusive: bool
    evidence_ids: tuple[str, ...]


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
            tier_threshold_rule=(
                payload.tier_threshold_rule.model_dump(mode="json")
                if payload.tier_threshold_rule is not None
                else None
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
        review.tier_threshold_rule = (
            payload.tier_threshold_rule.model_dump(mode="json")
            if payload.tier_threshold_rule is not None
            else None
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
                "tier_threshold_rule_confirmed": payload.tier_threshold_rule is not None,
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


def _canonical_rate_components(
    raw: object,
    *,
    price_per_kwh: Decimal,
    delivery_value: object,
    generation_value: object,
) -> tuple[list[dict[str, object]], Decimal, Decimal]:
    explicit_delivery = (
        _bounded_decimal(
            delivery_value,
            field="delivery component",
            minimum=Decimal("0"),
            maximum=Decimal("5"),
        )
        if delivery_value is not None
        else None
    )
    explicit_generation = (
        _bounded_decimal(
            generation_value,
            field="generation component",
            minimum=Decimal("0"),
            maximum=Decimal("5"),
        )
        if generation_value is not None
        else None
    )
    if isinstance(raw, str) and raw:
        if explicit_delivery is not None or explicit_generation is not None:
            raise RateWorkflowConflict("candidate rate components disagree")
        return (
            [
                {
                    "component": raw,
                    "amount_per_kwh": None,
                    "source_status": "combined_only",
                }
            ],
            Decimal("0"),
            Decimal("0"),
        )
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > 40:
        raise RateWorkflowConflict("candidate rate components are malformed")
    components: list[dict[str, object]] = []
    exact_total = Decimal("0")
    all_exact = bool(raw)
    named_amounts: dict[str, Decimal] = {}
    for item in raw:
        if not isinstance(item, dict) or not set(item) <= {
            "component",
            "amount_per_kwh",
            "source_status",
            "source_label",
        }:
            raise RateWorkflowConflict("candidate rate components are malformed")
        component = item.get("component")
        amount_value = item.get("amount_per_kwh")
        source_status = item.get("source_status", "exact" if amount_value is not None else None)
        source_label = item.get("source_label")
        if (
            not isinstance(component, str)
            or not 1 <= len(component) <= 80
            or source_status not in {"exact", "combined_only"}
            or (source_label is not None and not isinstance(source_label, str))
            or (isinstance(source_label, str) and len(source_label) > 160)
        ):
            raise RateWorkflowConflict("candidate rate components are malformed")
        amount = (
            _bounded_decimal(
                amount_value,
                field="rate component",
                minimum=Decimal("0"),
                maximum=Decimal("5"),
            )
            if amount_value is not None
            else None
        )
        if (amount is None) != (source_status == "combined_only"):
            raise RateWorkflowConflict("candidate rate component precision is unresolved")
        canonical: dict[str, object] = {
            "component": component,
            "amount_per_kwh": format(amount, "f") if amount is not None else None,
            "source_status": source_status,
        }
        if source_label is not None:
            canonical["source_label"] = source_label
        components.append(canonical)
        if amount is None:
            all_exact = False
        else:
            exact_total += amount
            named_amounts[component.lower()] = amount
    if explicit_delivery is not None or explicit_generation is not None:
        delivery = explicit_delivery or Decimal("0")
        generation = explicit_generation or Decimal("0")
        if not components:
            components = [
                {
                    "component": "delivery_rate",
                    "amount_per_kwh": format(delivery, "f"),
                    "source_status": "exact",
                },
                {
                    "component": "generation_rate",
                    "amount_per_kwh": format(generation, "f"),
                    "source_status": "exact",
                },
            ]
            exact_total = delivery + generation
            all_exact = True
        else:
            if named_amounts.get("delivery_rate", named_amounts.get("delivery")) != delivery:
                raise RateWorkflowConflict("candidate delivery rate component disagrees")
            if named_amounts.get("generation_rate", named_amounts.get("generation")) != generation:
                raise RateWorkflowConflict("candidate generation rate component disagrees")
    else:
        delivery = named_amounts.get("delivery_rate", named_amounts.get("delivery", Decimal("0")))
        generation = named_amounts.get(
            "generation_rate", named_amounts.get("generation", Decimal("0"))
        )
    if all_exact and exact_total != price_per_kwh:
        raise RateWorkflowConflict("candidate exact rate components do not sum to the period price")
    return components, delivery, generation


def _exact_utc_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise RateWorkflowConflict(f"candidate {field} is not an offset timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RateWorkflowConflict(f"candidate {field} is not an offset timestamp") from exc
    if parsed.utcoffset() is None:
        raise RateWorkflowConflict(f"candidate {field} is not an offset timestamp")
    return parsed.astimezone(UTC)


def _canonical_tier_threshold_rule(raw: object) -> dict[str, object] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not set(raw) <= {
        "rule_type",
        "season",
        "kwh_per_day",
        "source_allowance_kwh",
        "source_billing_days",
        "tier1_boundary_inclusive",
        "source_label",
    }:
        raise RateWorkflowConflict("candidate tier threshold evidence is malformed")
    season = raw.get("season")
    source_days = raw.get("source_billing_days")
    source_label = raw.get("source_label")
    if (
        raw.get("rule_type", "daily_allowance") != "daily_allowance"
        or not isinstance(season, str)
        or not 1 <= len(season) <= 30
        or not isinstance(source_days, int)
        or isinstance(source_days, bool)
        or not 1 <= source_days <= 62
        or raw.get("tier1_boundary_inclusive", True) is not True
        or not isinstance(source_label, str)
        or not 1 <= len(source_label) <= 160
    ):
        raise RateWorkflowConflict("candidate tier threshold evidence is malformed")
    per_day = _bounded_decimal(
        raw.get("kwh_per_day"),
        field="daily tier allowance",
        minimum=Decimal("0.00000001"),
        maximum=Decimal("1000"),
    )
    source_allowance = _bounded_decimal(
        raw.get("source_allowance_kwh"),
        field="source tier allowance",
        minimum=Decimal("0.00000001"),
        maximum=Decimal("100000"),
    )
    if per_day * source_days != source_allowance:
        raise RateWorkflowConflict("candidate daily tier allowance does not reconcile")
    return {
        "rule_type": "daily_allowance",
        "season": season,
        "kwh_per_day": per_day,
        "source_allowance_kwh": source_allowance,
        "source_billing_days": source_days,
        "tier1_boundary_inclusive": True,
        "source_label": source_label,
    }


def _canonical_dated_prices(
    plan: dict[str, Any],
    *,
    effective_start: datetime,
    effective_end: datetime | None,
) -> list[dict[str, object]]:
    raw = plan.get("dated_prices", plan.get("price_intervals"))
    if effective_end is None:
        raise RateWorkflowConflict(
            "candidate dynamic-hourly inputs require a finite immutable schedule end"
        )
    if not isinstance(raw, list) or not raw or len(raw) > 20_000:
        raise RateWorkflowConflict(
            "candidate dynamic-hourly inputs are not an executable immutable dated schedule"
        )
    start_bound = aware_utc(effective_start)
    end_bound = aware_utc(effective_end)
    values: list[dict[str, object]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RateWorkflowConflict("candidate dated price interval is malformed")
        start = _exact_utc_datetime(
            item.get("start_utc", item.get("interval_start_utc")),
            field="dated price start",
        )
        end = _exact_utc_datetime(
            item.get("end_utc", item.get("interval_end_utc")),
            field="dated price end",
        )
        if end <= start or end - start > timedelta(hours=1):
            raise RateWorkflowConflict("candidate dated price interval is invalid")
        price = _bounded_decimal(
            item.get("price_per_kwh"),
            field="dated price",
            minimum=Decimal("0.00000001"),
            maximum=Decimal("5"),
        )
        components, delivery, generation = _canonical_rate_components(
            item.get("rate_components"),
            price_per_kwh=price,
            delivery_value=item.get("delivery_per_kwh"),
            generation_value=item.get("generation_per_kwh"),
        )
        source_label = item.get("source_label", f"dated_price_{index + 1}")
        if not isinstance(source_label, str) or not 1 <= len(source_label) <= 160:
            raise RateWorkflowConflict("candidate dated price source label is malformed")
        values.append(
            {
                "start_utc": start,
                "end_utc": end,
                "price_per_kwh": price,
                "delivery_per_kwh": delivery,
                "generation_per_kwh": generation,
                "rate_components": components,
                "source_label": source_label,
            }
        )
    values.sort(key=lambda value: cast(datetime, value["start_utc"]))
    if cast(datetime, values[0]["start_utc"]) != start_bound:
        raise RateWorkflowConflict("candidate dated price schedule does not cover its version")
    cursor = start_bound
    for value in values:
        if cast(datetime, value["start_utc"]) != cursor:
            raise RateWorkflowConflict("candidate dated price schedule has a gap or overlap")
        cursor = cast(datetime, value["end_utc"])
    if cursor != end_bound:
        raise RateWorkflowConflict("candidate dated price schedule does not cover its version")
    return values


def _canonical_eligibility(raw: object) -> list[dict[str, Any] | str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 64:
        raise RateWorkflowConflict("candidate eligibility metadata is malformed")
    encoded = json.dumps(raw, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(encoded) > 20_000 or any(not isinstance(item, dict | str) for item in raw):
        raise RateWorkflowConflict("candidate eligibility metadata is malformed")
    return cast(list[dict[str, Any] | str], json.loads(encoded))


async def _official_plan_metadata(
    session: AsyncSession,
    *,
    candidate: RateCandidate,
    selected_plan_name: str,
    plan_data: dict[str, Any],
    stored_plan_type: str,
) -> tuple[dict[str, object], dict[str, object]]:
    entries = (
        await session.scalars(
            select(SceCatalogEntry).where(
                SceCatalogEntry.source_revision_id == candidate.source_revision_id
            )
        )
    ).all()
    matches = [
        entry
        for entry in entries
        if entry.canonical_name == selected_plan_name
        or entry.public_plan_name == selected_plan_name
        or (
            isinstance(entry.normalized_plan, dict)
            and entry.normalized_plan.get("rate_plan_name") == selected_plan_name
        )
    ]
    if len(matches) > 1:
        raise RateWorkflowConflict("candidate official plan metadata is ambiguous")
    entry = matches[0] if matches else None

    def choose(field: str, catalog_value: object, fallback: object) -> object:
        supplied = plan_data.get(field)
        if (
            supplied not in (None, "")
            and catalog_value not in (None, "")
            and supplied != catalog_value
        ):
            raise RateWorkflowConflict(f"candidate {field} metadata disagrees with catalog")
        return catalog_value if catalog_value not in (None, "") else supplied or fallback

    official_schedule_code = choose(
        "official_schedule_code",
        entry.official_schedule_code if entry else None,
        None,
    )
    public_plan_name = choose(
        "public_plan_name", entry.public_plan_name if entry else None, selected_plan_name
    )
    canonical_name = choose(
        "canonical_name", entry.canonical_name if entry else None, selected_plan_name
    )
    plan_type = choose("plan_type", entry.plan_type if entry else None, stored_plan_type)
    enrollment_status = choose(
        "enrollment_status", entry.enrollment_status if entry else None, "unknown"
    )
    for field, value, maximum in (
        ("official_schedule_code", official_schedule_code, 80),
        ("public_plan_name", public_plan_name, 160),
        ("canonical_name", canonical_name, 160),
        ("plan_type", plan_type, 48),
        ("enrollment_status", enrollment_status, 40),
    ):
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= maximum):
            raise RateWorkflowConflict(f"candidate {field} metadata is malformed")
    if plan_type != stored_plan_type:
        raise RateWorkflowConflict("candidate plan type disagrees with executable schedule")
    supplied_eligibility = _canonical_eligibility(plan_data.get("eligibility"))
    catalog_eligibility = _canonical_eligibility(entry.eligibility) if entry is not None else []
    if supplied_eligibility and catalog_eligibility and supplied_eligibility != catalog_eligibility:
        raise RateWorkflowConflict("candidate eligibility metadata disagrees with catalog")
    eligibility = catalog_eligibility or supplied_eligibility
    catalog_description = (
        entry.normalized_plan.get("description")
        if entry is not None and isinstance(entry.normalized_plan, dict)
        else None
    )
    description = choose("description", catalog_description, None)
    if description is not None and (
        not isinstance(description, str) or not 1 <= len(description) <= 2000
    ):
        raise RateWorkflowConflict("candidate description metadata is malformed")
    metadata: dict[str, object] = {
        "utility_code": "SCE",
        "official_schedule_code": official_schedule_code,
        "public_plan_name": public_plan_name,
        "canonical_name": canonical_name,
        "plan_type": plan_type,
        "enrollment_status": enrollment_status,
        "eligibility": eligibility,
        "description": description,
    }
    evidence: dict[str, object] = {
        "evidence_type": "official_plan_metadata",
        "source_revision_id": candidate.source_revision_id,
        **metadata,
    }
    if entry is not None:
        evidence.update(
            {
                "catalog_entry_id": entry.id,
                "source_url": entry.source_url,
                "source_level": entry.source_level,
            }
        )
    return metadata, evidence


def _expanded_periods(
    plan: dict[str, Any],
    *,
    tier_threshold_rule: dict[str, object] | None = None,
) -> list[dict[str, Any]]:
    raw_periods = plan.get("periods")
    if not isinstance(raw_periods, list) or not raw_periods or len(raw_periods) > 200:
        raise RateWorkflowConflict("candidate periods are incomplete")
    expanded: list[dict[str, Any]] = []
    for raw in raw_periods:
        if not isinstance(raw, dict):
            raise RateWorkflowConflict("candidate period is malformed")
        season = raw.get("season")
        day_type = raw.get("day_type")
        if (
            not isinstance(season, str)
            or not season
            or len(season) > 30
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for character in season
            )
        ):
            raise RateWorkflowConflict("candidate season is unsupported")
        day_types = {
            "weekend_holiday": ("weekend", "holiday"),
            "all_days": ("all",),
        }.get(str(day_type), (str(day_type),))
        if not set(day_types) <= {
            "weekday",
            "weekend",
            "holiday",
            "all",
            "event_day",
            "non_event_day",
        }:
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
            or start >= 1440
            or end <= 0
            or end == start
        ):
            raise RateWorkflowConflict("candidate period boundaries are invalid")
        source_label = raw.get("source_label")
        if source_label is not None and (
            not isinstance(source_label, str) or not 1 <= len(source_label) <= 160
        ):
            raise RateWorkflowConflict("candidate period source label is malformed")
        price = _bounded_decimal(
            raw.get("price_per_kwh"),
            field="period price",
            minimum=Decimal("0.00000001"),
            maximum=Decimal("5"),
        )
        tier_start_value = raw.get("tier_start_kwh", raw.get("tier_min_kwh"))
        tier_end_value = raw.get("tier_end_kwh", raw.get("tier_max_kwh"))
        tier_name = name.lower().replace("-", "_")
        threshold_basis: object
        threshold_value_raw: object
        if tier_threshold_rule is not None:
            source_allowance = cast(Decimal, tier_threshold_rule["source_allowance_kwh"])
            if tier_name in {"tier_1", "tier1", "tier_one"}:
                tier_start_value = "0" if tier_start_value is None else tier_start_value
                tier_end_value = source_allowance if tier_end_value is None else tier_end_value
            elif tier_name in {"tier_2", "tier2", "tier_two"}:
                tier_start_value = (
                    source_allowance if tier_start_value is None else tier_start_value
                )
            threshold_basis = "account_daily_baseline"
            threshold_value_raw = None
        else:
            threshold_basis = raw.get("threshold_basis", plan.get("tier_threshold_basis"))
            threshold_value_raw = raw.get("threshold_value")
        tier_start = (
            Decimal("0")
            if tier_start_value is None
            else _bounded_decimal(
                tier_start_value,
                field="tier lower bound",
                minimum=Decimal("0"),
                maximum=Decimal("1000000"),
            )
        )
        tier_end = (
            None
            if tier_end_value is None
            else _bounded_decimal(
                tier_end_value,
                field="tier upper bound",
                minimum=Decimal("0.000001"),
                maximum=Decimal("1000000"),
            )
        )
        if tier_end is not None and tier_end <= tier_start:
            raise RateWorkflowConflict("candidate tier bounds are invalid")
        if tier_threshold_rule is not None:
            source_allowance = cast(Decimal, tier_threshold_rule["source_allowance_kwh"])
            if tier_name in {"tier_1", "tier1", "tier_one"} and (
                tier_start != 0 or tier_end != source_allowance
            ):
                raise RateWorkflowConflict(
                    "candidate Tier 1 bounds disagree with reviewed evidence"
                )
            if tier_name in {"tier_2", "tier2", "tier_two"} and (
                tier_start != source_allowance or tier_end is not None
            ):
                raise RateWorkflowConflict(
                    "candidate Tier 2 bounds disagree with reviewed evidence"
                )
            if tier_name in {"tier_1", "tier1", "tier_one"}:
                tier_start, tier_end = Decimal("0"), _ACCOUNT_TIER_MARKER
            elif tier_name in {"tier_2", "tier2", "tier_two"}:
                tier_start, tier_end = _ACCOUNT_TIER_MARKER, None
            else:
                raise RateWorkflowConflict(
                    "candidate account baseline supports exactly Tier 1 and Tier 2"
                )
        boundary_inclusive = raw.get("boundary_inclusive", True)
        if not isinstance(boundary_inclusive, bool):
            raise RateWorkflowConflict("candidate tier boundary behavior is unresolved")
        if threshold_basis is not None and (
            not isinstance(threshold_basis, str) or not 1 <= len(threshold_basis) <= 80
        ):
            raise RateWorkflowConflict("candidate tier threshold basis is malformed")
        threshold_value = (
            _bounded_decimal(
                threshold_value_raw,
                field="tier threshold value",
                minimum=Decimal("0"),
                maximum=Decimal("1000000"),
            )
            if threshold_value_raw is not None
            else None
        )
        components, delivery, generation = _canonical_rate_components(
            raw.get("rate_components", plan.get("rate_components")),
            price_per_kwh=price,
            delivery_value=raw.get("delivery_per_kwh"),
            generation_value=raw.get("generation_per_kwh"),
        )
        windows = ((start, end),) if end > start else ((start, 1440), (0, end))
        for expanded_day_type in day_types:
            for window_start, window_end in windows:
                expanded.append(
                    {
                        "season": season,
                        "day_type": expanded_day_type,
                        "name": name,
                        "start_minute": window_start,
                        "end_minute": window_end,
                        "price_per_kwh": price,
                        "delivery_per_kwh": delivery,
                        "generation_per_kwh": generation,
                        "rate_components": components,
                        "tier_start_kwh": tier_start,
                        "tier_end_kwh": tier_end,
                        "boundary_inclusive": boundary_inclusive,
                        "threshold_basis": threshold_basis,
                        "threshold_value": threshold_value,
                        "source_label": source_label or name,
                    }
                )
    return expanded


def _canonical_season_definitions(raw: object, *, required: bool) -> list[dict[str, object]]:
    if raw in (None, {}):
        if required:
            raise RateWorkflowConflict("candidate season evidence is unresolved")
        raw = {
            "all_year": {
                "start_month": 1,
                "start_day": 1,
                "end_month": 12,
                "end_day": 31,
                "source_label": "not_applicable_nonseasonal_plan",
            }
        }
    values: list[dict[str, object]] = []
    if isinstance(raw, dict):
        entries = [
            {"season_name": name, **definition}
            for name, definition in raw.items()
            if isinstance(name, str) and isinstance(definition, dict)
        ]
        if len(entries) != len(raw):
            raise RateWorkflowConflict("candidate season definitions are malformed")
    elif isinstance(raw, list):
        entries = raw
    else:
        raise RateWorkflowConflict("candidate season definitions are malformed")
    for entry in entries:
        if not isinstance(entry, dict):
            raise RateWorkflowConflict("candidate season definitions are malformed")
        name = entry.get("season_name")
        start_month = entry.get("start_month")
        end_month = entry.get("end_month")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 30
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for character in name
            )
            or not isinstance(start_month, int)
            or isinstance(start_month, bool)
            or not isinstance(end_month, int)
            or isinstance(end_month, bool)
            or not 1 <= start_month <= 12
            or not 1 <= end_month <= 12
        ):
            raise RateWorkflowConflict("candidate season boundaries are invalid")
        start_day = entry.get("start_day", 1)
        end_day = entry.get("end_day", monthrange(2000, end_month)[1])
        if (
            not isinstance(start_day, int)
            or isinstance(start_day, bool)
            or not isinstance(end_day, int)
            or isinstance(end_day, bool)
        ):
            raise RateWorkflowConflict("candidate season boundaries are invalid")
        try:
            date(2000, start_month, start_day)
            date(2000, end_month, end_day)
        except ValueError as exc:
            raise RateWorkflowConflict("candidate season boundaries are invalid") from exc
        source_label = entry.get("source_label")
        if source_label is not None and (
            not isinstance(source_label, str) or len(source_label) > 160
        ):
            raise RateWorkflowConflict("candidate season source label is invalid")
        values.append(
            {
                "season_name": name,
                "start_month": start_month,
                "start_day": start_day,
                "end_month": end_month,
                "end_day": end_day,
                "source_label": source_label,
            }
        )
    if not values or len({value["season_name"] for value in values}) != len(values):
        raise RateWorkflowConflict("candidate season definitions are incomplete or duplicated")
    cursor = date(2000, 1, 1)
    for offset in range(366):
        local_day = cursor + timedelta(days=offset)
        key = local_day.month * 100 + local_day.day
        matches = 0
        for value in values:
            start = cast(int, value["start_month"]) * 100 + cast(int, value["start_day"])
            end = cast(int, value["end_month"]) * 100 + cast(int, value["end_day"])
            if (start <= key <= end) if start <= end else (key >= start or key <= end):
                matches += 1
        if matches != 1:
            raise RateWorkflowConflict("candidate season boundaries have a gap or overlap")
    return values


def _canonical_holiday_treatment(normalized: dict[str, Any], periods: list[dict[str, Any]]) -> str:
    raw = normalized.get("holiday_treatment")
    aliases = {
        "weekend_schedule": "same_as_weekend",
        "explicit_schedule": "explicit_holiday_schedule",
    }
    treatment = aliases.get(str(raw), str(raw)) if raw is not None else "unresolved"
    allowed = {
        "not_applicable",
        "same_as_weekday",
        "same_as_weekend",
        "explicit_holiday_schedule",
        "no_special_treatment",
        "event_calendar_required",
        "unresolved",
    }
    if treatment not in allowed:
        raise RateWorkflowConflict("candidate holiday treatment is unsupported")
    day_specific = any(
        period["day_type"] not in {"all", "event_day", "non_event_day"} for period in periods
    )
    if treatment == "unresolved" and day_specific:
        raise RateWorkflowConflict("candidate holiday treatment is unresolved")
    return "not_applicable" if treatment == "unresolved" else treatment


def _validate_tier_axis(candidates: list[dict[str, Any]]) -> None:
    if not candidates:
        raise RateWorkflowConflict("candidate schedule has a gap")
    boundaries = sorted(
        {Decimal("0")}
        | {cast(Decimal, period["tier_start_kwh"]) for period in candidates}
        | {
            cast(Decimal, period["tier_end_kwh"])
            for period in candidates
            if period["tier_end_kwh"] is not None
        }
    )
    for start, end in pairwise(boundaries):
        midpoint = start + (end - start) / Decimal(2)
        active = [
            period
            for period in candidates
            if midpoint >= period["tier_start_kwh"]
            and (period["tier_end_kwh"] is None or midpoint < period["tier_end_kwh"])
        ]
        if len(active) != 1:
            raise RateWorkflowConflict("candidate tier schedule has a gap or overlap")
    beyond = boundaries[-1] + Decimal("1")
    active_beyond = [
        period
        for period in candidates
        if beyond >= period["tier_start_kwh"]
        and (period["tier_end_kwh"] is None or beyond < period["tier_end_kwh"])
    ]
    if len(active_beyond) != 1 or active_beyond[0]["tier_end_kwh"] is not None:
        raise RateWorkflowConflict("candidate tier schedule is not open-ended")


def _validate_one_day(
    periods: list[dict[str, Any]], season: str, day_type_priority: tuple[str, ...]
) -> None:
    eligible = [
        period
        for period in periods
        if period["season"] in (season, "all") and period["day_type"] in day_type_priority
    ]
    boundaries = sorted(
        {0, 1440}
        | {int(period["start_minute"]) for period in eligible}
        | {int(period["end_minute"]) for period in eligible}
    )
    for start, end in pairwise(boundaries):
        midpoint = start + (end - start) // 2
        candidates = [
            period
            for period in eligible
            if period["start_minute"] <= midpoint < period["end_minute"]
        ]
        if candidates:
            specificity = max(
                (
                    int(period["season"] == season),
                    len(day_type_priority) - day_type_priority.index(period["day_type"]),
                )
                for period in candidates
            )
            candidates = [
                period
                for period in candidates
                if (
                    int(period["season"] == season),
                    len(day_type_priority) - day_type_priority.index(period["day_type"]),
                )
                == specificity
            ]
        _validate_tier_axis(candidates)


def _validate_period_coverage(
    periods: list[dict[str, Any]],
    seasons: list[dict[str, object]],
    holiday_treatment: str,
) -> None:
    event_aware = any(period["day_type"] in {"event_day", "non_event_day"} for period in periods)
    for season_value in seasons:
        season = str(season_value["season_name"])
        regular_day_types = ["weekday", "weekend"]
        if holiday_treatment == "explicit_holiday_schedule":
            regular_day_types.append("holiday")
        for day_type in regular_day_types:
            priority = ("non_event_day", day_type, "all") if event_aware else (day_type, "all")
            _validate_one_day(periods, season, priority)
        if event_aware:
            for day_type in regular_day_types:
                _validate_one_day(periods, season, ("event_day", day_type, "all"))


def _canonical_event_calendar(
    normalized: dict[str, Any],
    *,
    required: bool,
    effective_start: datetime,
    effective_end: datetime | None,
) -> dict[str, object] | None:
    raw = normalized.get("event_calendar")
    if not required and raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("status") not in {"complete", "resolved"}:
        raise RateWorkflowConflict("candidate event calendar is unresolved")
    raw_dates = raw.get("local_dates", raw.get("dates"))
    coverage_start_value = raw.get("coverage_start")
    coverage_end_value = raw.get("coverage_end")
    if (
        not isinstance(raw_dates, list)
        or not isinstance(coverage_start_value, str)
        or not isinstance(coverage_end_value, str)
        or effective_end is None
    ):
        raise RateWorkflowConflict("candidate event calendar coverage is unresolved")
    try:
        local_dates = sorted({date.fromisoformat(str(value)) for value in raw_dates})
        coverage_start = date.fromisoformat(coverage_start_value)
        coverage_end = date.fromisoformat(coverage_end_value)
    except ValueError as exc:
        raise RateWorkflowConflict("candidate event calendar dates are invalid") from exc
    zone = ZoneInfo("America/Los_Angeles")
    version_start = aware_utc(effective_start).astimezone(zone).date()
    version_last_day = (
        aware_utc(effective_end).astimezone(zone) - timedelta(microseconds=1)
    ).date()
    if (
        coverage_end < coverage_start
        or coverage_start > version_start
        or coverage_end < version_last_day
        or any(value < coverage_start or value > coverage_end for value in local_dates)
    ):
        raise RateWorkflowConflict("candidate event calendar does not cover the effective version")
    return {
        "evidence_type": "event_calendar",
        "status": "resolved",
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "local_dates": [value.isoformat() for value in local_dates],
    }


def _canonical_holiday_calendar(
    normalized: dict[str, Any],
    *,
    required: bool,
    effective_start: datetime,
    effective_end: datetime | None,
) -> dict[str, object] | None:
    raw = normalized.get("holiday_calendar")
    if not required and raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "status",
        "authority",
        "source_url",
        "coverage_start",
        "coverage_end",
        "holidays",
    }:
        raise RateWorkflowConflict("candidate holiday calendar evidence is unresolved")
    authority = raw.get("authority")
    source_url = raw.get("source_url")
    try:
        parsed_url = urlsplit(source_url) if isinstance(source_url, str) else None
        source_port = parsed_url.port if parsed_url is not None else None
    except ValueError as exc:
        raise RateWorkflowConflict("candidate holiday calendar authority is unresolved") from exc
    hostname = parsed_url.hostname.lower() if parsed_url and parsed_url.hostname else ""
    if (
        raw.get("status") not in {"complete", "resolved"}
        or authority != "Southern California Edison"
        or parsed_url is None
        or parsed_url.scheme != "https"
        or (hostname != "sce.com" and not hostname.endswith(".sce.com"))
        or parsed_url.username is not None
        or parsed_url.password is not None
        or source_port not in (None, 443)
    ):
        raise RateWorkflowConflict("candidate holiday calendar authority is unresolved")
    coverage_start_value = raw.get("coverage_start")
    coverage_end_value = raw.get("coverage_end")
    raw_holidays = raw.get("holidays")
    if (
        not isinstance(coverage_start_value, str)
        or not isinstance(coverage_end_value, str)
        or not isinstance(raw_holidays, list)
        or len(raw_holidays) > 1000
        or effective_end is None
    ):
        raise RateWorkflowConflict("candidate holiday calendar coverage is unresolved")
    try:
        coverage_start = date.fromisoformat(coverage_start_value)
        coverage_end = date.fromisoformat(coverage_end_value)
    except ValueError as exc:
        raise RateWorkflowConflict("candidate holiday calendar dates are invalid") from exc
    holidays: list[dict[str, str]] = []
    seen_dates: set[date] = set()
    for raw_holiday in raw_holidays:
        if not isinstance(raw_holiday, dict) or set(raw_holiday) != {"local_date", "name"}:
            raise RateWorkflowConflict("candidate holiday calendar entry is malformed")
        raw_date = raw_holiday.get("local_date")
        name = raw_holiday.get("name")
        if not isinstance(raw_date, str) or not isinstance(name, str) or not 1 <= len(name) <= 120:
            raise RateWorkflowConflict("candidate holiday calendar entry is malformed")
        try:
            local_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise RateWorkflowConflict("candidate holiday calendar dates are invalid") from exc
        if local_date in seen_dates:
            raise RateWorkflowConflict("candidate holiday calendar contains duplicate dates")
        seen_dates.add(local_date)
        holidays.append({"local_date": local_date.isoformat(), "name": name})
    zone = ZoneInfo("America/Los_Angeles")
    version_start = aware_utc(effective_start).astimezone(zone).date()
    version_last_day = (
        (aware_utc(effective_end).astimezone(zone) - timedelta(microseconds=1)).date()
        if effective_end is not None
        else None
    )
    if (
        coverage_end < coverage_start
        or coverage_start > version_start
        or (version_last_day is not None and coverage_end < version_last_day)
        or any(
            date.fromisoformat(item["local_date"]) < coverage_start
            or date.fromisoformat(item["local_date"]) > coverage_end
            for item in holidays
        )
    ):
        raise RateWorkflowConflict(
            "candidate holiday calendar does not cover the effective version"
        )
    return {
        "evidence_type": "holiday_calendar",
        "status": "resolved",
        "authority": authority,
        "source_url": source_url,
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "holidays": sorted(holidays, key=lambda item: item["local_date"]),
    }


def _canonical_fixed_charges(
    plan_data: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Decimal]]:
    limits = {
        "daily_fixed_charge": Decimal("20"),
        "monthly_fixed_charge": Decimal("500"),
        "minimum_charge": Decimal("500"),
        "meter_charge": Decimal("500"),
        "other_fixed_charge": Decimal("500"),
    }
    values = {
        field: _bounded_decimal(
            plan_data.get(field, "0"),
            field=field.replace("_", " "),
            minimum=Decimal("0"),
            maximum=maximum,
        )
        for field, maximum in limits.items()
    }
    rows: dict[str, dict[str, str]] = {}
    raw_rows = plan_data.get("fixed_charges", [])
    if not isinstance(raw_rows, list):
        raise RateWorkflowConflict("candidate fixed charges are malformed")
    applies_values = {
        "per_account_per_day",
        "per_account_per_month",
        "per_account_per_cycle",
        "per_meter_per_day",
        "per_meter_per_month",
        "per_meter_per_cycle",
    }
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise RateWorkflowConflict("candidate fixed charges are malformed")
        kind = raw.get("charge", raw.get("kind"))
        applies = raw.get("applies")
        if kind not in limits or not isinstance(applies, str) or applies not in applies_values:
            raise RateWorkflowConflict("candidate fixed-charge applicability is unsupported")
        if kind in rows:
            raise RateWorkflowConflict("candidate fixed charges contain duplicates")
        amount = _bounded_decimal(
            raw.get("amount"),
            field=str(kind).replace("_", " "),
            minimum=Decimal("0"),
            maximum=limits[str(kind)],
        )
        scalar = values[str(kind)]
        if scalar and scalar != amount:
            raise RateWorkflowConflict("candidate fixed-charge values disagree")
        values[str(kind)] = amount
        rows[str(kind)] = {
            "charge": str(kind),
            "amount": format(amount, "f"),
            "currency": "USD",
            "applies": applies,
        }
    defaults = {
        "daily_fixed_charge": "per_account_per_day",
        "monthly_fixed_charge": "per_account_per_month",
    }
    for kind, applies in defaults.items():
        if kind not in rows:
            rows[kind] = {
                "charge": kind,
                "amount": format(values[kind], "f"),
                "currency": "USD",
                "applies": applies,
            }
    for kind in ("minimum_charge", "meter_charge", "other_fixed_charge"):
        if values[kind] and kind not in rows:
            raise RateWorkflowConflict(
                f"candidate {kind} requires explicit account/meter and recurrence semantics"
            )
    if rows.get("daily_fixed_charge", {}).get("applies") != "per_account_per_day":
        raise RateWorkflowConflict("candidate daily fixed charge applicability is invalid")
    if rows.get("monthly_fixed_charge", {}).get("applies") != "per_account_per_month":
        raise RateWorkflowConflict("candidate monthly fixed charge applicability is invalid")
    if "meter_charge" in rows and not rows["meter_charge"]["applies"].startswith("per_meter_"):
        raise RateWorkflowConflict("candidate meter charge must apply per utility meter")
    return list(rows.values()), values


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
        or pricing_model
        not in {
            "flat",
            "tiered",
            "seasonal_tiered",
            "time_of_use",
            "seasonal_time_of_use",
            "time_of_use_plus_baseline_credit",
            "time_of_use_with_baseline_credit",
            "critical_peak_pricing",
            "dynamic_hourly",
        }
    ):
        raise RateWorkflowConflict("candidate plan metadata is not publishable")
    tier_threshold_rule = _canonical_tier_threshold_rule(
        review.tier_threshold_rule or plan_data.get("tier_threshold_rule")
    )
    warnings = candidate.validation_evidence.get("warnings", [])
    if isinstance(warnings, list) and "PUBLIC_SOURCE_PRICES_ARE_DISPLAY_ROUNDED" in warnings:
        raise RateWorkflowConflict(
            "candidate exact tariff prices are required; rounded public prices are review-only"
        )
    if normalized.get("rate_component_scope_verified") is False:
        raise RateWorkflowConflict(
            "candidate rate component scope requires authoritative tariff evidence"
        )
    if normalized.get("baseline_credit_scope_verified") is False:
        raise RateWorkflowConflict(
            "candidate baseline credit scope requires authoritative tariff evidence"
        )
    coverage = candidate.validation_evidence.get("coverage")
    if coverage != "complete":
        if coverage != "semantic_tier_coverage" or pricing_model not in {
            "tiered",
            "seasonal_tiered",
        }:
            raise RateWorkflowConflict("candidate reusable schedule is incomplete")
        if tier_threshold_rule is None:
            raise RateWorkflowConflict(
                "candidate reusable schedule is incomplete; account baseline evidence is required"
            )
    dated_prices = (
        _canonical_dated_prices(
            plan_data,
            effective_start=review.effective_start,
            effective_end=review.effective_end,
        )
        if pricing_model == "dynamic_hourly"
        else []
    )
    periods = (
        []
        if pricing_model == "dynamic_hourly"
        else _expanded_periods(plan_data, tier_threshold_rule=tier_threshold_rule)
    )
    season_evidence_required = pricing_model in {
        "seasonal_tiered",
        "seasonal_time_of_use",
    } or any(period["season"] != "all" for period in periods)
    season_definitions = _canonical_season_definitions(
        normalized.get("season_definitions"), required=season_evidence_required
    )
    valid_seasons = {str(item["season_name"]) for item in season_definitions}
    if any(period["season"] not in valid_seasons | {"all"} for period in periods):
        raise RateWorkflowConflict("candidate period references an undefined season")
    holiday_treatment = _canonical_holiday_treatment(normalized, periods)
    if pricing_model != "dynamic_hourly":
        _validate_period_coverage(periods, season_definitions, holiday_treatment)
    if (
        pricing_model in {"tiered", "seasonal_tiered"}
        and len({(period["tier_start_kwh"], period["tier_end_kwh"]) for period in periods}) < 2
    ):
        raise RateWorkflowConflict("candidate tier bounds are unresolved")
    holiday_calendar_required = holiday_treatment in {
        "same_as_weekday",
        "same_as_weekend",
        "explicit_holiday_schedule",
    } and any(period["day_type"] in {"weekday", "weekend", "holiday"} for period in periods)
    holiday_calendar = _canonical_holiday_calendar(
        normalized,
        required=holiday_calendar_required,
        effective_start=review.effective_start,
        effective_end=review.effective_end,
    )
    event_aware = any(period["day_type"] in {"event_day", "non_event_day"} for period in periods)
    event_calendar = _canonical_event_calendar(
        normalized,
        required=event_aware or holiday_treatment == "event_calendar_required",
        effective_start=review.effective_start,
        effective_end=review.effective_end,
    )
    fixed_charges, fixed_charge_values = _canonical_fixed_charges(plan_data)
    daily = fixed_charge_values["daily_fixed_charge"]
    monthly = fixed_charge_values["monthly_fixed_charge"]
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
    stored_plan_type = {"time_of_use_plus_baseline_credit": "time_of_use_with_baseline_credit"}.get(
        pricing_model, pricing_model
    )
    plan_metadata, metadata_evidence = await _official_plan_metadata(
        session,
        candidate=candidate,
        selected_plan_name=plan_name,
        plan_data=plan_data,
        stored_plan_type=stored_plan_type,
    )
    plan.utility_code = cast(str, plan_metadata["utility_code"])
    plan.official_schedule_code = cast(str | None, plan_metadata["official_schedule_code"])
    plan.public_plan_name = cast(str, plan_metadata["public_plan_name"])
    plan.canonical_name = cast(str, plan_metadata["canonical_name"])
    plan.plan_type = cast(str, plan_metadata["plan_type"])
    plan.enrollment_status = cast(str, plan_metadata["enrollment_status"])
    plan.eligibility = cast(list[dict[str, Any] | str], plan_metadata["eligibility"])
    plan.description = cast(str | None, plan_metadata["description"])
    plan.currency = currency
    plan.energy_unit = "kWh"
    revision = await session.get(RateSourceRevision, candidate.source_revision_id)
    if revision is None:
        raise RateWorkflowConflict("candidate provenance revision is missing")
    now = _utc_now()
    price_components: list[dict[str, object]] = []
    seen_components: set[str] = set()
    for period in periods:
        for component in cast(list[dict[str, object]], period["rate_components"]):
            key = json.dumps(component, sort_keys=True, separators=(",", ":"))
            if key not in seen_components:
                seen_components.add(key)
                price_components.append(component)
    for dated_price in dated_prices:
        for component in cast(list[dict[str, object]], dated_price["rate_components"]):
            key = json.dumps(component, sort_keys=True, separators=(",", ":"))
            if key not in seen_components:
                seen_components.add(key)
                price_components.append(component)
    eligibility_evidence = [*plan.eligibility, metadata_evidence]
    if holiday_calendar is not None:
        eligibility_evidence.append(holiday_calendar)
    if event_calendar is not None:
        eligibility_evidence.append(event_calendar)
    if tier_threshold_rule is not None:
        eligibility_evidence.append(
            {
                "evidence_type": "account_tier_threshold_requirement",
                "rule_type": "daily_allowance",
                "season": tier_threshold_rule["season"],
                "tier1_boundary_inclusive": True,
                "source_label": tier_threshold_rule["source_label"],
                "account_scoped": True,
            }
        )
    version = RatePlanVersion(
        rate_plan_id=plan.id,
        version=version_number,
        effective_start=review.effective_start,
        effective_end=review.effective_end,
        timezone="America/Los_Angeles",
        pricing_model=pricing_model,
        source_version=revision.artifact_sha256,
        holiday_treatment=holiday_treatment,
        season_definitions=season_definitions,
        fixed_charges=fixed_charges,
        price_components=price_components,
        eligibility_evidence=eligibility_evidence,
        daily_fixed_charge=daily,
        monthly_fixed_charge=monthly,
        minimum_charge=fixed_charge_values["minimum_charge"],
        meter_charge=fixed_charge_values["meter_charge"],
        other_fixed_charge=fixed_charge_values["other_fixed_charge"],
        baseline_credit_per_kwh=baseline,
        # Account-specific baseline allowances never become executable global
        # version state. Legacy rows retain these nullable columns, but every
        # new reviewed DOMESTIC threshold is resolved from the assigned account.
        tier_threshold_kwh_per_day=None,
        tier_threshold_season=None,
        tier_threshold_source_kwh=None,
        tier_threshold_source_days=None,
        tier1_boundary_inclusive=True,
        source_hash=revision.artifact_sha256,
        algorithm_version="cost-v2",
        state="draft",
        published_by_user_id=actor_user_id,
        published_at=now,
    )
    session.add(version)
    await session.flush()
    if holiday_calendar is not None:
        raw_holidays = holiday_calendar["holidays"]
        assert isinstance(raw_holidays, list)
        for holiday in raw_holidays:
            assert isinstance(holiday, dict)
            session.add(
                RateHoliday(
                    rate_plan_version_id=version.id,
                    local_date=date.fromisoformat(str(holiday["local_date"])),
                    name=str(holiday["name"]),
                )
            )
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
                delivery_per_kwh=period["delivery_per_kwh"],
                generation_per_kwh=period["generation_per_kwh"],
                rate_components=period["rate_components"],
                baseline_credit_per_kwh=baseline,
                tier_start_kwh=period["tier_start_kwh"],
                tier_end_kwh=period["tier_end_kwh"],
                boundary_inclusive=period["boundary_inclusive"],
                threshold_basis=period["threshold_basis"],
                threshold_value=period["threshold_value"],
                source_label=period["source_label"],
            )
        )
    for dated_price in dated_prices:
        session.add(
            RateDatedPrice(
                rate_plan_version_id=version.id,
                start_utc=cast(datetime, dated_price["start_utc"]),
                end_utc=cast(datetime, dated_price["end_utc"]),
                price_per_kwh=cast(Decimal, dated_price["price_per_kwh"]),
                delivery_per_kwh=cast(Decimal, dated_price["delivery_per_kwh"]),
                generation_per_kwh=cast(Decimal, dated_price["generation_per_kwh"]),
                rate_components=cast(list[dict[str, Any]], dated_price["rate_components"]),
                source_label=cast(str, dated_price["source_label"]),
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


async def replace_utility_account_tier_threshold(
    session: AsyncSession,
    *,
    account: UtilityAccount,
    rate_plan_id: str,
    season: str,
    kwh_per_day: Decimal,
    source_allowance_kwh: Decimal,
    source_billing_days: int,
    tier1_boundary_inclusive: bool,
    source_label: str,
    source_kind: str,
    source_artifact_sha256: str,
    effective_start: datetime,
    effective_end: datetime | None,
    actor_user_id: str,
) -> tuple[UtilityAccountTierThreshold, bool]:
    """Replace one account/plan/season threshold without mutating shared rates."""

    if (
        not season
        or len(season) > 30
        or kwh_per_day <= 0
        or source_allowance_kwh <= 0
        or not 1 <= source_billing_days <= 62
        or kwh_per_day * source_billing_days != source_allowance_kwh
        or tier1_boundary_inclusive is not True
        or not 1 <= len(source_label) <= 160
        or source_kind not in {"candidate_review", "bill_rate_import"}
        or len(source_artifact_sha256) != 64
        or set(source_artifact_sha256) - set("0123456789abcdef")
    ):
        raise RateWorkflowConflict("account tier-threshold evidence is incomplete or invalid")
    start = aware_utc(effective_start)
    end = aware_utc(effective_end) if effective_end is not None else None
    if end is not None and end <= start:
        raise RateWorkflowConflict("account tier-threshold effective range is invalid")
    locked_account = await session.scalar(
        select(UtilityAccount).where(UtilityAccount.id == account.id).with_for_update()
    )
    if locked_account is None:
        raise NotFound("utility account does not exist")
    existing = (
        await session.scalars(
            select(UtilityAccountTierThreshold)
            .where(
                UtilityAccountTierThreshold.utility_account_id == locked_account.id,
                UtilityAccountTierThreshold.rate_plan_id == rate_plan_id,
                UtilityAccountTierThreshold.season == season,
            )
            .order_by(
                UtilityAccountTierThreshold.effective_start,
                UtilityAccountTierThreshold.id,
            )
            .with_for_update()
        )
    ).all()
    exact = next(
        (item for item in existing if aware_utc(item.effective_start) == start),
        None,
    )
    if exact is not None:
        exact_end = aware_utc(exact.effective_end) if exact.effective_end is not None else None
        if (
            exact_end == end
            and exact.kwh_per_day == kwh_per_day
            and exact.source_allowance_kwh == source_allowance_kwh
            and exact.source_billing_days == source_billing_days
            and exact.tier1_boundary_inclusive is tier1_boundary_inclusive
            and exact.source_label == source_label
            and exact.source_kind == source_kind
            and exact.source_artifact_sha256 == source_artifact_sha256
        ):
            return exact, False
        raise RateWorkflowConflict(
            "different account tier-threshold evidence already starts at this instant"
        )
    for item in existing:
        item_start = aware_utc(item.effective_start)
        item_end = aware_utc(item.effective_end) if item.effective_end is not None else None
        if item_start < start and (item_end is None or item_end > start):
            item.effective_end = start
    future_starts = sorted(
        aware_utc(item.effective_start)
        for item in existing
        if aware_utc(item.effective_start) > start
    )
    if future_starts and (end is None or end > future_starts[0]):
        end = future_starts[0]
    threshold = UtilityAccountTierThreshold(
        utility_account_id=locked_account.id,
        rate_plan_id=rate_plan_id,
        season=season,
        kwh_per_day=kwh_per_day,
        source_allowance_kwh=source_allowance_kwh,
        source_billing_days=source_billing_days,
        tier1_boundary_inclusive=tier1_boundary_inclusive,
        source_label=source_label,
        source_kind=source_kind,
        source_artifact_sha256=source_artifact_sha256,
        effective_start=start,
        effective_end=end,
        created_by_user_id=actor_user_id,
    )
    session.add(threshold)
    await session.flush()
    return threshold, True


async def resolve_utility_account_tier_threshold(
    session: AsyncSession,
    *,
    utility_account_id: str,
    rate_plan_id: str,
    season: str,
    instant: datetime,
) -> UtilityAccountTierThreshold | None:
    """Resolve one exact effective rule, preferring the named season over ``all``."""

    at = aware_utc(instant)
    rows = (
        await session.scalars(
            select(UtilityAccountTierThreshold).where(
                UtilityAccountTierThreshold.utility_account_id == utility_account_id,
                UtilityAccountTierThreshold.rate_plan_id == rate_plan_id,
                UtilityAccountTierThreshold.season.in_((season, "all")),
                UtilityAccountTierThreshold.effective_start <= at,
                (
                    UtilityAccountTierThreshold.effective_end.is_(None)
                    | (UtilityAccountTierThreshold.effective_end > at)
                ),
            )
        )
    ).all()
    exact = [item for item in rows if item.season == season]
    fallback = [item for item in rows if item.season == "all"]
    selected = exact or fallback
    if len(selected) > 1:
        raise RateWorkflowConflict("account tier-threshold evidence overlaps")
    if not selected:
        return None
    threshold = selected[0]
    if threshold.kwh_per_day * threshold.source_billing_days != threshold.source_allowance_kwh:
        raise RateWorkflowConflict("stored account tier-threshold evidence does not reconcile")
    return threshold


async def resolve_utility_account_cycle_tier_threshold(
    session: AsyncSession,
    *,
    utility_account_id: str,
    rate_plan_id: str,
    season_definitions: object,
    timezone: str,
    cycle_start: datetime,
    cycle_end: datetime,
) -> AccountCycleTierThreshold | None:
    """Sum exact account allowances for every local billing day in ``[start, end)``."""

    start = aware_utc(cycle_start)
    end = aware_utc(cycle_end)
    if end <= start:
        raise RateWorkflowConflict("billing cycle range is invalid")
    zone = ZoneInfo(timezone)
    local_start = start.astimezone(zone)
    local_end = end.astimezone(zone)
    if (
        local_start.hour
        or local_start.minute
        or local_start.second
        or local_start.microsecond
        or local_end.hour
        or local_end.minute
        or local_end.second
        or local_end.microsecond
    ):
        raise RateWorkflowConflict("account tier thresholds require local-day cycle boundaries")
    rows = (
        await session.scalars(
            select(UtilityAccountTierThreshold).where(
                UtilityAccountTierThreshold.utility_account_id == utility_account_id,
                UtilityAccountTierThreshold.rate_plan_id == rate_plan_id,
                UtilityAccountTierThreshold.effective_start < end,
                (
                    UtilityAccountTierThreshold.effective_end.is_(None)
                    | (UtilityAccountTierThreshold.effective_end > start)
                ),
            )
        )
    ).all()
    if not rows:
        return None

    def select_rule(at: datetime, season: str) -> UtilityAccountTierThreshold | None:
        active = [
            item
            for item in rows
            if aware_utc(item.effective_start) <= at
            and (item.effective_end is None or aware_utc(item.effective_end) > at)
        ]
        exact = [item for item in active if item.season == season]
        fallback = [item for item in active if item.season == "all"]
        selected = exact or fallback
        if len(selected) > 1:
            raise RateWorkflowConflict("account tier-threshold evidence overlaps")
        return selected[0] if selected else None

    total = Decimal("0")
    evidence_ids: list[str] = []
    inclusive: bool | None = None
    local_day = local_start.date()
    while local_day < local_end.date():
        day_start_local = datetime(
            local_day.year,
            local_day.month,
            local_day.day,
            tzinfo=zone,
        )
        next_local_day = local_day + timedelta(days=1)
        day_end_local = datetime(
            next_local_day.year,
            next_local_day.month,
            next_local_day.day,
            tzinfo=zone,
        )
        representative = day_start_local + timedelta(hours=12)
        season = season_from_storage(season_definitions, representative)
        day_start_utc = day_start_local.astimezone(UTC)
        day_end_utc = day_end_local.astimezone(UTC)
        first = select_rule(day_start_utc, season)
        last = select_rule(day_end_utc - timedelta(microseconds=1), season)
        if first is None or last is None or first.id != last.id:
            raise RateWorkflowConflict(
                "account tier-threshold evidence does not cover a complete billing day"
            )
        if first.kwh_per_day * first.source_billing_days != first.source_allowance_kwh:
            raise RateWorkflowConflict("stored account tier-threshold evidence does not reconcile")
        if inclusive is not None and first.tier1_boundary_inclusive is not inclusive:
            raise RateWorkflowConflict(
                "account tier-threshold boundary semantics change within the billing cycle"
            )
        inclusive = first.tier1_boundary_inclusive
        total += first.kwh_per_day
        if first.id not in evidence_ids:
            evidence_ids.append(first.id)
        local_day = next_local_day
    if inclusive is None:
        raise RateWorkflowConflict("billing cycle contains no local service days")
    return AccountCycleTierThreshold(total, inclusive, tuple(evidence_ids))


async def resolve_assigned_utility_account_cycle_tier_threshold(
    session: AsyncSession,
    *,
    utility_account_id: str,
    timezone: str,
    cycle_start: datetime,
    cycle_end: datetime,
) -> AccountCycleTierThreshold | None:
    """Resolve the immutable assigned version and account allowance for each local day.

    A rate-version transition at a local-day boundary is supported. A gap,
    overlap, or transition inside a service day leaves the cumulative account
    threshold unresolved rather than applying one version's seasons to the
    entire billing cycle.
    """

    start = aware_utc(cycle_start)
    end = aware_utc(cycle_end)
    if end <= start:
        raise RateWorkflowConflict("billing cycle range is invalid")
    zone = ZoneInfo(timezone)
    local_start = start.astimezone(zone)
    local_end = end.astimezone(zone)
    if (
        local_start.hour
        or local_start.minute
        or local_start.second
        or local_start.microsecond
        or local_end.hour
        or local_end.minute
        or local_end.second
        or local_end.microsecond
    ):
        raise RateWorkflowConflict("account tier thresholds require local-day cycle boundaries")

    assigned_versions = (
        await session.execute(
            select(RateAssignment, RatePlanVersion)
            .join(
                RatePlanVersion,
                RatePlanVersion.id == RateAssignment.rate_plan_version_id,
            )
            .where(
                RateAssignment.utility_account_id == utility_account_id,
                RateAssignment.effective_start < end,
                (RateAssignment.effective_end.is_(None) | (RateAssignment.effective_end > start)),
                RatePlanVersion.state == "published",
                RatePlanVersion.effective_start < end,
                (RatePlanVersion.effective_end.is_(None) | (RatePlanVersion.effective_end > start)),
            )
            .order_by(RateAssignment.effective_start, RateAssignment.id)
        )
    ).all()
    if not assigned_versions:
        return None
    plan_ids = {version.rate_plan_id for _assignment, version in assigned_versions}
    threshold_rows = (
        await session.scalars(
            select(UtilityAccountTierThreshold).where(
                UtilityAccountTierThreshold.utility_account_id == utility_account_id,
                UtilityAccountTierThreshold.rate_plan_id.in_(plan_ids),
                UtilityAccountTierThreshold.effective_start < end,
                (
                    UtilityAccountTierThreshold.effective_end.is_(None)
                    | (UtilityAccountTierThreshold.effective_end > start)
                ),
            )
        )
    ).all()
    if not threshold_rows:
        return None

    def covering_version(day_start: datetime, day_end: datetime) -> RatePlanVersion | None:
        matches = [
            version
            for assignment, version in assigned_versions
            if aware_utc(assignment.effective_start) <= day_start
            and (assignment.effective_end is None or aware_utc(assignment.effective_end) >= day_end)
            and aware_utc(version.effective_start) <= day_start
            and (version.effective_end is None or aware_utc(version.effective_end) >= day_end)
        ]
        if len(matches) > 1:
            raise RateWorkflowConflict("rate assignments overlap within a billing day")
        return matches[0] if matches else None

    def select_rule(
        *, at: datetime, rate_plan_id: str, season: str
    ) -> UtilityAccountTierThreshold | None:
        active = [
            item
            for item in threshold_rows
            if item.rate_plan_id == rate_plan_id
            and aware_utc(item.effective_start) <= at
            and (item.effective_end is None or aware_utc(item.effective_end) > at)
        ]
        exact = [item for item in active if item.season == season]
        fallback = [item for item in active if item.season == "all"]
        selected = exact or fallback
        if len(selected) > 1:
            raise RateWorkflowConflict("account tier-threshold evidence overlaps")
        return selected[0] if selected else None

    total = Decimal("0")
    evidence_ids: list[str] = []
    inclusive: bool | None = None
    local_day = local_start.date()
    while local_day < local_end.date():
        day_start_local = datetime(local_day.year, local_day.month, local_day.day, tzinfo=zone)
        next_local_day = local_day + timedelta(days=1)
        day_end_local = datetime(
            next_local_day.year,
            next_local_day.month,
            next_local_day.day,
            tzinfo=zone,
        )
        day_start_utc = day_start_local.astimezone(UTC)
        day_end_utc = day_end_local.astimezone(UTC)
        version = covering_version(day_start_utc, day_end_utc)
        if version is None or version.timezone != timezone:
            return None
        representative = day_start_utc + (day_end_utc - day_start_utc) / 2
        season = season_from_storage(
            version.season_definitions,
            representative.astimezone(ZoneInfo(version.timezone)),
        )
        first = select_rule(
            at=day_start_utc,
            rate_plan_id=version.rate_plan_id,
            season=season,
        )
        last = select_rule(
            at=day_end_utc - timedelta(microseconds=1),
            rate_plan_id=version.rate_plan_id,
            season=season,
        )
        if first is None or last is None or first.id != last.id:
            return None
        if first.kwh_per_day * first.source_billing_days != first.source_allowance_kwh:
            raise RateWorkflowConflict("stored account tier-threshold evidence does not reconcile")
        if inclusive is not None and first.tier1_boundary_inclusive is not inclusive:
            raise RateWorkflowConflict(
                "account tier-threshold boundary semantics change within the billing cycle"
            )
        inclusive = first.tier1_boundary_inclusive
        total += first.kwh_per_day
        if first.id not in evidence_ids:
            evidence_ids.append(first.id)
        local_day = next_local_day
    if inclusive is None:
        raise RateWorkflowConflict("billing cycle contains no local service days")
    return AccountCycleTierThreshold(total, inclusive, tuple(evidence_ids))


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
    if review.selected_plan_name is None:
        raise RateWorkflowConflict("candidate selected plan evidence is missing")
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
    threshold_rule = _canonical_tier_threshold_rule(
        review.tier_threshold_rule
        or selected_candidate_plan(candidate, review.selected_plan_name).get("tier_threshold_rule")
    )
    threshold: UtilityAccountTierThreshold | None = None
    if threshold_rule is not None:
        revision = await session.get(RateSourceRevision, candidate.source_revision_id)
        if revision is None:
            raise RateWorkflowConflict("candidate provenance revision is missing")
        threshold, _threshold_created = await replace_utility_account_tier_threshold(
            session,
            account=account,
            rate_plan_id=version.rate_plan_id,
            season=cast(str, threshold_rule["season"]),
            kwh_per_day=cast(Decimal, threshold_rule["kwh_per_day"]),
            source_allowance_kwh=cast(Decimal, threshold_rule["source_allowance_kwh"]),
            source_billing_days=cast(int, threshold_rule["source_billing_days"]),
            tier1_boundary_inclusive=cast(bool, threshold_rule["tier1_boundary_inclusive"]),
            source_label=cast(str, threshold_rule["source_label"]),
            source_kind="candidate_review",
            source_artifact_sha256=revision.artifact_sha256,
            effective_start=assignment.effective_start,
            effective_end=assignment.effective_end,
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
                "utility_account_tier_threshold_id": threshold.id if threshold else None,
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
    "replace_utility_account_tier_threshold",
    "resolve_assigned_utility_account_cycle_tier_threshold",
    "resolve_utility_account_cycle_tier_threshold",
    "resolve_utility_account_tier_threshold",
    "review_rate_candidate",
    "safe_review",
    "selected_candidate_plan",
]
