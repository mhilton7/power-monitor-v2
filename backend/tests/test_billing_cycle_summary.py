from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal

import pytest
from backend.app.main import session_factory
from backend.app.models import (
    Circuit,
    Device,
    Home,
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
from backend.app.routes import billing as billing_route
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _FixedDateTime(datetime):
    current = datetime(2026, 8, 17, 7, 2, tzinfo=UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _FixedDateTime:
        return cls.fromtimestamp(cls.current.timestamp(), tz=tz or cls.current.tzinfo)


async def _saved_interval(
    *,
    session: AsyncSession,
    device: Device,
    start: datetime,
    sequence: int,
) -> None:
    raw = RawReading(
        device_id=device.id,
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
        active_power_mw=60_000,
        frequency_mhz=60_000,
        power_factor_milli=1_000,
        pzem_energy_wh=sequence,
        interval_energy_mwh=1_000,
        energy_selection="pzem_register_delta",
        pzem_status="ok",
        time_trusted=True,
        flags=[],
        record_crc32=sequence,
        payload_sha256=f"{sequence:x}" * 64,
    )
    session.add(raw)
    await session.flush()
    session.add(
        NormalizedInterval(
            device_id=device.id,
            raw_reading_id=raw.id,
            start_utc=start,
            end_utc=start + timedelta(minutes=1),
            energy_mwh=1_000,
            average_power_mw=60_000,
            completeness=Decimal("1"),
            energy_selection="pzem_register_delta",
            algorithm_version="normalize-v1",
            source_authenticated=True,
        )
    )


@pytest.mark.asyncio
async def test_billing_cycle_tier_is_unconfirmed_until_branch_coverage_is_complete(
    owner_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime(2026, 8, 17, 7, 0, tzinfo=UTC)
    async with session_factory() as session:
        home = await session.scalar(select(Home))
        account = await session.scalar(select(UtilityAccount))
        user_id = await session.scalar(select(User.id))
        assert home is not None and account is not None and user_id is not None
        account.billing_day = 17
        account.cost_scope = "full_account"
        account.baseline_region = "16"
        account.summer_baseline_kwh_per_day = Decimal("19.3")
        branch = Circuit(
            home_id=home.id,
            name="Main service",
            purpose="whole_home_total",
            is_home_total=True,
            is_billing_source=True,
            aggregate_mode="verified_sum",
            non_overlapping_confirmed=True,
        )
        plan = RatePlan(name="DOMESTIC", utility_name="SCE", rate_class="residential_tiered")
        session.add_all((branch, plan))
        await session.flush()
        devices = [
            Device(
                home_id=home.id,
                circuit_id=branch.id,
                friendly_name=f"Main {index}",
                pzem_variant="pzem004t-v4-classic-candidate",
                ct_rating_a=Decimal("100"),
                measurement_scope="full_account",
            )
            for index in (1, 2)
        ]
        session.add_all(devices)
        version = RatePlanVersion(
            rate_plan_id=plan.id,
            version=1,
            effective_start=datetime(2026, 1, 1, tzinfo=UTC),
            timezone="America/Los_Angeles",
            pricing_model="seasonal_tiered",
            tier_threshold_kwh_per_day=Decimal("19.3"),
            tier_threshold_season="summer",
            tier_threshold_source_kwh=Decimal("579.0"),
            daily_fixed_charge=Decimal("0.769"),
            source_hash="b" * 64,
            state="draft",
        )
        session.add(version)
        await session.flush()
        session.add_all(
            (
                RatePeriod(
                    rate_plan_version_id=version.id,
                    season="summer",
                    day_type="all",
                    period_name="tier_1",
                    start_minute=0,
                    end_minute=1440,
                    price_per_kwh=Decimal("0.30863"),
                    tier_start_kwh=Decimal("0"),
                    tier_end_kwh=Decimal("579.0"),
                ),
                RatePeriod(
                    rate_plan_version_id=version.id,
                    season="summer",
                    day_type="all",
                    period_name="tier_2",
                    start_minute=0,
                    end_minute=1440,
                    price_per_kwh=Decimal("0.40962"),
                    tier_start_kwh=Decimal("579.0"),
                ),
            )
        )
        await session.flush()
        version.state = "published"
        session.add(
            RateAssignment(
                utility_account_id=account.id,
                rate_plan_version_id=version.id,
                effective_start=version.effective_start,
                assigned_by_user_id=user_id,
            )
        )
        for sequence, device in enumerate(devices, start=1):
            await _saved_interval(
                session=session,
                device=device,
                start=start,
                sequence=sequence,
            )
        await session.commit()
        home_id = home.id
        branch_id = branch.id

    monkeypatch.setattr(billing_route, "datetime", _FixedDateTime)
    incomplete = await owner_client.get("/api/v1/billing", params={"home_id": home_id})
    assert incomplete.status_code == 200, incomplete.text
    account_body = incomplete.json()["accounts"][0]
    assert account_body["home_total_branch"]["id"] == branch_id
    rate_body = account_body["current_rate_plan"]
    assert rate_body["name"] == "DOMESTIC"
    assert rate_body["utility_name"] == "SCE"
    assert Decimal(str(rate_body["tier_1_price_per_kwh"])) == Decimal("0.30863")
    assert Decimal(str(rate_body["tier_2_price_per_kwh"])) == Decimal("0.40962")
    assert Decimal(str(rate_body["daily_service_charge"])) == Decimal("0.769")
    assert Decimal(str(rate_body["daily_baseline_allowance_kwh"])) == Decimal("19.3")
    assert rate_body["daily_baseline_source"] == "settings_seasonal_baseline"
    assert rate_body["currently_used"] is True
    cycle = account_body["current_billing_cycle"]
    assert Decimal(str(cycle["reading_coverage"])) == Decimal("0.5")
    assert cycle["tier_state"] == "not_confirmed"
    assert cycle["tier_confirmation_rule"] == (
        "cycle_total_including_recovered_and_bounded_estimated_energy"
    )
    assert cycle["tier_1_remaining_kwh"] is None
    assert cycle["amount_above_tier_1_kwh"] is None
    assert cycle["calculation_state"] == "unavailable"
    assert {item["code"] for item in cycle["availability_reasons"]} == {"unknown_gap_energy"}

    async with session_factory() as session:
        stored_devices = (
            await session.scalars(select(Device).where(Device.circuit_id == branch_id))
        ).all()
        for index, device in enumerate(stored_devices, start=1):
            sample = StatelessTelemetrySample(
                device_id=device.id,
                boot_id=f"00000000-0000-0000-0000-00000000000{index}",
                sample_sequence=2,
                telemetry_protocol="pm-protocol/1.0.0",
                sampled_at=start + timedelta(minutes=2),
                received_at=start + timedelta(minutes=2),
                effective_at=start + timedelta(minutes=2),
                sensor_time_trusted=True,
                uptime_ms=120_000,
                pzem_energy_wh=2,
                pzem_status="ok",
                firmware_version="0.1.0-rc.24",
                firmware_build_id=f"{index}" * 64,
                time_status="trusted",
                payload_sha256=f"{index}" * 64,
            )
            session.add(sample)
            await session.flush()
            session.add(
                TelemetryEnergyEvent(
                    home_id=device.home_id,
                    device_id=device.id,
                    sample_id=sample.id,
                    event_type="connection_gap_recovered",
                    gap_start_utc=start + timedelta(minutes=1),
                    gap_end_utc=start + timedelta(minutes=2),
                    prior_energy_wh=1,
                    current_energy_wh=2,
                    recovered_energy_mwh=1_000,
                    billing_status="included",
                    evidence={
                        "power_curve_fabricated": False,
                        "crosses_billing_cycle": False,
                    },
                )
            )
        await session.commit()

    recovered = await owner_client.get("/api/v1/billing", params={"home_id": home_id})
    assert recovered.status_code == 200, recovered.text
    recovered_cycle = recovered.json()["accounts"][0]["current_billing_cycle"]
    assert Decimal(str(recovered_cycle["reading_coverage"])) == Decimal("0.5")
    assert Decimal(str(recovered_cycle["measured_energy_kwh"])) == Decimal("0.002")
    assert Decimal(str(recovered_cycle["recovered_gap_energy_kwh"])) == Decimal("0.002")
    assert Decimal(str(recovered_cycle["estimated_missing_energy_kwh"])) == Decimal("0")
    assert Decimal(str(recovered_cycle["current_usage_kwh"])) == Decimal("0.004")
    assert Decimal(str(recovered_cycle["saved_usage_kwh"])) == Decimal("0.004")
    assert recovered_cycle["energy_quality"]["raw_history_modified"] is False
    assert recovered_cycle["tier_state"] == "tier_1"
    assert recovered_cycle["calculation_state"] == "exact"
    assert recovered_cycle["cost_to_date"] is not None
    assert recovered_cycle["confidence"] == "high"
    assert {item["code"] for item in recovered_cycle["availability_reasons"]} == {
        "cumulative_energy_recovered"
    }

    _FixedDateTime.current = datetime(2026, 8, 17, 7, 1, tzinfo=UTC)
    complete = await owner_client.get("/api/v1/billing", params={"home_id": home_id})
    assert complete.status_code == 200, complete.text
    complete_cycle = complete.json()["accounts"][0]["current_billing_cycle"]
    assert Decimal(str(complete_cycle["reading_coverage"])) == Decimal("1")
    assert complete_cycle["tier_state"] == "tier_1"
    assert complete_cycle["tier_1_remaining_kwh"] is not None
    breakdown = complete_cycle["tier_breakdown"]
    assert Decimal(str(breakdown["tier_1"]["usage_kwh"])) == Decimal("0.002")
    assert Decimal(str(breakdown["tier_2"]["usage_kwh"])) == Decimal("0")
    assert Decimal(str(breakdown["tier_2"]["cost"])) == Decimal("0")
    assert Decimal(str(breakdown["service_charge_to_date"])) == Decimal("0")
    assert complete_cycle["projection"]["status"] == "insufficient_data"
    assert complete_cycle["projection"]["confidence"] is None
