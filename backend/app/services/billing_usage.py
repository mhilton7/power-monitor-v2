from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import case, extract, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    Alert,
    BillingCycleAdjustment,
    Circuit,
    Device,
    NormalizedInterval,
    RawReading,
    TelemetryEnergyEvent,
    UtilityAccount,
    aware_utc,
)


async def estimate_short_gap_energy(
    session: AsyncSession,
    *,
    events: list[TelemetryEnergyEvent],
    cycle_start: datetime,
    scope_end: datetime,
    reading_coverage: Decimal,
    minimum_coverage: Decimal,
    maximum_gap_seconds: int,
    unresolved_counter_resets: int,
) -> dict[str, object]:
    """Estimate billing-only energy without creating or changing History rows."""

    estimated_mwh = Decimal("0")
    lower_mwh = Decimal("0")
    upper_mwh = Decimal("0")
    estimated_seconds = 0
    unknown_seconds = 0
    unknown_count = 0
    methods: set[str] = set()
    details: list[dict[str, object]] = []
    for event in events:
        gap_start = aware_utc(event.gap_start_utc) if event.gap_start_utc else None
        gap_end = aware_utc(event.gap_end_utc) if event.gap_end_utc else None
        duration_seconds = (
            max(0, int((gap_end - gap_start).total_seconds()))
            if gap_start is not None and gap_end is not None and gap_end > gap_start
            else 0
        )
        blocked_reason: str | None = None
        if gap_start is None or gap_end is None or duration_seconds <= 0:
            blocked_reason = "gap_bounds_unavailable"
        elif (
            gap_start < cycle_start
            or gap_end > scope_end
            or bool(event.evidence.get("crosses_billing_cycle") is True)
        ):
            blocked_reason = "gap_crosses_billing_cycle_boundary"
        elif unresolved_counter_resets:
            blocked_reason = "unresolved_counter_reset"
        elif reading_coverage < minimum_coverage:
            blocked_reason = "reading_coverage_below_minimum_threshold"
        elif duration_seconds > maximum_gap_seconds:
            blocked_reason = "gap_exceeds_maximum_estimatable_duration"

        before: NormalizedInterval | None = None
        after: NormalizedInterval | None = None
        if blocked_reason is None:
            assert gap_start is not None and gap_end is not None
            before = (
                await session.scalars(
                    select(NormalizedInterval)
                    .where(
                        NormalizedInterval.device_id == event.device_id,
                        NormalizedInterval.source_authenticated.is_(True),
                        NormalizedInterval.energy_mwh.is_not(None),
                        NormalizedInterval.end_utc <= gap_start,
                        NormalizedInterval.end_utc
                        >= gap_start - timedelta(seconds=maximum_gap_seconds),
                    )
                    .order_by(NormalizedInterval.end_utc.desc())
                    .limit(1)
                )
            ).first()
            after = (
                await session.scalars(
                    select(NormalizedInterval)
                    .where(
                        NormalizedInterval.device_id == event.device_id,
                        NormalizedInterval.source_authenticated.is_(True),
                        NormalizedInterval.energy_mwh.is_not(None),
                        NormalizedInterval.start_utc >= gap_end,
                        NormalizedInterval.start_utc
                        <= gap_end + timedelta(seconds=maximum_gap_seconds),
                    )
                    .order_by(NormalizedInterval.start_utc)
                    .limit(1)
                )
            ).first()
            if before is None or after is None:
                blocked_reason = "neighboring_intervals_unavailable"

        if blocked_reason is not None:
            unknown_count += 1
            unknown_seconds += duration_seconds
            details.append(
                {
                    "event_id": event.id,
                    "status": "unknown",
                    "duration_seconds": duration_seconds,
                    "reason": blocked_reason,
                }
            )
            continue

        assert before is not None and after is not None
        before_seconds = Decimal(str((before.end_utc - before.start_utc).total_seconds()))
        after_seconds = Decimal(str((after.end_utc - after.start_utc).total_seconds()))
        if before_seconds <= 0 or after_seconds <= 0:
            unknown_count += 1
            unknown_seconds += duration_seconds
            details.append(
                {
                    "event_id": event.id,
                    "status": "unknown",
                    "duration_seconds": duration_seconds,
                    "reason": "neighboring_interval_duration_invalid",
                }
            )
            continue
        before_rate = Decimal(before.energy_mwh or 0) / before_seconds
        after_rate = Decimal(after.energy_mwh or 0) / after_seconds
        gap_seconds = Decimal(duration_seconds)
        estimate = (((before_rate + after_rate) / Decimal(2)) * gap_seconds).quantize(
            Decimal("0.000001")
        )
        lower = (min(before_rate, after_rate) * gap_seconds).quantize(Decimal("0.000001"))
        upper = (max(before_rate, after_rate) * gap_seconds).quantize(Decimal("0.000001"))
        estimated_mwh += estimate
        lower_mwh += lower
        upper_mwh += upper
        estimated_seconds += duration_seconds
        methods.add("short_gap_neighbor_interpolation")
        details.append(
            {
                "event_id": event.id,
                "status": "estimated",
                "duration_seconds": duration_seconds,
                "method": "short_gap_neighbor_interpolation",
                "energy_kwh": estimate / Decimal(1_000_000),
                "lower_kwh": lower / Decimal(1_000_000),
                "upper_kwh": upper / Decimal(1_000_000),
            }
        )
    return {
        "estimated_mwh": estimated_mwh,
        "lower_mwh": lower_mwh,
        "upper_mwh": upper_mwh,
        "estimated_seconds": estimated_seconds,
        "unknown_seconds": unknown_seconds,
        "unknown_count": unknown_count,
        "methods": tuple(sorted(methods)),
        "details": details,
        "raw_history_modified": False,
    }


@dataclass(frozen=True)
class AccountCycleUsage:
    member_ids: tuple[str, ...]
    measured_kwh: Decimal
    recovered_kwh: Decimal
    adjustment_kwh: Decimal
    estimated_kwh: Decimal
    estimate_lower_kwh: Decimal
    estimate_upper_kwh: Decimal
    reading_coverage: Decimal
    unknown_gap_count: int
    unresolved_counter_reset_count: int
    has_energy_evidence: bool

    @property
    def current_kwh(self) -> Decimal:
        return self.measured_kwh + self.recovered_kwh + self.adjustment_kwh + self.estimated_kwh

    @property
    def lower_kwh(self) -> Decimal:
        return self.current_kwh - self.estimated_kwh + self.estimate_lower_kwh

    @property
    def upper_kwh(self) -> Decimal:
        return self.current_kwh - self.estimated_kwh + self.estimate_upper_kwh

    @property
    def resolved(self) -> bool:
        return bool(self.member_ids) and not (
            self.unknown_gap_count or self.unresolved_counter_reset_count
        )


async def account_cycle_usage(
    session: AsyncSession,
    account: UtilityAccount,
    cycle_start: datetime,
    scope_end: datetime,
) -> AccountCycleUsage:
    """Read account-authorized sensor evidence, without pricing historical intervals.

    Selection of a chart or sensor never changes the account's tariff usage basis.
    Aggregate SQL avoids materializing saved readings in the API process; only
    bounded gap evidence needs per-gap neighboring-interval reads. The database
    still sums the account's current-cycle range. Corrections and bounded gap
    estimates retain the existing Billing rules/provenance.
    """
    member_ids = tuple(
        (
            await session.scalars(
                select(Device.id)
                .join(Circuit, Circuit.id == Device.circuit_id)
                .where(
                    Circuit.home_id == account.home_id,
                    Circuit.is_home_total.is_(True),
                    Circuit.is_billing_source.is_(True),
                    Circuit.aggregate_mode == "verified_sum",
                    Circuit.non_overlapping_confirmed.is_(True),
                    Device.home_id == account.home_id,
                    Device.include_in_aggregate.is_(True),
                )
                .order_by(Device.id)
            )
        ).all()
    )
    adjustment = await session.scalar(
        select(BillingCycleAdjustment).where(
            BillingCycleAdjustment.utility_account_id == account.id,
            BillingCycleAdjustment.cycle_start_utc == cycle_start,
            BillingCycleAdjustment.reason == "verified_cycle_to_date_seed",
        )
    )
    automatic_start = cycle_start
    adjustment_kwh = Decimal("0")
    if adjustment is not None:
        through = adjustment.evidence.get("through_utc")
        if isinstance(through, str):
            parsed = datetime.fromisoformat(through.replace("Z", "+00:00"))
            if parsed.utcoffset() is not None and cycle_start <= parsed <= scope_end:
                automatic_start = parsed.astimezone(UTC)
                adjustment_kwh = Decimal(adjustment.energy_mwh) / Decimal(1_000_000)
    expected = Decimal(str((scope_end - cycle_start).total_seconds())) * len(member_ids)
    seeded_seconds = Decimal(str((automatic_start - cycle_start).total_seconds())) * len(member_ids)
    duration = extract("epoch", NormalizedInterval.end_utc) - extract(
        "epoch", NormalizedInterval.start_utc
    )
    energy_mwh, reliable_seconds = (
        await session.execute(
            select(
                func.sum(NormalizedInterval.energy_mwh),
                func.sum(
                    case(
                        (
                            NormalizedInterval.energy_mwh.is_not(None),
                            duration * NormalizedInterval.completeness,
                        ),
                        else_=0,
                    )
                ),
            )
            .outerjoin(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(
                NormalizedInterval.device_id.in_(member_ids),
                or_(
                    NormalizedInterval.source_kind == "stateless_v2",
                    RawReading.reset_generation == Device.reset_generation,
                ),
                NormalizedInterval.source_authenticated.is_(True),
                NormalizedInterval.start_utc >= automatic_start,
                NormalizedInterval.end_utc <= scope_end,
            )
        )
    ).one()
    reliable = Decimal(str(reliable_seconds or 0)) + seeded_seconds
    coverage = min(Decimal("1"), reliable / expected) if expected else Decimal(bool(member_ids))
    events = list(
        (
            await session.scalars(
                select(TelemetryEnergyEvent).where(
                    TelemetryEnergyEvent.device_id.in_(member_ids),
                    TelemetryEnergyEvent.billing_status.in_(("included", "unresolved")),
                    TelemetryEnergyEvent.gap_end_utc > automatic_start,
                    TelemetryEnergyEvent.gap_start_utc < scope_end,
                )
            )
        ).all()
    )
    recovered = [
        event
        for event in events
        if event.billing_status == "included"
        and event.gap_end_utc is not None
        and aware_utc(event.gap_end_utc) <= scope_end
    ]
    unresolved = [event for event in events if event.billing_status == "unresolved"]
    recovered_seconds = sum(
        max(0, int((aware_utc(event.gap_end_utc) - aware_utc(event.gap_start_utc)).total_seconds()))
        for event in recovered
        if event.gap_start_utc is not None and event.gap_end_utc is not None
    )
    resets = int(
        await session.scalar(
            select(func.count(Alert.id)).where(
                Alert.device_id.in_(member_ids),
                Alert.alert_type == "pzem_energy_counter_reset",
                Alert.state == "open",
                Alert.opened_at >= automatic_start,
                Alert.opened_at < scope_end,
            )
        )
        or 0
    )
    estimate = await estimate_short_gap_energy(
        session,
        events=unresolved,
        cycle_start=automatic_start,
        scope_end=scope_end,
        reading_coverage=coverage,
        minimum_coverage=account.estimate_min_coverage,
        maximum_gap_seconds=account.max_estimatable_gap_seconds,
        unresolved_counter_resets=resets,
    )
    missing_seconds = max(Decimal("0"), expected - reliable)
    classified_seconds = (
        recovered_seconds
        + cast(int, estimate["estimated_seconds"])
        + cast(int, estimate["unknown_seconds"])
    )
    unknown = cast(int, estimate["unknown_count"]) + int(missing_seconds > classified_seconds)
    return AccountCycleUsage(
        member_ids=member_ids,
        measured_kwh=Decimal(energy_mwh or 0) / Decimal(1_000_000),
        recovered_kwh=Decimal(sum(int(event.recovered_energy_mwh or 0) for event in recovered))
        / Decimal(1_000_000),
        adjustment_kwh=adjustment_kwh,
        estimated_kwh=Decimal(str(estimate["estimated_mwh"])) / Decimal(1_000_000),
        estimate_lower_kwh=Decimal(str(estimate["lower_mwh"])) / Decimal(1_000_000),
        estimate_upper_kwh=Decimal(str(estimate["upper_mwh"])) / Decimal(1_000_000),
        reading_coverage=coverage,
        unknown_gap_count=unknown,
        unresolved_counter_reset_count=resets,
        has_energy_evidence=bool(
            member_ids
            and (
                energy_mwh is not None
                or recovered
                or adjustment_kwh
                or automatic_start > cycle_start
                or scope_end == cycle_start
            )
        ),
    )
