from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from backend.app.main import session_factory
from backend.app.models import (
    BillingCycleAdjustment,
    Circuit,
    Device,
    DeviceHeartbeat,
    NormalizedInterval,
    RateAssignment,
    RatePeriod,
    RatePlan,
    RatePlanVersion,
    User,
    UtilityAccount,
    role_permissions,
)
from backend.app.routes import dashboard
from backend.app.services.cost_engine import (
    CostContext,
    EventCalendar,
    PricePeriod,
    RateVersion,
    price_sensor_interval,
    resolve_price_period,
)
from backend.app.services.live_pricing import _schedule_instants
from httpx import AsyncClient
from sqlalchemy import delete, select

NOW = datetime(2026, 9, 2, 7, tzinfo=UTC)
CYCLE_START = datetime(2026, 9, 1, 7, tzinfo=UTC)


@pytest.fixture
def pricing_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard, "_dashboard_now", lambda: NOW)


async def _seed(
    *,
    tiered: bool = False,
    authoritative: bool = True,
    age_seconds: int = 0,
    energy_kwh: Decimal = Decimal("10.5"),
    tou: bool = False,
    flat_price: Decimal = Decimal("0.20"),
    power_w: Decimal = Decimal("2000"),
    effective_start: datetime = CYCLE_START,
    effective_end: datetime | None = None,
    coverage: Decimal = Decimal("1"),
    interval_start: datetime = CYCLE_START,
    interval_end: datetime = NOW,
    baseline_credit_per_kwh: Decimal = Decimal("0"),
    threshold_basis: str | None = None,
) -> dict[str, str]:
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "owner@example.com"))
        account = await session.scalar(select(UtilityAccount))
        assert user is not None and account is not None
        circuit = Circuit(
            home_id=account.home_id,
            name="Synthetic main service",
            purpose="whole_home_total" if authoritative else "electrical_section",
            is_home_total=authoritative,
            is_billing_source=authoritative,
            non_overlapping_confirmed=authoritative,
            aggregate_mode="verified_sum" if authoritative else "individual",
        )
        session.add(circuit)
        await session.flush()
        meter = Device(
            home_id=account.home_id,
            circuit_id=circuit.id,
            friendly_name="Synthetic service meter",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
            include_in_aggregate=True,
        )
        plan = RatePlan(name="Synthetic test tariff", utility_name="Test", rate_class="test")
        session.add_all((meter, plan))
        await session.flush()
        version = RatePlanVersion(
            rate_plan_id=plan.id,
            version=1,
            effective_start=effective_start,
            effective_end=effective_end,
            timezone="America/Los_Angeles",
            pricing_model="tiered_time_of_use"
            if tiered and tou
            else "tiered"
            if tiered
            else "time_of_use"
            if tou
            else "flat",
            source_hash="e" * 64,
            algorithm_version="cost-v1",
            baseline_credit_per_kwh=baseline_credit_per_kwh,
            state="draft",
        )
        session.add(version)
        await session.flush()
        periods = [("Tier 1", "0.20", "0", "10"), ("Tier 2", "0.30", "10", None)]
        if not tiered:
            periods = [("Flat", str(flat_price), "0", None)]
        for name, price, start, end in periods:
            for period_name, begin, finish, increment in (
                [
                    ("Off-peak", 0, 960, Decimal("0")),
                    ("Peak", 960, 1260, Decimal("0.20")),
                    ("Off-peak", 1260, 1440, Decimal("0")),
                ]
                if tou
                else [(name, 0, 1440, Decimal("0"))]
            ):
                session.add(
                    RatePeriod(
                        rate_plan_version_id=version.id,
                        season="all",
                        day_type="all",
                        period_name=period_name,
                        start_minute=begin,
                        end_minute=finish,
                        price_per_kwh=Decimal(price) + increment,
                        tier_start_kwh=Decimal(start),
                        tier_end_kwh=Decimal(end) if end is not None else None,
                        threshold_basis=threshold_basis,
                    )
                )
        await session.flush()
        version.state = "published"
        session.add_all(
            (
                RateAssignment(
                    utility_account_id=account.id,
                    rate_plan_version_id=version.id,
                    effective_start=effective_start,
                    effective_end=effective_end,
                    assigned_by_user_id=user.id,
                ),
                DeviceHeartbeat(
                    device_id=meter.id,
                    boot_id="00000000-0000-0000-0000-000000000001",
                    received_at=NOW - timedelta(seconds=age_seconds),
                    measured_at=NOW - timedelta(seconds=age_seconds),
                    active_power_w=power_w,
                    pzem_status="ok",
                    storage_status="healthy",
                    time_status="trusted",
                ),
                NormalizedInterval(
                    device_id=meter.id,
                    source_kind="stateless_v2",
                    start_utc=interval_start,
                    end_utc=interval_end,
                    energy_mwh=int(energy_kwh * Decimal(1_000_000)),
                    completeness=coverage,
                    energy_selection="synthetic_test",
                    algorithm_version="test",
                ),
            )
        )
        await session.commit()
        return {
            "home": account.home_id,
            "meter": meter.id,
            "version": version.id,
            "account": account.id,
            "circuit": circuit.id,
            "user": user.id,
            "plan": plan.id,
        }


@pytest.mark.asyncio
async def test_stale_load_never_produces_live_cost_but_flat_price_remains_available(
    owner_client: AsyncClient,
    pricing_clock: None,
) -> None:
    ids = await _seed(age_seconds=60)
    response = await owner_client.get("/api/v1/home", params={"home_id": ids["home"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(str(body["current_rate"]["price_per_kwh"])) == Decimal("0.20")
    assert body["devices"][0].get("estimated_cost_per_hour") is None


@pytest.mark.asyncio
async def test_partial_sensor_cannot_confirm_account_tier(
    owner_client: AsyncClient,
    pricing_clock: None,
) -> None:
    ids = await _seed(tiered=True, authoritative=False)
    response = await owner_client.get("/api/v1/home", params={"device_id": ids["meter"]})
    assert response.status_code == 200, response.text
    assert response.json()["current_rate"]["price_per_kwh"] is None


@pytest.mark.asyncio
async def test_selected_partial_sensor_uses_service_meter_tier_not_its_own_usage(
    owner_client: AsyncClient,
    pricing_clock: None,
) -> None:
    ids = await _seed(tiered=True)
    async with session_factory() as session:
        partial = Device(
            home_id=ids["home"],
            friendly_name="Synthetic partial circuit",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        session.add(partial)
        await session.flush()
        session.add(
            DeviceHeartbeat(
                device_id=partial.id,
                boot_id="00000000-0000-0000-0000-000000000002",
                received_at=NOW,
                measured_at=NOW,
                active_power_w=Decimal("2000"),
                pzem_status="ok",
                storage_status="healthy",
                time_status="trusted",
            )
        )
        await session.commit()
        partial_id = partial.id
    response = await owner_client.get("/api/v1/home", params={"device_id": partial_id})
    assert response.status_code == 200, response.text
    rate = response.json()["current_rate"]
    assert rate["price_per_kwh"] is not None
    assert Decimal(str(rate["price_per_kwh"])) == Decimal("0.30")
    assert Decimal(str(rate["cumulative_cycle_kwh"])) == Decimal("10.5")
    card = next(item for item in response.json()["devices"] if item["id"] == partial_id)
    assert Decimal(str(card["estimated_cost_per_hour"])) == Decimal("0.60")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rate", "power", "expected"),
    [
        ("0", "2000", "0"),
        ("0.20", "0", "0"),
        ("0.20", "2000", "0.40"),
    ],
)
async def test_live_pricing_zero_values_are_valid_and_endpoint_skips_history_summaries(
    owner_client: AsyncClient,
    pricing_clock: None,
    monkeypatch: pytest.MonkeyPatch,
    rate: str,
    power: str,
    expected: str,
) -> None:
    ids = await _seed(flat_price=Decimal(rate), power_w=Decimal(power))

    async def no_summary(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("pricing refresh must not load historical summaries")

    monkeypatch.setattr(dashboard, "_summary", no_summary)
    response = await owner_client.get("/api/v1/home/pricing", params={"home_id": ids["home"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["home_id"] == ids["home"]
    assert body["current_rate_state"] == "available"
    assert Decimal(str(body["current_rate"]["price_per_kwh"])) == Decimal(rate)
    assert Decimal(str(body["current_rate"]["estimated_cost_per_hour"])) == Decimal(expected)
    assert body["current_rate"]["load_state"] == "live"
    assert body["current_rate"]["load_fresh_until"] == "2026-09-02T07:00:30Z"
    assert body["current_rate"]["next_change_at"] is None
    assert body["next_pricing_refresh_at"] == "2026-09-03T07:00:00Z"
    assert "summaries" not in body


@pytest.mark.asyncio
async def test_tier_progress_is_usage_not_coverage_and_exact_threshold_is_marginal_tier_two(
    owner_client: AsyncClient,
    pricing_clock: None,
) -> None:
    await _seed(tiered=True, energy_kwh=Decimal("9.5"))
    response = await owner_client.get("/api/v1/home/pricing")
    rate = response.json()["current_rate"]
    assert rate["current_tier"] == 1
    assert Decimal(rate["remaining_tier_kwh"]) == Decimal("0.5")
    assert Decimal(rate["tier_progress_percent"]) == Decimal("95")
    assert Decimal(rate["reading_coverage"]) == Decimal("1")
    async with session_factory() as session:
        interval = await session.scalar(select(NormalizedInterval))
        assert interval is not None
        interval.energy_mwh = 10_000_000
        await session.commit()
    at_threshold = (await owner_client.get("/api/v1/home/pricing")).json()["current_rate"]
    assert at_threshold["current_tier"] == 2
    assert Decimal(at_threshold["price_per_kwh"]) == Decimal("0.30")
    assert Decimal(at_threshold["estimated_cost_per_hour"]) == Decimal("0.60")


def test_synthetic_tier_crossing_cost_stays_chronological_not_current_price_times_energy() -> None:
    rate = RateVersion(
        id="synthetic",
        timezone="America/Los_Angeles",
        effective_start=CYCLE_START,
        effective_end=None,
        periods=(
            PricePeriod(
                "all", "all", "Tier 1", 0, 1440, Decimal("0.20"), tier_end_kwh=Decimal("10")
            ),
            PricePeriod(
                "all", "all", "Tier 2", 0, 1440, Decimal("0.30"), tier_start_kwh=Decimal("10")
            ),
        ),
    )
    result = price_sensor_interval(
        start_utc=NOW - timedelta(minutes=1),
        end_utc=NOW,
        energy_mwh=1_000_000,
        rate=rate,
        context=CostContext(cumulative_cycle_kwh_before=Decimal("9.5")),
    )
    assert result.total_microdollars == 250_000
    assert [item.energy_mwh for item in result.slices] == [500_000, 500_000]


@pytest.mark.asyncio
async def test_clock_only_tou_transition_uses_server_account_timezone_and_preserves_offline_price(
    owner_client: AsyncClient,
    pricing_clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(tou=True)
    boundary = datetime(2026, 9, 2, 23, tzinfo=UTC)  # 16:00 in the account, not the browser.
    monkeypatch.setattr(dashboard, "_dashboard_now", lambda: boundary - timedelta(seconds=1))
    before = (await owner_client.get("/api/v1/home/pricing")).json()["current_rate"]
    assert before["tou_period"] == "Off-peak"
    assert before["next_change_at"] == "2026-09-02T23:00:00Z"
    assert Decimal(before["next_price_per_kwh"]) == Decimal("0.40")
    monkeypatch.setattr(dashboard, "_dashboard_now", lambda: boundary)
    after = (await owner_client.get("/api/v1/home/pricing")).json()["current_rate"]
    assert after["tou_period"] == "Peak"
    assert Decimal(after["price_per_kwh"]) == Decimal("0.40")
    assert after["estimated_cost_per_hour"] is None
    assert after["load_state"] == "stale"


@pytest.mark.asyncio
async def test_hybrid_reports_both_tou_and_tier_without_forecasting_consumption(
    owner_client: AsyncClient,
    pricing_clock: None,
) -> None:
    await _seed(tou=True, tiered=True)
    rate = (await owner_client.get("/api/v1/home/pricing")).json()["current_rate"]
    assert rate["tou_period"] == "Off-peak"
    assert rate["current_tier"] == 2
    assert Decimal(rate["price_per_kwh"]) == Decimal("0.30")
    assert rate["next_change_at"] == "2026-09-02T23:00:00Z"
    assert rate["next_period"] == "Peak"
    assert rate["next_price_per_kwh"] is None


@pytest.mark.asyncio
async def test_incomplete_usage_cannot_guess_tier_but_late_authenticated_interval_resolves_it(
    owner_client: AsyncClient,
    pricing_clock: None,
) -> None:
    middle = CYCLE_START + timedelta(hours=12)
    ids = await _seed(tiered=True, energy_kwh=Decimal("5"), interval_start=middle)
    before = (await owner_client.get("/api/v1/home/pricing")).json()
    assert before["current_rate_state"] == "incomplete_usage"
    assert before["current_rate"]["price_per_kwh"] is None
    assert Decimal(before["current_rate"]["reading_coverage"]) == Decimal("0.5")
    async with session_factory() as session:
        session.add(
            NormalizedInterval(
                device_id=ids["meter"],
                source_kind="stateless_v2",
                start_utc=CYCLE_START,
                end_utc=middle,
                energy_mwh=4_500_000,
                completeness=Decimal("1"),
                energy_selection="synthetic_test",
                algorithm_version="test",
            )
        )
        await session.commit()
    after = (await owner_client.get("/api/v1/home/pricing")).json()["current_rate"]
    assert Decimal(after["cycle_usage_kwh"]) == Decimal("9.5")
    assert Decimal(after["tier_progress_percent"]) == Decimal("95")
    assert Decimal(after["price_per_kwh"]) == Decimal("0.20")


@pytest.mark.asyncio
async def test_existing_verified_cycle_seed_is_applied_once_without_changing_saved_readings(
    owner_client: AsyncClient,
    pricing_clock: None,
) -> None:
    middle = CYCLE_START + timedelta(hours=12)
    ids = await _seed(tiered=True, energy_kwh=Decimal("1"), interval_start=middle)
    async with session_factory() as session:
        session.add(
            BillingCycleAdjustment(
                utility_account_id=ids["account"],
                cycle_start_utc=CYCLE_START,
                energy_mwh=9_500_000,
                reason="verified_cycle_to_date_seed",
                evidence={
                    "source": "administrator_verified_cycle_to_date",
                    "through_utc": middle.isoformat(),
                },
                created_by_user_id=ids["user"],
            )
        )
        await session.commit()
    for _ in range(2):
        rate = (await owner_client.get("/api/v1/home/pricing")).json()["current_rate"]
        assert Decimal(rate["cycle_usage_kwh"]) == Decimal("10.5")
        assert Decimal(rate["price_per_kwh"]) == Decimal("0.30")
    async with session_factory() as session:
        values = (await session.scalars(select(NormalizedInterval.energy_mwh))).all()
        assert values == [1_000_000]


@pytest.mark.asyncio
async def test_cycle_rollover_resets_tier_usage_not_rate_history(
    owner_client: AsyncClient,
    pricing_clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(tiered=True)
    monkeypatch.setattr(dashboard, "_dashboard_now", lambda: datetime(2026, 10, 1, 7, tzinfo=UTC))
    rate = (await owner_client.get("/api/v1/home/pricing")).json()["current_rate"]
    assert rate["current_tier"] == 1
    assert Decimal(rate["cycle_usage_kwh"]) == Decimal("0")
    assert Decimal(rate["price_per_kwh"]) == Decimal("0.20")
    assert rate["estimated_cost_per_hour"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("removed_permission", ["rates.view", "billing.view", "dashboard.view"])
async def test_live_pricing_permission_combination_does_not_broaden_access(
    owner_client: AsyncClient,
    pricing_clock: None,
    removed_permission: str,
) -> None:
    await _seed()
    async with session_factory() as session:
        await session.execute(
            delete(role_permissions).where(
                role_permissions.c.permission_name == removed_permission,
            )
        )
        await session.commit()
    response = await owner_client.get("/api/v1/home/pricing")
    if removed_permission == "dashboard.view":
        assert response.status_code == 403
    else:
        assert response.status_code == 200
        assert response.json()["current_rate_state"] == "permission_denied"
        assert response.json()["current_rate"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["absent", "expired", "pending"])
async def test_absent_expired_and_future_assignments_have_explicit_nonzero_states(
    owner_client: AsyncClient,
    pricing_clock: None,
    phase: str,
) -> None:
    if phase != "absent":
        await _seed(
            effective_start=NOW + timedelta(minutes=5) if phase == "pending" else CYCLE_START,
            effective_end=NOW if phase == "expired" else None,
        )
    body = (await owner_client.get("/api/v1/home/pricing")).json()
    assert body["current_rate_state"] == "unconfigured"
    assert body["current_rate"] is None
    assert body["next_pricing_refresh_at"] == (
        "2026-09-02T07:05:00Z" if phase == "pending" else None
    )


@pytest.mark.asyncio
async def test_scheduled_midcycle_assignment_changes_on_clock_without_sensor_reading(
    owner_client: AsyncClient,
    pricing_clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transition = NOW + timedelta(minutes=5)
    ids = await _seed(effective_end=transition)
    async with session_factory() as session:
        version = RatePlanVersion(
            rate_plan_id=ids["plan"],
            version=2,
            effective_start=transition,
            timezone="America/Los_Angeles",
            pricing_model="flat",
            source_hash="a" * 64,
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
                period_name="New flat",
                start_minute=0,
                end_minute=1440,
                price_per_kwh=Decimal("0.45"),
            )
        )
        await session.flush()
        version.state = "published"
        session.add(
            RateAssignment(
                utility_account_id=ids["account"],
                rate_plan_version_id=version.id,
                effective_start=transition,
                assigned_by_user_id=ids["user"],
            )
        )
        await session.commit()
        new_version_id = version.id
    before = (await owner_client.get("/api/v1/home/pricing")).json()["current_rate"]
    assert before["next_change_at"] == "2026-09-02T07:05:00Z"
    assert Decimal(before["next_price_per_kwh"]) == Decimal("0.45")
    monkeypatch.setattr(dashboard, "_dashboard_now", lambda: transition)
    after = (await owner_client.get("/api/v1/home/pricing")).json()["current_rate"]
    assert after["version_id"] == new_version_id
    assert Decimal(after["price_per_kwh"]) == Decimal("0.45")
    assert after["estimated_cost_per_hour"] is None


@pytest.mark.parametrize(
    ("instant", "transition"),
    [
        (datetime(2026, 3, 8, 9, 59, tzinfo=UTC), datetime(2026, 3, 8, 10, tzinfo=UTC)),
        (datetime(2026, 11, 1, 8, 59, tzinfo=UTC), datetime(2026, 11, 1, 9, tzinfo=UTC)),
    ],
)
def test_schedule_candidates_include_real_dst_transition_and_both_fold_offsets(
    instant: datetime,
    transition: datetime,
) -> None:
    rate = RateVersion(
        id="synthetic",
        timezone="America/Los_Angeles",
        effective_start=CYCLE_START,
        effective_end=None,
        periods=(
            PricePeriod(
                "all",
                "all",
                "test",
                150,
                1440,
                Decimal("0.20"),
            ),
        ),
    )
    assert transition in _schedule_instants(rate, instant)
    assert (
        transition.astimezone(ZoneInfo(rate.timezone)).utcoffset()
        != (transition - timedelta(minutes=1)).astimezone(ZoneInfo(rate.timezone)).utcoffset()
    )


@pytest.mark.asyncio
async def test_real_serialized_pricing_response_matches_shared_frontend_fixture(
    owner_client: AsyncClient,
    pricing_clock: None,
) -> None:
    ids = await _seed(tiered=True)
    response = await owner_client.get("/api/v1/home/pricing")
    assert response.status_code == 200, response.text
    body = response.json()
    replacements = {
        ids["home"]: "00000000-0000-0000-0000-000000000101",
        ids["account"]: "00000000-0000-0000-0000-000000000102",
        ids["meter"]: "00000000-0000-0000-0000-000000000103",
        ids["circuit"]: "00000000-0000-0000-0000-000000000104",
        ids["version"]: "00000000-0000-0000-0000-000000000105",
        body["current_rate"]["assignment_id"]: "00000000-0000-0000-0000-000000000106",
    }
    sanitized = response.text
    for actual, synthetic in replacements.items():
        sanitized = sanitized.replace(actual, synthetic)
    fixture = Path("frontend/tests/fixtures/live-pricing-api.json")
    assert json.loads(sanitized) == json.loads(fixture.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_adjacent_same_price_tou_segments_are_refresh_boundaries_not_price_changes(
    owner_client: AsyncClient,
    pricing_clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(tou=True)
    monkeypatch.setattr(dashboard, "_dashboard_now", lambda: datetime(2026, 9, 3, 5, tzinfo=UTC))
    body = (await owner_client.get("/api/v1/home/pricing")).json()
    assert body["next_pricing_refresh_at"] == "2026-09-03T07:00:00Z"
    assert body["current_rate"]["next_change_at"] == "2026-09-03T23:00:00Z"
    assert body["current_rate"]["next_period"] == "Peak"


@pytest.mark.asyncio
async def test_incomplete_hybrid_keeps_known_tou_period_but_not_unconfirmed_tier(
    owner_client: AsyncClient,
    pricing_clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(tiered=True, tou=True, coverage=Decimal("0.5"))
    rate = (await owner_client.get("/api/v1/home/pricing")).json()["current_rate"]
    assert rate["tou_period"] == "Off-peak"
    assert rate["price_per_kwh"] is None
    assert rate["current_tier"] is None
    monkeypatch.setattr(dashboard, "_dashboard_now", lambda: datetime(2026, 9, 3, 5, tzinfo=UTC))
    late = (await owner_client.get("/api/v1/home/pricing")).json()["current_rate"]
    assert late["tou_period"] == "Off-peak"
    assert late["next_change_at"] == "2026-09-03T23:00:00Z"


@pytest.mark.asyncio
async def test_revoked_member_makes_selected_aggregate_live_cost_unavailable(
    owner_client: AsyncClient,
    pricing_clock: None,
) -> None:
    ids = await _seed()
    async with session_factory() as session:
        session.add(
            Device(
                home_id=ids["home"],
                circuit_id=ids["circuit"],
                friendly_name="Revoked test member",
                pzem_variant="pzem004t-v4-classic-candidate",
                ct_rating_a=Decimal("100"),
                include_in_aggregate=True,
                revoked_at=NOW - timedelta(hours=1),
            )
        )
        await session.commit()
    body = (await owner_client.get("/api/v1/home")).json()
    assert body["aggregate_measurement"]["partial"] is True
    assert body["current_rate"]["estimated_cost_per_hour"] is None
    assert body["current_rate"]["load_state"] == "unavailable"


@pytest.mark.asyncio
async def test_no_intervals_is_not_measured_zero_usage(
    owner_client: AsyncClient,
    pricing_clock: None,
) -> None:
    await _seed(tiered=True)
    async with session_factory() as session:
        await session.execute(delete(NormalizedInterval))
        await session.commit()
    rate = (await owner_client.get("/api/v1/home/pricing")).json()["current_rate"]
    assert rate["cycle_usage_kwh"] is None
    assert rate["measured_cycle_kwh"] is None
    assert rate["price_per_kwh"] is None


def test_far_known_event_date_includes_intraday_tariff_boundary() -> None:
    rate = RateVersion(
        id="synthetic",
        timezone="America/Los_Angeles",
        effective_start=CYCLE_START,
        effective_end=None,
        periods=(
            PricePeriod("all", "all", "Base", 0, 1440, Decimal("0.20")),
            PricePeriod("all", "event_day", "Event surcharge", 960, 1260, Decimal("0.40")),
        ),
        event_calendar=EventCalendar(
            local_dates=frozenset({date(2026, 9, 20)}),
            coverage_start=date(2026, 1, 1),
            coverage_end=date(2026, 12, 31),
        ),
    )
    assert datetime(2026, 9, 20, 23, tzinfo=UTC) in _schedule_instants(rate, NOW)


def test_far_season_weekend_start_includes_first_new_weekday_price() -> None:
    now = datetime(2028, 1, 15, 8, tzinfo=UTC)
    rate = RateVersion(
        id="synthetic",
        timezone="America/Los_Angeles",
        effective_start=now,
        effective_end=None,
        summer_months=(7, 8),
        periods=(
            PricePeriod("winter", "all", "Base", 0, 1440, Decimal("0.20")),
            PricePeriod("summer", "weekend", "Base", 0, 1440, Decimal("0.20")),
            PricePeriod("summer", "weekday", "Base", 0, 960, Decimal("0.20")),
            PricePeriod("summer", "weekday", "Peak", 960, 1260, Decimal("0.40")),
            PricePeriod("summer", "weekday", "Base", 1260, 1440, Decimal("0.20")),
        ),
    )
    current = resolve_price_period(rate, now, Decimal("0"))
    first_change = next(
        instant
        for instant in sorted(_schedule_instants(rate, now))
        if (
            resolve_price_period(rate, instant, Decimal("0")).name,
            resolve_price_period(rate, instant, Decimal("0")).price_per_kwh,
        )
        != (current.name, current.price_per_kwh)
    )
    # July 1 is Saturday. The new season's first weekday peak is Monday,
    # not the next separately enumerated month boundary on August 1.
    assert first_change == datetime(2028, 7, 3, 23, tzinfo=UTC)
