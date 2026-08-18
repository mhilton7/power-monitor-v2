from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from backend.app.main import session_factory
from backend.app.models import (
    BillingEstimate,
    Circuit,
    CostRun,
    Device,
    Home,
    IntervalCost,
    IntervalCostSelection,
    NormalizedInterval,
    RateAssignment,
    RatePeriod,
    RatePlan,
    RatePlanVersion,
    RawReading,
    User,
    UtilityAccount,
)
from backend.app.schemas.device import DurableReading, ReadingBatchRequest
from backend.app.services.ingestion import ingest_batch
from backend.app.services.rate_workflow import replace_rate_assignment
from httpx import AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from worker.app.jobs import calculate_billing_estimates, calculate_pending_costs


def _backlog_record(*, sequence: int, start: datetime, energy_mwh: int) -> DurableReading:
    return DurableReading(
        sequence=sequence,
        reset_generation=0,
        interval_start_utc=start,
        interval_end_utc=start + timedelta(minutes=1),
        monotonic_start_us=sequence * 60_000_000,
        monotonic_end_us=(sequence + 1) * 60_000_000,
        sample_count=60,
        expected_sample_count=60,
        voltage_mv=120_000,
        current_ma=1_000,
        active_power_mw=600_000,
        frequency_mhz=60_000,
        power_factor_milli=1_000,
        pzem_energy_wh=sequence,
        interval_energy_mwh=energy_mwh,
        energy_selection="pzem_delta",
        pzem_status="ok",
        time_trusted=True,
        flags=[],
        record_crc32=sequence,
    )


async def _published_rate(
    session: AsyncSession,
    *,
    plan: RatePlan,
    version_number: int,
    effective_start: datetime,
    price: Decimal,
    baseline_credit: Decimal = Decimal("0"),
    daily_fixed_charge: Decimal = Decimal("0"),
) -> RatePlanVersion:
    version = RatePlanVersion(
        rate_plan_id=plan.id,
        version=version_number,
        effective_start=effective_start,
        timezone="America/Los_Angeles",
        pricing_model="time_of_use",
        baseline_credit_per_kwh=baseline_credit,
        daily_fixed_charge=daily_fixed_charge,
        source_hash=f"{version_number:x}" * 64,
        algorithm_version="cost-v1",
        state="draft",
    )
    session.add(version)
    await session.flush()
    session.add(
        RatePeriod(
            rate_plan_version_id=version.id,
            season="all",
            day_type="all",
            period_name="flat",
            start_minute=0,
            end_minute=1440,
            price_per_kwh=price,
        )
    )
    await session.flush()
    version.state = "published"
    await session.flush()
    return version


async def _interval(
    session: AsyncSession,
    *,
    device: Device,
    start: datetime,
    energy_mwh: int,
    sequence: int = 1,
) -> NormalizedInterval:
    end = start + timedelta(minutes=1)
    raw = RawReading(
        device_id=device.id,
        sequence=sequence,
        reset_generation=device.reset_generation,
        interval_start_utc=start,
        interval_end_utc=end,
        monotonic_start_us=1,
        monotonic_end_us=60_000_001,
        sample_count=60,
        expected_sample_count=60,
        voltage_mv=120_000,
        current_ma=1_000,
        active_power_mw=energy_mwh,
        frequency_mhz=60_000,
        power_factor_milli=1_000,
        pzem_energy_wh=sequence,
        interval_energy_mwh=energy_mwh,
        energy_selection="pzem_register_delta",
        pzem_status="ok",
        time_trusted=True,
        flags=[],
        record_crc32=sequence,
        payload_sha256=f"{sequence:x}" * 64,
    )
    session.add(raw)
    await session.flush()
    interval = NormalizedInterval(
        device_id=device.id,
        raw_reading_id=raw.id,
        start_utc=start,
        end_utc=end,
        energy_mwh=energy_mwh,
        average_power_mw=energy_mwh,
        completeness=Decimal("1"),
        energy_selection="pzem_register_delta",
        algorithm_version="normalize-v1",
        source_authenticated=True,
    )
    session.add(interval)
    await session.flush()
    return interval


@pytest.mark.asyncio
async def test_repricing_preserves_old_cost_and_selects_only_new_rate(
    owner_client: AsyncClient,
) -> None:
    async with session_factory() as session:
        home = await session.scalar(select(Home))
        account = await session.scalar(select(UtilityAccount))
        user_id = await session.scalar(select(User.id))
        assert home is not None and account is not None and user_id is not None
        device = Device(
            home_id=home.id,
            friendly_name="Main",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        plan = RatePlan(name="Immutable plan", utility_name="SCE", rate_class="test")
        session.add_all((device, plan))
        await session.flush()
        first = await _published_rate(
            session,
            plan=plan,
            version_number=1,
            effective_start=datetime(2026, 1, 1, tzinfo=UTC),
            price=Decimal("0.10"),
        )
        await replace_rate_assignment(
            session,
            account=account,
            version=first,
            actor_user_id=user_id,
        )
        await session.flush()
        interval = await _interval(
            session,
            device=device,
            start=datetime(2026, 8, 13, 20, tzinfo=UTC),
            energy_mwh=1_000_000,
        )
        assert await calculate_pending_costs(session) == 1
        await session.commit()

        second = await _published_rate(
            session,
            plan=plan,
            version_number=2,
            effective_start=datetime(2026, 8, 1, tzinfo=UTC),
            price=Decimal("0.30"),
        )
        await replace_rate_assignment(
            session,
            account=account,
            version=second,
            actor_user_id=user_id,
        )
        await session.flush()
        await session.execute(
            delete(IntervalCostSelection).where(
                IntervalCostSelection.normalized_interval_id == interval.id
            )
        )
        assert await calculate_pending_costs(session) == 1
        await session.commit()

        assert await session.scalar(select(func.count(IntervalCost.id))) == 2
        selected = await session.scalar(
            select(IntervalCost)
            .join(
                IntervalCostSelection,
                IntervalCostSelection.interval_cost_id == IntervalCost.id,
            )
            .where(IntervalCostSelection.normalized_interval_id == interval.id)
        )
        assert selected is not None
        assert selected.rate_plan_version_id == second.id
        assert selected.energy_cost_microdollars == 300_000


@pytest.mark.asyncio
async def test_late_backlog_reprices_later_tier_usage_without_mutating_evidence(
    owner_client: AsyncClient,
) -> None:
    async with session_factory() as session:
        home = await session.scalar(select(Home))
        account = await session.scalar(select(UtilityAccount))
        user_id = await session.scalar(select(User.id))
        assert home is not None and account is not None and user_id is not None
        device = Device(
            home_id=home.id,
            friendly_name="Backlog meter",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        plan = RatePlan(name="Tiered", utility_name="SCE", rate_class="test")
        session.add_all((device, plan))
        await session.flush()
        version = RatePlanVersion(
            rate_plan_id=plan.id,
            version=1,
            effective_start=datetime(2026, 1, 1, tzinfo=UTC),
            timezone="America/Los_Angeles",
            pricing_model="tiered",
            source_hash="a" * 64,
            algorithm_version="cost-v1",
            state="draft",
        )
        session.add(version)
        await session.flush()
        session.add_all(
            (
                RatePeriod(
                    rate_plan_version_id=version.id,
                    season="all",
                    day_type="all",
                    period_name="tier-one",
                    start_minute=0,
                    end_minute=1440,
                    price_per_kwh=Decimal("0.10"),
                    tier_start_kwh=Decimal("0"),
                    tier_end_kwh=Decimal("1"),
                ),
                RatePeriod(
                    rate_plan_version_id=version.id,
                    season="all",
                    day_type="all",
                    period_name="tier-two",
                    start_minute=0,
                    end_minute=1440,
                    price_per_kwh=Decimal("0.30"),
                    tier_start_kwh=Decimal("1"),
                ),
            )
        )
        await session.flush()
        version.state = "published"
        await session.flush()
        session.add(
            RateAssignment(
                utility_account_id=account.id,
                rate_plan_version_id=version.id,
                effective_start=version.effective_start,
                assigned_by_user_id=user_id,
            )
        )
        start = datetime(2026, 8, 13, 20, tzinfo=UTC)

        # Sequence 2 arrives and is priced before the earlier backlog record.
        await ingest_batch(
            session,
            device.id,
            ReadingBatchRequest(
                protocol_id="pm-protocol/1.0.0",
                records=[
                    _backlog_record(
                        sequence=2,
                        start=start + timedelta(minutes=1),
                        energy_mwh=600_000,
                    )
                ],
            ),
        )
        assert await calculate_pending_costs(session) == 1
        first_selected = await session.scalar(
            select(IntervalCost)
            .join(
                IntervalCostSelection,
                IntervalCostSelection.interval_cost_id == IntervalCost.id,
            )
            .join(
                NormalizedInterval,
                NormalizedInterval.id == IntervalCost.normalized_interval_id,
            )
            .where(NormalizedInterval.device_id == device.id)
        )
        assert first_selected is not None
        assert first_selected.energy_cost_microdollars == 60_000

        # The missing earlier record invalidates only mutable selections. The
        # worker rebuilds both selections in chronological tier order.
        await ingest_batch(
            session,
            device.id,
            ReadingBatchRequest(
                protocol_id="pm-protocol/1.0.0",
                records=[_backlog_record(sequence=1, start=start, energy_mwh=600_000)],
            ),
        )
        assert await session.scalar(select(func.count(IntervalCostSelection.interval_cost_id))) == 0
        assert await calculate_pending_costs(session) == 2
        await session.commit()

        selected_rows = (
            await session.execute(
                select(NormalizedInterval.start_utc, IntervalCost.energy_cost_microdollars)
                .join(
                    IntervalCostSelection,
                    IntervalCostSelection.interval_cost_id == IntervalCost.id,
                )
                .join(
                    NormalizedInterval,
                    NormalizedInterval.id == IntervalCost.normalized_interval_id,
                )
                .order_by(NormalizedInterval.start_utc)
            )
        ).all()
        assert [(timestamp.replace(tzinfo=UTC), cost) for timestamp, cost in selected_rows] == [
            (start, 60_000),
            (start + timedelta(minutes=1), 100_000),
        ]
        assert await session.scalar(select(func.count(IntervalCost.id))) == 3
        assert await session.scalar(select(func.count(RawReading.id))) == 2
        assert await session.scalar(select(func.count(RatePlanVersion.id))) == 1


@pytest.mark.asyncio
async def test_verified_full_account_aggregate_applies_baseline_once(
    owner_client: AsyncClient,
) -> None:
    async with session_factory() as session:
        home = await session.scalar(select(Home))
        account = await session.scalar(select(UtilityAccount))
        user_id = await session.scalar(select(User.id))
        assert home is not None and account is not None and user_id is not None
        account.cost_scope = "full_account"
        account.baseline_allocation_kwh = Decimal("1")
        circuit = Circuit(home_id=home.id, name="Verified mains", aggregate_mode="verified_sum")
        plan = RatePlan(name="Whole account", utility_name="SCE", rate_class="test")
        session.add_all((circuit, plan))
        await session.flush()
        devices = [
            Device(
                home_id=home.id,
                circuit_id=circuit.id,
                friendly_name=f"Main {index}",
                pzem_variant="pzem004t-v4-classic-candidate",
                ct_rating_a=Decimal("100"),
                measurement_scope="full_account",
            )
            for index in (1, 2)
        ]
        session.add_all(devices)
        await session.flush()
        version = await _published_rate(
            session,
            plan=plan,
            version_number=1,
            effective_start=datetime(2026, 1, 1, tzinfo=UTC),
            price=Decimal("0.40"),
            baseline_credit=Decimal("0.10"),
            daily_fixed_charge=Decimal("0.79"),
        )
        session.add(
            RateAssignment(
                utility_account_id=account.id,
                rate_plan_version_id=version.id,
                effective_start=version.effective_start,
                assigned_by_user_id=user_id,
            )
        )
        start = datetime(2026, 8, 13, 20, tzinfo=UTC)
        for index, device in enumerate(devices, start=1):
            await _interval(
                session,
                device=device,
                start=start,
                energy_mwh=750_000,
                sequence=index,
            )
        assert await calculate_pending_costs(session) == 2
        assert await calculate_billing_estimates(session, now=start + timedelta(minutes=1)) == 1
        await session.commit()

        totals = (
            await session.execute(
                select(
                    func.sum(IntervalCost.energy_cost_microdollars),
                    func.sum(IntervalCost.credit_microdollars),
                )
            )
        ).one()
        assert totals == (600_000, 100_000)
        assert await session.scalar(select(func.count(CostRun.id))) == 2
        estimate = await session.scalar(select(BillingEstimate))
        assert estimate is not None
        assert estimate.scope_kind == "full_account"
        assert estimate.rate_plan_version_id == version.id
        assert estimate.sensor_energy_mwh == 1_500_000
        assert estimate.energy_cost_microdollars == 600_000
        assert estimate.credit_microdollars == 100_000
        assert estimate.fixed_charge_microdollars == 10_270_000
        assert estimate.total_microdollars == 10_770_000
        assert estimate.completeness < 1
        assert estimate.missing_intervals > 0


@pytest.mark.asyncio
async def test_published_rate_period_cannot_be_changed_or_appended(
    owner_client: AsyncClient,
) -> None:
    async with session_factory() as session:
        plan = RatePlan(name="Guarded", utility_name="SCE", rate_class="test")
        session.add(plan)
        await session.flush()
        version = await _published_rate(
            session,
            plan=plan,
            version_number=1,
            effective_start=datetime(2026, 1, 1, tzinfo=UTC),
            price=Decimal("0.20"),
        )
        version_id = version.id
        await session.commit()
        period = await session.scalar(
            select(RatePeriod).where(RatePeriod.rate_plan_version_id == version_id)
        )
        assert period is not None
        period.price_per_kwh = Decimal("9.99")
        with pytest.raises(ValueError, match="children of published"):
            await session.flush()
        await session.rollback()

        session.add(
            RatePeriod(
                rate_plan_version_id=version_id,
                season="all",
                day_type="all",
                period_name="injected",
                start_minute=0,
                end_minute=1440,
                price_per_kwh=Decimal("0.01"),
            )
        )
        with pytest.raises(ValueError, match="children of published"):
            await session.flush()
