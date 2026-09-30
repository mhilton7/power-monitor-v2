from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    RateAssignment,
    RateDatedPrice,
    RateHoliday,
    RatePeriod,
    RatePlan,
    RatePlanVersion,
    UtilityAccount,
    aware_utc,
)
from .billing_usage import account_cycle_usage
from .cost_engine import (
    CostContext,
    DatedPrice,
    PricePeriod,
    RateVersion,
    effective_tier_bounds,
    event_calendar_from_evidence,
    holiday_calendar_from_evidence,
    resolve_price_period,
    season_definitions_from_storage,
    season_from_storage,
)
from .rate_workflow import resolve_assigned_utility_account_cycle_tier_threshold


def cycle_bounds(account: UtilityAccount, now: datetime) -> tuple[datetime, datetime]:
    zone = ZoneInfo(account.timezone)
    local = now.astimezone(zone)
    year, month = local.year, local.month
    if local.day < account.billing_day:
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    start = datetime(year, month, account.billing_day, tzinfo=zone)
    end = datetime(year + int(month == 12), month % 12 + 1, account.billing_day, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


async def _domain_rate(session: AsyncSession, version: RatePlanVersion) -> RateVersion:
    periods = (
        await session.scalars(
            select(RatePeriod).where(
                RatePeriod.rate_plan_version_id == version.id,
            )
        )
    ).all()
    dated = (
        await session.scalars(
            select(RateDatedPrice).where(
                RateDatedPrice.rate_plan_version_id == version.id,
            )
        )
    ).all()
    holidays = frozenset(
        (
            await session.scalars(
                select(RateHoliday.local_date).where(
                    RateHoliday.rate_plan_version_id == version.id,
                )
            )
        ).all()
    )
    calendar = holiday_calendar_from_evidence(version.eligibility_evidence)
    if calendar is not None and calendar.local_dates != holidays:
        raise ValueError("stored holiday calendar does not match its persisted holiday rows")
    return RateVersion(
        id=version.id,
        rate_plan_id=version.rate_plan_id,
        timezone=version.timezone,
        effective_start=aware_utc(version.effective_start),
        effective_end=aware_utc(version.effective_end) if version.effective_end else None,
        periods=tuple(
            PricePeriod(
                season=p.season,
                day_type=p.day_type,
                name=p.period_name,
                start_minute=p.start_minute,
                end_minute=p.end_minute,
                price_per_kwh=p.price_per_kwh,
                tier_start_kwh=p.tier_start_kwh,
                tier_end_kwh=p.tier_end_kwh,
                boundary_inclusive=p.boundary_inclusive,
                threshold_basis=p.threshold_basis,
            )
            for p in periods
        ),
        dated_prices=tuple(
            DatedPrice(
                start_utc=aware_utc(p.start_utc),
                end_utc=aware_utc(p.end_utc),
                name=p.source_label,
                price_per_kwh=p.price_per_kwh,
            )
            for p in dated
        ),
        season_definitions=season_definitions_from_storage(version.season_definitions),
        holiday_treatment=version.holiday_treatment,
        holiday_calendar=calendar,
        event_calendar=event_calendar_from_evidence(version.eligibility_evidence),
        tier_threshold_kwh_per_day=version.tier_threshold_kwh_per_day,
        tier_threshold_season=version.tier_threshold_season,
        tier_threshold_source_kwh=version.tier_threshold_source_kwh,
        tier1_boundary_inclusive=version.tier1_boundary_inclusive,
    )


def _needs_usage(rate: RateVersion, version: RatePlanVersion, account: UtilityAccount) -> bool:
    return (
        version.pricing_model in ("tiered", "seasonal_tiered", "tiered_time_of_use")
        or any(p.tier_start_kwh > 0 or p.tier_end_kwh is not None for p in rate.periods)
        or (account.cost_scope == "full_account" and version.baseline_credit_per_kwh > 0)
    )


def _price(
    period: PricePeriod, version: RatePlanVersion, account: UtilityAccount, usage: Decimal
) -> Decimal:
    value = period.price_per_kwh + version.cca_adjustment_per_kwh
    value += value * version.surcharge_percent / Decimal(100)
    if (
        account.cost_scope == "full_account"
        and account.baseline_allocation_kwh is not None
        and usage < account.baseline_allocation_kwh
    ):
        value -= version.baseline_credit_per_kwh
    return max(Decimal("0"), value)


def _schedule_instants(rate: RateVersion, now: datetime) -> set[datetime]:
    """Bounded weekly schedule look-ahead, with both folds and real DST transitions."""
    if rate.dated_prices:
        return {
            instant
            for p in rate.dated_prices
            for instant in (p.start_utc, p.end_utc)
            if instant > now
        }
    zone = ZoneInfo(rate.timezone)
    day = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = {
        0,
        1440,
        *(p.start_minute for p in rate.periods),
        *(p.end_minute for p in rate.periods),
    }
    instants: set[datetime] = set()
    local_days = {day}
    # A weekly look-ahead proves ordinary weekday/weekend transitions, but not a
    # season, holiday, or event beyond that week. Include those explicit calendar
    # boundaries before comparing a farther assignment or billing-cycle endpoint.
    for year in (day.year, day.year + 1):
        for month in range(1, 13):
            local_days.add(datetime(year, month, 1, tzinfo=zone))
        for season in rate.season_definitions:
            for month, month_day, following_day in (
                (season.start_month, season.start_day, False),
                (season.end_month, season.end_day, True),
            ):
                try:
                    boundary = datetime(year, month, month_day, tzinfo=zone)
                except ValueError:
                    # February 29 definitions in a common year are covered by
                    # the March 1 month boundary above.
                    continue
                if following_day:
                    boundary += timedelta(days=1)
                local_days.add(boundary)
    for calendar in (rate.holiday_calendar, rate.event_calendar):
        if calendar is None:
            continue
        for local_date in (*calendar.local_dates, calendar.coverage_end):
            boundary = datetime(local_date.year, local_date.month, local_date.day, tzinfo=zone)
            for offset in (0, 1):
                local_days.add(boundary + timedelta(days=offset))
    # A new season can begin on a weekend without changing that day's price.
    # Cover its whole weekly pattern before considering a later calendar anchor.
    local_days = {
        boundary + timedelta(days=offset) for boundary in local_days for offset in range(8)
    }
    # A spring jump may skip a tariff boundary completely; evaluation must happen
    # at the actual offset transition, not at the normalized nonexistent time.
    for local_day in local_days:
        if local_day + timedelta(days=1) <= now:
            continue
        for minute in minutes:
            local = local_day + timedelta(minutes=minute)
            for fold in (0, 1):
                instant = local.replace(fold=fold).astimezone(UTC)
                if instant > now:
                    instants.add(instant)
        cursor = local_day.astimezone(UTC)
        day_end = (local_day + timedelta(days=1)).astimezone(UTC)
        while cursor < day_end:
            end = min(day_end, cursor + timedelta(hours=1))
            if cursor.astimezone(zone).utcoffset() != end.astimezone(zone).utcoffset():
                probe = cursor
                while probe < end:
                    following = probe + timedelta(minutes=1)
                    if probe.astimezone(zone).utcoffset() != following.astimezone(zone).utcoffset():
                        if following > now:
                            instants.add(following)
                        break
                    probe = following
            cursor = end
    return instants


async def current_pricing(
    session: AsyncSession,
    home_id: str,
    now: datetime,
) -> tuple[dict[str, object] | None, str, datetime | None]:
    accounts = (
        await session.scalars(
            select(UtilityAccount)
            .where(
                UtilityAccount.home_id == home_id,
            )
            .limit(2)
        )
    ).all()
    if len(accounts) != 1:
        return None, "unconfigured" if not accounts else "unavailable", None
    account = accounts[0]
    assignments = (
        await session.execute(
            select(RateAssignment, RatePlanVersion, RatePlan)
            .join(RatePlanVersion, RatePlanVersion.id == RateAssignment.rate_plan_version_id)
            .join(RatePlan, RatePlan.id == RatePlanVersion.rate_plan_id)
            .where(
                RateAssignment.utility_account_id == account.id,
                RatePlanVersion.state == "published",
            )
        )
    ).all()
    active = [
        row
        for row in assignments
        if aware_utc(row[0].effective_start) <= now
        and (row[0].effective_end is None or aware_utc(row[0].effective_end) > now)
        and aware_utc(row[1].effective_start) <= now
        and (row[1].effective_end is None or aware_utc(row[1].effective_end) > now)
    ]
    scheduled = {
        aware_utc(value)
        for assignment, version, _plan in assignments
        for value in (
            assignment.effective_start,
            assignment.effective_end,
            version.effective_start,
            version.effective_end,
        )
        if value is not None and aware_utc(value) > now
    }
    if len(active) != 1:
        return None, "unavailable" if active else "unconfigured", min(scheduled, default=None)
    assignment, version, plan = active[0]
    cycle_start, cycle_end = cycle_bounds(account, now)
    scope_end = now.replace(second=0, microsecond=0)
    usage = await account_cycle_usage(session, account, cycle_start, scope_end)
    local = now.astimezone(ZoneInfo(version.timezone))
    season = season_from_storage(version.season_definitions, local)
    cycle_days = (
        cycle_end.astimezone(ZoneInfo(account.timezone)).date()
        - cycle_start.astimezone(ZoneInfo(account.timezone)).date()
    ).days
    threshold = await resolve_assigned_utility_account_cycle_tier_threshold(
        session,
        utility_account_id=account.id,
        timezone=account.timezone,
        cycle_start=cycle_start,
        cycle_end=cycle_end,
    )
    context = CostContext(
        cumulative_cycle_kwh_before=usage.current_kwh,
        billing_cycle_days=cycle_days,
        tier_threshold_cycle_kwh=threshold.total_kwh if threshold else None,
        tier_threshold_season=season,
        tier1_boundary_inclusive=threshold.tier1_boundary_inclusive if threshold else True,
    )
    period: PricePeriod | None = None
    domain: RateVersion | None = None
    tier_confirmed = False
    needs_usage = True
    allowance: Decimal | None = None
    current_tier_start: Decimal | None = None
    current_tier_end: Decimal | None = None
    tier: int | None = None
    known_tou_period: str | None = None
    reasons: list[dict[str, str]] = []
    try:
        domain = await _domain_rate(session, version)
        needs_usage = _needs_usage(domain, version, account)
        if domain.tier_threshold_kwh_per_day is not None and not (
            aware_utc(assignment.effective_start) <= cycle_start
            and (
                assignment.effective_end is None or aware_utc(assignment.effective_end) >= cycle_end
            )
            and aware_utc(version.effective_start) <= cycle_start
            and (version.effective_end is None or aware_utc(version.effective_end) >= cycle_end)
        ):
            raise ValueError("legacy daily allowance does not cover the full billing cycle")
        tier_confirmed = not needs_usage or usage.resolved
        if tier_confirmed:
            period = resolve_price_period(
                domain, now, usage.current_kwh, context, incremental_energy=True
            )
            if needs_usage and usage.estimated_kwh:
                lower = resolve_price_period(
                    domain, now, usage.lower_kwh, context, incremental_energy=True
                )
                upper = resolve_price_period(
                    domain, now, usage.upper_kwh, context, incremental_energy=True
                )
                if lower != upper or _price(lower, version, account, usage.lower_kwh) != _price(
                    upper, version, account, usage.upper_kwh
                ):
                    period = None
                    tier_confirmed = False
        bounds = [
            effective_tier_bounds(domain, p, context)
            for p in domain.periods
            if p.season in (season, "all")
        ]
        starts = sorted({start for start, _end in bounds})
        clock_period_names = {
            resolve_price_period(domain, now, start, context, incremental_energy=True).name
            for start in starts
        }
        if len(clock_period_names) == 1:
            known_tou_period = next(iter(clock_period_names))
        allowance = min((end for _start, end in bounds if end is not None), default=None)
        if period is not None and (len(starts) > 1 or allowance is not None):
            current_tier_start, current_tier_end = effective_tier_bounds(domain, period, context)
            tier = starts.index(current_tier_start) + 1
    except ValueError:
        period = None
        tier_confirmed = False
        reasons.append(
            {
                "code": "rate_schedule_unresolved",
                "message": (
                    "The approved schedule or account baseline cannot resolve one exact price."
                ),
            }
        )
    if needs_usage and not usage.member_ids:
        reasons.append(
            {
                "code": "billing_source_service_branch_not_configured",
                "message": (
                    "A verified Main service billing source is required for the account tier."
                ),
            }
        )
    if needs_usage and usage.unknown_gap_count:
        reasons.append(
            {
                "code": "unknown_gap_energy",
                "message": "Incomplete account energy prevents a confirmed current tier.",
            }
        )
    if needs_usage and usage.unresolved_counter_reset_count:
        reasons.append(
            {
                "code": "unresolved_counter_reset",
                "message": "An unresolved meter reset prevents a confirmed account tier.",
            }
        )
    if usage.recovered_kwh:
        reasons.append(
            {
                "code": "cumulative_energy_recovered",
                "message": "Gap energy is included from the authenticated cumulative meter total.",
            }
        )
    if usage.estimated_kwh:
        reasons.append(
            {
                "code": "estimated_missing_energy",
                "message": "The account usage includes an explicitly bounded short-gap estimate.",
            }
        )
    effective_price = _price(period, version, account, usage.current_kwh) if period else None
    next_change: datetime | None = None
    next_price: Decimal | None = None
    next_period: str | None = None
    midnight = (
        local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    ).astimezone(UTC)
    refresh = min({midnight, cycle_end, *scheduled})
    if domain is not None:
        candidates = sorted({*_schedule_instants(domain, now), *scheduled, cycle_end})
        for instant in candidates:
            future = [
                row
                for row in assignments
                if aware_utc(row[0].effective_start) <= instant
                and (row[0].effective_end is None or aware_utc(row[0].effective_end) > instant)
                and aware_utc(row[1].effective_start) <= instant
                and (row[1].effective_end is None or aware_utc(row[1].effective_end) > instant)
            ]
            if len(future) != 1:
                next_change = instant
                break
            future_assignment, future_version, _ = future[0]
            identity_changes = future_assignment.id != assignment.id
            try:
                future_domain = (
                    domain
                    if future_version.id == version.id
                    else await _domain_rate(session, future_version)
                )
                future_usage = Decimal("0") if instant == cycle_end else usage.current_kwh
                future_period = resolve_price_period(
                    future_domain,
                    instant,
                    future_usage,
                    context,
                    incremental_energy=True,
                )
                if period is None:
                    changed = (
                        identity_changes
                        or instant == cycle_end
                        or (known_tou_period is not None and future_period.name != known_tou_period)
                    )
                else:
                    changed = identity_changes or (
                        (
                            future_period.name,
                            _price(future_period, future_version, account, future_usage),
                            effective_tier_bounds(future_domain, future_period, context),
                        )
                        != (
                            period.name,
                            effective_price,
                            effective_tier_bounds(domain, period, context),
                        )
                    )
                if not changed:
                    continue
                next_change = instant
                next_period = (
                    None
                    if instant >= cycle_end and _needs_usage(future_domain, future_version, account)
                    else future_period.name
                )
                # No clock-time forecast for a usage-dependent tier crossing. A
                # future hybrid price can differ if consumption crosses a tier.
                if not _needs_usage(future_domain, future_version, account):
                    next_price = _price(future_period, future_version, account, future_usage)
            except ValueError:
                next_change = instant
            break
    if next_change is not None:
        refresh = min(refresh, next_change)
    state = (
        "available"
        if effective_price is not None
        else "incomplete_usage"
        if needs_usage
        and not tier_confirmed
        and not any(reason["code"] == "rate_schedule_unresolved" for reason in reasons)
        else "unavailable"
    )
    has_tou = version.pricing_model in (
        "time_of_use",
        "seasonal_time_of_use",
        "time_of_use_with_baseline_credit",
        "tiered_time_of_use",
        "tiered_tou",
        "hybrid",
    ) or bool(domain and any(p.start_minute != 0 or p.end_minute != 1440 for p in domain.periods))
    return (
        {
            "plan_name": plan.name,
            "version_id": version.id,
            "assignment_id": assignment.id,
            "account_id": account.id,
            "effective_start": aware_utc(version.effective_start),
            "evaluated_at": now,
            "timezone": account.timezone,
            "pricing_model": version.pricing_model,
            "pricing_state": state,
            "period": period.name if period else None,
            "tou_period": (period.name if period else known_tou_period) if has_tou else None,
            "current_tier": tier,
            "tier_state": (
                f"{'estimated_' if usage.estimated_kwh else ''}tier_{tier}"
                if tier
                else "not_confirmed"
                if needs_usage
                else None
            ),
            "tier_confirmed": tier_confirmed,
            "tier_confirmation_rule": "account_cycle_sensor_evidence"
            if needs_usage
            else "not_applicable",
            "reading_coverage": usage.reading_coverage,
            "measured_cycle_kwh": usage.measured_kwh if usage.has_energy_evidence else None,
            "recovered_gap_energy_kwh": usage.recovered_kwh,
            "unknown_gap_count": usage.unknown_gap_count,
            "unresolved_counter_reset_count": usage.unresolved_counter_reset_count,
            "availability_reasons": reasons,
            "tier_1_allowance_kwh": allowance,
            "price_per_kwh": effective_price,
            "base_price_per_kwh": period.price_per_kwh if period else None,
            "cca_adjustment_per_kwh": version.cca_adjustment_per_kwh,
            "surcharge_percent": version.surcharge_percent,
            "applied_baseline_credit_per_kwh": (
                version.baseline_credit_per_kwh
                if period is not None
                and account.cost_scope == "full_account"
                and account.baseline_allocation_kwh is not None
                and usage.current_kwh < account.baseline_allocation_kwh
                else Decimal("0")
            ),
            "marginal_fixed_charges_included": False,
            "cumulative_cycle_kwh": usage.current_kwh if usage.has_energy_evidence else None,
            "cycle_usage_kwh": usage.current_kwh if usage.has_energy_evidence else None,
            "cycle_start": cycle_start,
            "cycle_end": cycle_end,
            "usage_scope": "verified_billing_source" if usage.member_ids else "unavailable",
            "usage_device_ids": usage.member_ids,
            "remaining_tier_kwh": max(Decimal("0"), current_tier_end - usage.current_kwh)
            if current_tier_end is not None and tier_confirmed
            else None,
            "tier_progress_percent": min(
                Decimal("100"),
                max(Decimal("0"), usage.current_kwh - current_tier_start)
                / (current_tier_end - current_tier_start)
                * 100,
            )
            if current_tier_end is not None
            and current_tier_start is not None
            and current_tier_end > current_tier_start
            and tier_confirmed
            else None,
            "period_start_minute": period.start_minute if period else None,
            "period_end_minute": period.end_minute if period else None,
            "next_change_at": next_change,
            "next_price_per_kwh": next_price,
            "next_period": next_period,
            "next_pricing_refresh_at": refresh,
            "scope": account.cost_scope,
            "fixed_charges_included": account.cost_scope == "full_account",
            "baseline_credit_included": account.cost_scope == "full_account"
            and version.baseline_credit_per_kwh > 0,
            "cca_or_direct_access": account.cca_provider,
        },
        state,
        refresh,
    )
