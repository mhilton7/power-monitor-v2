from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from backend.app.main import engine, session_factory
from backend.app.models import (
    BillingCycleAdjustment,
    BillingEstimate,
    BillingEstimateSelection,
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
    StatelessTelemetrySample,
    TelemetryEnergyEvent,
    User,
    UtilityAccount,
)
from backend.app.schemas.device import DurableReading, ReadingBatchRequest
from backend.app.services.ingestion import ingest_batch
from backend.app.services.rate_workflow import (
    replace_rate_assignment,
    replace_utility_account_tier_threshold,
    resolve_assigned_utility_account_cycle_tier_threshold,
)
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
    season_definitions: list[dict[str, object]] | None = None,
    tier_two_price: Decimal | None = None,
) -> RatePlanVersion:
    version = RatePlanVersion(
        rate_plan_id=plan.id,
        version=version_number,
        effective_start=effective_start,
        timezone="America/Los_Angeles",
        pricing_model="tiered" if tier_two_price is not None else "time_of_use",
        season_definitions=season_definitions or [],
        baseline_credit_per_kwh=baseline_credit,
        daily_fixed_charge=daily_fixed_charge,
        source_hash=f"{version_number:x}" * 64,
        algorithm_version="cost-v1",
        state="draft",
    )
    session.add(version)
    await session.flush()
    periods = [
        RatePeriod(
            rate_plan_version_id=version.id,
            season="all",
            day_type="all",
            period_name="tier-one" if tier_two_price is not None else "flat",
            start_minute=0,
            end_minute=1440,
            price_per_kwh=price,
            tier_start_kwh=Decimal("0"),
            tier_end_kwh=Decimal("1") if tier_two_price is not None else None,
            threshold_basis="account_daily_baseline" if tier_two_price is not None else None,
        )
    ]
    if tier_two_price is not None:
        periods.append(
            RatePeriod(
                rate_plan_version_id=version.id,
                season="all",
                day_type="all",
                period_name="tier-two",
                start_minute=0,
                end_minute=1440,
                price_per_kwh=tier_two_price,
                tier_start_kwh=Decimal("1"),
                threshold_basis="account_daily_baseline",
            )
        )
    session.add_all(periods)
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
async def test_recovered_gap_energy_advances_next_interval_into_tier_two(
    owner_client: AsyncClient,
) -> None:
    async with session_factory() as session:
        home = await session.scalar(select(Home))
        account = await session.scalar(select(UtilityAccount))
        user_id = await session.scalar(select(User.id))
        assert home is not None and account is not None and user_id is not None
        account.billing_day = 1
        device = Device(
            home_id=home.id,
            friendly_name="Recovered-gap meter",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        plan = RatePlan(name="Recovered tier", utility_name="SCE", rate_class="test")
        session.add_all((device, plan))
        await session.flush()
        version = RatePlanVersion(
            rate_plan_id=plan.id,
            version=1,
            effective_start=datetime(2026, 1, 1, tzinfo=UTC),
            timezone="America/Los_Angeles",
            pricing_model="tiered",
            source_hash="b" * 64,
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
        await replace_rate_assignment(
            session,
            account=account,
            version=version,
            actor_user_id=user_id,
        )
        interval_start = datetime(2026, 8, 13, 20, tzinfo=UTC)
        sample = StatelessTelemetrySample(
            device_id=device.id,
            boot_id="11111111-1111-1111-1111-111111111111",
            sample_sequence=1,
            telemetry_protocol="pm-stateless-telemetry/2.0.0",
            sampled_at=interval_start - timedelta(minutes=1),
            received_at=interval_start - timedelta(minutes=1),
            effective_at=interval_start - timedelta(minutes=1),
            sensor_time_trusted=True,
            uptime_ms=60_000,
            pzem_status="ok",
            firmware_version="0.1.0-rc.22",
            firmware_build_id="1" * 64,
            time_status="trusted",
            payload_sha256="c" * 64,
        )
        session.add(sample)
        await session.flush()
        session.add(
            TelemetryEnergyEvent(
                home_id=home.id,
                device_id=device.id,
                sample_id=sample.id,
                event_type="connection_gap_recovered",
                gap_start_utc=interval_start - timedelta(hours=1),
                gap_end_utc=interval_start - timedelta(minutes=1),
                prior_energy_wh=100,
                current_energy_wh=1_200,
                recovered_energy_mwh=1_100_000,
                billing_status="included",
                evidence={"source": "authenticated_pzem_counter_delta"},
            )
        )
        interval = await _interval(
            session,
            device=device,
            start=interval_start,
            energy_mwh=100_000,
            sequence=2,
        )

        assert await calculate_pending_costs(session) == 1
        selected = await session.scalar(
            select(IntervalCost)
            .join(
                IntervalCostSelection,
                IntervalCostSelection.interval_cost_id == IntervalCost.id,
            )
            .where(IntervalCost.normalized_interval_id == interval.id)
        )
        assert selected is not None
        assert selected.period_name == "tier-two"
        assert selected.energy_cost_microdollars == 30_000


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
        session.add(
            BillingCycleAdjustment(
                utility_account_id=account.id,
                cycle_start_utc=datetime(2026, 8, 1, 7, tzinfo=UTC),
                energy_mwh=100_000,
                reason="verified_cycle_to_date_seed",
                evidence={"source": "verified test seed"},
                created_by_user_id=user_id,
            )
        )
        await session.flush()
        assert await calculate_billing_estimates(session, now=start + timedelta(minutes=1)) == 0
        assert await session.scalar(select(func.count(BillingEstimateSelection.scope_id))) == 0


@pytest.mark.asyncio
async def test_mid_cycle_rate_change_prices_each_interval_with_immutable_version(
    owner_client: AsyncClient,
) -> None:
    async with session_factory() as session:
        account = await session.scalar(select(UtilityAccount))
        user_id = await session.scalar(select(User.id))
        assert account is not None and user_id is not None
        account.billing_day = 1
        plan = RatePlan(name="Mid-cycle plan", utility_name="SCE", rate_class="test")
        session.add(plan)
        await session.flush()
        old_version = await _published_rate(
            session,
            plan=plan,
            version_number=1,
            effective_start=datetime(2026, 1, 1, tzinfo=UTC),
            price=Decimal("0.10"),
            daily_fixed_charge=Decimal("0.50"),
            season_definitions=[
                {
                    "season_name": "summer",
                    "start_month": 1,
                    "start_day": 1,
                    "end_month": 12,
                    "end_day": 31,
                }
            ],
        )
        await replace_rate_assignment(
            session,
            account=account,
            version=old_version,
            actor_user_id=user_id,
        )
        transition = datetime(2026, 8, 15, 7, tzinfo=UTC)
        new_version = await _published_rate(
            session,
            plan=plan,
            version_number=2,
            effective_start=transition,
            price=Decimal("0.20"),
            daily_fixed_charge=Decimal("1.00"),
            season_definitions=[
                {
                    "season_name": "winter",
                    "start_month": 1,
                    "start_day": 1,
                    "end_month": 12,
                    "end_day": 31,
                }
            ],
        )
        await replace_rate_assignment(
            session,
            account=account,
            version=new_version,
            actor_user_id=user_id,
        )
        for season, daily in (("summer", Decimal("10")), ("winter", Decimal("5"))):
            await replace_utility_account_tier_threshold(
                session,
                account=account,
                rate_plan_id=plan.id,
                season=season,
                kwh_per_day=daily,
                source_allowance_kwh=daily * 30,
                source_billing_days=30,
                tier1_boundary_inclusive=True,
                source_label=f"{season} account allowance",
                source_kind="candidate_review",
                source_artifact_sha256="a" * 64,
                effective_start=datetime(2026, 1, 1, tzinfo=UTC),
                effective_end=None,
                actor_user_id=user_id,
            )
        threshold = await resolve_assigned_utility_account_cycle_tier_threshold(
            session,
            utility_account_id=account.id,
            timezone=account.timezone,
            cycle_start=datetime(2026, 8, 1, 7, tzinfo=UTC),
            cycle_end=datetime(2026, 9, 1, 7, tzinfo=UTC),
        )
        assert threshold is not None
        assert threshold.total_kwh == Decimal("225")
        device = Device(
            home_id=account.home_id,
            friendly_name="Mid-cycle pricing fixture",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
            measurement_scope="energy_only",
        )
        session.add(device)
        await session.flush()
        old_interval = await _interval(
            session,
            device=device,
            start=datetime(2026, 8, 10, 12, tzinfo=UTC),
            energy_mwh=1_000_000,
            sequence=1,
        )
        new_interval = await _interval(
            session,
            device=device,
            start=datetime(2026, 8, 20, 12, tzinfo=UTC),
            energy_mwh=1_000_000,
            sequence=2,
        )
        assert await calculate_pending_costs(session) == 2
        selected = (
            await session.execute(
                select(
                    IntervalCost.normalized_interval_id,
                    IntervalCost.rate_plan_version_id,
                    IntervalCost.energy_cost_microdollars,
                )
                .join(
                    IntervalCostSelection,
                    IntervalCostSelection.interval_cost_id == IntervalCost.id,
                )
                .where(IntervalCost.normalized_interval_id.in_((old_interval.id, new_interval.id)))
                .order_by(IntervalCost.energy_cost_microdollars)
            )
        ).all()
        assert [tuple(row) for row in selected] == [
            (old_interval.id, old_version.id, 100_000),
            (new_interval.id, new_version.id, 200_000),
        ]
        assert (
            await calculate_billing_estimates(
                session,
                now=datetime(2026, 8, 20, 12, tzinfo=UTC),
            )
            == 0
        )
        assert await session.scalar(select(func.count(BillingEstimateSelection.scope_id))) == 0


@pytest.mark.asyncio
async def test_midday_account_threshold_transition_skips_only_unresolved_account(
    owner_client: AsyncClient,
) -> None:
    async with session_factory() as session:
        first_home = await session.scalar(select(Home))
        first_account = await session.scalar(select(UtilityAccount))
        user_id = await session.scalar(select(User.id))
        assert first_home is not None and first_account is not None and user_id is not None
        first_account.billing_day = 1
        second_home = Home(name="Independent flat-rate home", timezone="America/Los_Angeles")
        session.add(second_home)
        await session.flush()
        second_account = UtilityAccount(
            home_id=second_home.id,
            timezone="America/Los_Angeles",
            billing_day=1,
            cost_scope="energy_only",
        )
        tier_plan = RatePlan(name="Midday symbolic tier", utility_name="SCE", rate_class="test")
        flat_plan = RatePlan(name="Independent flat", utility_name="SCE", rate_class="test")
        session.add_all((second_account, tier_plan, flat_plan))
        await session.flush()
        old_tier = await _published_rate(
            session,
            plan=tier_plan,
            version_number=1,
            effective_start=datetime(2026, 1, 1, tzinfo=UTC),
            price=Decimal("0.10"),
            tier_two_price=Decimal("0.30"),
        )
        await replace_rate_assignment(
            session,
            account=first_account,
            version=old_tier,
            actor_user_id=user_id,
        )
        new_tier = await _published_rate(
            session,
            plan=tier_plan,
            version_number=2,
            effective_start=datetime(2026, 8, 15, 19, tzinfo=UTC),
            price=Decimal("0.11"),
            tier_two_price=Decimal("0.31"),
        )
        await replace_rate_assignment(
            session,
            account=first_account,
            version=new_tier,
            actor_user_id=user_id,
        )
        await replace_utility_account_tier_threshold(
            session,
            account=first_account,
            rate_plan_id=tier_plan.id,
            season="all",
            kwh_per_day=Decimal("10"),
            source_allowance_kwh=Decimal("300"),
            source_billing_days=30,
            tier1_boundary_inclusive=True,
            source_label="verified all-season allowance",
            source_kind="candidate_review",
            source_artifact_sha256="d" * 64,
            effective_start=datetime(2026, 1, 1, tzinfo=UTC),
            effective_end=None,
            actor_user_id=user_id,
        )
        flat_version = await _published_rate(
            session,
            plan=flat_plan,
            version_number=1,
            effective_start=datetime(2026, 1, 1, tzinfo=UTC),
            price=Decimal("0.25"),
        )
        await replace_rate_assignment(
            session,
            account=second_account,
            version=flat_version,
            actor_user_id=user_id,
        )
        tier_device = Device(
            home_id=first_home.id,
            friendly_name="Unresolved midday tier",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        flat_device = Device(
            home_id=second_home.id,
            friendly_name="Independent flat meter",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        session.add_all((tier_device, flat_device))
        await session.flush()
        instant = datetime(2026, 8, 20, 12, tzinfo=UTC)
        tier_interval = await _interval(
            session,
            device=tier_device,
            start=instant,
            energy_mwh=100_000,
            sequence=1,
        )
        flat_interval = await _interval(
            session,
            device=flat_device,
            start=instant,
            energy_mwh=100_000,
            sequence=1,
        )

        assert await calculate_pending_costs(session) == 1
        selected_ids = set(
            (
                await session.scalars(
                    select(IntervalCostSelection.normalized_interval_id).where(
                        IntervalCostSelection.normalized_interval_id.in_(
                            (tier_interval.id, flat_interval.id)
                        )
                    )
                )
            ).all()
        )
        assert selected_ids == {flat_interval.id}
        flat_cost = await session.scalar(
            select(IntervalCost)
            .join(
                IntervalCostSelection,
                IntervalCostSelection.interval_cost_id == IntervalCost.id,
            )
            .where(IntervalCost.normalized_interval_id == flat_interval.id)
        )
        assert flat_cost is not None and flat_cost.energy_cost_microdollars == 25_000


@pytest.mark.asyncio
@pytest.mark.skipif(
    engine.dialect.name == "postgresql",
    reason="PostgreSQL integrity guards prevent constructing overlapping assignments",
)
async def test_overlapping_rate_assignments_leave_interval_unpriced(
    owner_client: AsyncClient,
) -> None:
    async with session_factory() as session:
        home = await session.scalar(select(Home))
        account = await session.scalar(select(UtilityAccount))
        user_id = await session.scalar(select(User.id))
        assert home is not None and account is not None and user_id is not None
        plan = RatePlan(name="Ambiguous assignment", utility_name="SCE", rate_class="test")
        device = Device(
            home_id=home.id,
            friendly_name="Ambiguous rate meter",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        session.add_all((plan, device))
        await session.flush()
        first = await _published_rate(
            session,
            plan=plan,
            version_number=1,
            effective_start=datetime(2026, 1, 1, tzinfo=UTC),
            price=Decimal("0.10"),
        )
        second = await _published_rate(
            session,
            plan=plan,
            version_number=2,
            effective_start=datetime(2026, 2, 1, tzinfo=UTC),
            price=Decimal("0.30"),
        )
        session.add_all(
            (
                RateAssignment(
                    utility_account_id=account.id,
                    rate_plan_version_id=first.id,
                    effective_start=first.effective_start,
                    assigned_by_user_id=user_id,
                ),
                RateAssignment(
                    utility_account_id=account.id,
                    rate_plan_version_id=second.id,
                    effective_start=second.effective_start,
                    assigned_by_user_id=user_id,
                ),
            )
        )
        await session.flush()
        interval = await _interval(
            session,
            device=device,
            start=datetime(2026, 8, 20, 12, tzinfo=UTC),
            energy_mwh=1_000_000,
        )

        assert await calculate_pending_costs(session) == 0
        assert (
            await session.scalar(
                select(IntervalCostSelection.normalized_interval_id).where(
                    IntervalCostSelection.normalized_interval_id == interval.id
                )
            )
            is None
        )


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
