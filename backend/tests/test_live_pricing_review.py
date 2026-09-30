from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from backend.app.main import session_factory
from backend.app.models import (
    NormalizedInterval,
    RateAssignment,
    RatePeriod,
    RatePlanVersion,
    StatelessTelemetrySample,
    TelemetryEnergyEvent,
    UtilityAccount,
)
from backend.app.routes import dashboard
from backend.app.services.billing_usage import account_cycle_usage
from backend.tests.test_live_pricing import CYCLE_START, NOW, _seed
from httpx import AsyncClient


@pytest.fixture(autouse=True)
def pricing_review_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard, "_dashboard_now", lambda: NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("baseline", "expected_price"),
    [(Decimal("10"), None), (Decimal("20"), Decimal("0.20")), (Decimal("1"), Decimal("0.30"))],
)
async def test_bounded_estimate_cannot_guess_baseline_credit_eligibility(
    owner_client: AsyncClient,
    baseline: Decimal,
    expected_price: Decimal | None,
) -> None:
    gap_start = NOW - timedelta(minutes=20)
    gap_end = NOW - timedelta(minutes=10)
    ids = await _seed(
        flat_price=Decimal("0.30"),
        baseline_credit_per_kwh=Decimal("0.10"),
        energy_kwh=Decimal("9"),
        interval_end=gap_start,
    )
    async with session_factory() as session:
        account = await session.get(UtilityAccount, ids["account"])
        assert account is not None
        account.cost_scope = "full_account"
        account.baseline_allocation_kwh = baseline
        sample = StatelessTelemetrySample(
            device_id=ids["meter"],
            boot_id="00000000-0000-0000-0000-000000000002",
            sample_sequence=1,
            telemetry_protocol="pm-protocol/1.0.0",
            sampled_at=gap_end,
            received_at=gap_end,
            effective_at=gap_end,
            sensor_time_trusted=True,
            uptime_ms=600_000,
            pzem_status="unavailable",
            firmware_version="synthetic-test",
            firmware_build_id="c" * 64,
            time_status="trusted",
            payload_sha256="d" * 64,
        )
        session.add(sample)
        await session.flush()
        session.add_all(
            (
                TelemetryEnergyEvent(
                    home_id=ids["home"],
                    device_id=ids["meter"],
                    sample_id=sample.id,
                    event_type="connection_gap_unresolved",
                    gap_start_utc=gap_start,
                    gap_end_utc=gap_end,
                    billing_status="unresolved",
                    evidence={"crosses_billing_cycle": False},
                ),
                NormalizedInterval(
                    device_id=ids["meter"],
                    source_kind="stateless_v2",
                    start_utc=gap_end,
                    end_utc=NOW,
                    energy_mwh=800_000,
                    completeness=Decimal("1"),
                    energy_selection="synthetic_test",
                    algorithm_version="test",
                ),
            )
        )
        await session.commit()
        usage = await account_cycle_usage(session, account, CYCLE_START, NOW)
        assert usage.resolved
        assert usage.estimated_kwh > 0
        assert usage.lower_kwh < Decimal("10") < usage.upper_kwh
        assert usage.reading_coverage > Decimal("0.99")

    response = await owner_client.get("/api/v1/home/pricing", params={"home_id": ids["home"]})
    assert response.status_code == 200, response.text
    body = response.json()
    rate = body["current_rate"]
    if expected_price is None:
        assert body["current_rate_state"] == "incomplete_usage"
        assert rate["price_per_kwh"] is None
        assert rate["estimated_cost_per_hour"] is None
    else:
        assert body["current_rate_state"] == "available"
        assert Decimal(rate["price_per_kwh"]) == expected_price
        assert Decimal(rate["estimated_cost_per_hour"]) == expected_price * 2


@pytest.mark.asyncio
async def test_mutable_daily_settings_cannot_replace_missing_immutable_account_threshold(
    owner_client: AsyncClient,
) -> None:
    ids = await _seed(tiered=True, threshold_basis="account_daily_baseline")
    async with session_factory() as session:
        account = await session.get(UtilityAccount, ids["account"])
        assert account is not None
        account.summer_baseline_kwh_per_day = Decimal("1")
        account.winter_baseline_kwh_per_day = Decimal("1")
        await session.commit()

    response = await owner_client.get("/api/v1/home/pricing", params={"home_id": ids["home"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_rate_state"] == "unavailable"
    rate = body["current_rate"]
    assert rate["price_per_kwh"] is None
    assert rate["current_tier"] is None
    assert rate["estimated_cost_per_hour"] is None
    assert Decimal(rate["cycle_usage_kwh"]) == Decimal("10.5")
    assert "rate_schedule_unresolved" in {reason["code"] for reason in rate["availability_reasons"]}


@pytest.mark.asyncio
async def test_known_season_change_outside_weekly_lookahead_precedes_later_cycle_end(
    owner_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activation = NOW + timedelta(minutes=5)
    ids = await _seed(effective_end=activation)
    async with session_factory() as session:
        account = await session.get(UtilityAccount, ids["account"])
        assert account is not None
        account.billing_day = 15
        version = RatePlanVersion(
            rate_plan_id=ids["plan"],
            version=2,
            effective_start=activation,
            timezone="America/Los_Angeles",
            pricing_model="seasonal_time_of_use",
            source_hash="f" * 64,
            algorithm_version="cost-v1",
            state="draft",
        )
        session.add(version)
        await session.flush()
        for season, amount in [("summer", "0.20"), ("winter", "0.30")]:
            session.add(
                RatePeriod(
                    rate_plan_version_id=version.id,
                    season=season,
                    day_type="all",
                    period_name=f"{season.title()} energy",
                    start_minute=0,
                    end_minute=1440,
                    price_per_kwh=Decimal(amount),
                )
            )
        await session.flush()
        version.state = "published"
        session.add(
            RateAssignment(
                utility_account_id=ids["account"],
                rate_plan_version_id=version.id,
                effective_start=activation,
                assigned_by_user_id=ids["user"],
            )
        )
        await session.commit()
    monkeypatch.setattr(dashboard, "_dashboard_now", lambda: datetime(2026, 9, 20, 7, tzinfo=UTC))

    response = await owner_client.get("/api/v1/home/pricing", params={"home_id": ids["home"]})
    assert response.status_code == 200, response.text
    rate = response.json()["current_rate"]
    assert Decimal(rate["price_per_kwh"]) == Decimal("0.20")
    assert rate["next_change_at"] == "2026-10-01T07:00:00Z"
    assert rate["next_period"] == "Winter energy"
    assert Decimal(rate["next_price_per_kwh"]) == Decimal("0.30")
