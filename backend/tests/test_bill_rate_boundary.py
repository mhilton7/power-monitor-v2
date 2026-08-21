from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from backend.app.bill_rate_import.parser import extract_rate_plan_from_text
from backend.app.main import session_factory
from backend.app.models import Base, NormalizedInterval, RawReading, Rollup
from backend.app.schemas.billing import RatePlanDraft
from backend.app.services.cost_engine import (
    CostContext,
    PricePeriod,
    RateVersion,
    price_sensor_interval,
)
from httpx import AsyncClient
from sqlalchemy import func, select

SCHEDULE = """
Rate plan: TOU-D-4-9PM
Base Services Charge $0.79 per day
Baseline Credit $0.10/kWh
Summer Weekday Off-Peak 00:00-16:00 $0.34/kWh
Summer Weekday On-Peak 16:00-21:00 $0.58/kWh
Summer Weekday Off-Peak 21:00-24:00 $0.34/kWh
Summer Weekend Off-Peak 00:00-16:00 $0.34/kWh
Summer Weekend Mid-Peak 16:00-21:00 $0.46/kWh
Summer Weekend Off-Peak 21:00-24:00 $0.34/kWh
Summer Holiday Off-Peak 00:00-16:00 $0.34/kWh
Summer Holiday Mid-Peak 16:00-21:00 $0.46/kWh
Summer Holiday Off-Peak 21:00-24:00 $0.34/kWh
Winter All Off-Peak 00:00-08:00 $0.37/kWh
Winter All Super-Off-Peak 08:00-16:00 $0.33/kWh
Winter All Mid-Peak 16:00-21:00 $0.51/kWh
Winter All Off-Peak 21:00-24:00 $0.37/kWh
"""


def normalized(draft: RatePlanDraft) -> dict[str, Any]:
    value = draft.model_dump(mode="json")
    value.pop("source_artifact_sha256")
    for field in value["fields"]:
        field.pop("source")
    return value


def test_same_rates_different_customer_usage_and_total_are_invariant() -> None:
    first = (
        SCHEDULE + "\nCustomer: Jane Example\nAccount Number: 111\nTotal kWh 120\nAmount Due $44.12"
    )
    second = (
        SCHEDULE
        + "\nCustomer: Different Person\nAccount Number: 999\nTotal kWh 9120\nAmount Due $3444.99"
    )
    one = extract_rate_plan_from_text(first, "a" * 64)
    two = extract_rate_plan_from_text(second, "b" * 64)
    assert normalized(one) == normalized(two)
    serialized = str(normalized(one)).lower()
    for forbidden in ("jane", "111", "44.12", "different person", "999", "9120", "3444.99"):
        assert forbidden not in serialized


def test_bill_total_cannot_change_cost_with_same_sensor_and_rate() -> None:
    draft_one = extract_rate_plan_from_text(SCHEDULE + "\nAmount Due $1.00", "a" * 64)
    draft_two = extract_rate_plan_from_text(SCHEDULE + "\nAmount Due $9999.00", "b" * 64)
    periods = tuple(
        PricePeriod(
            period.season,
            period.day_type,
            period.name,
            period.start_minute,
            period.end_minute,
            period.price_per_kwh,
        )
        for period in draft_one.periods
    )
    rate = RateVersion(
        id="published-rate-version",
        timezone="America/Los_Angeles",
        effective_start=datetime(2026, 1, 1, tzinfo=UTC),
        effective_end=None,
        periods=periods,
    )
    assert draft_one.periods == draft_two.periods
    first_cost = price_sensor_interval(
        start_utc=datetime(2026, 8, 13, 20, tzinfo=UTC),
        end_utc=datetime(2026, 8, 13, 20, 1, tzinfo=UTC),
        energy_mwh=500_000,
        rate=rate,
        context=CostContext(),
    ).total_microdollars
    second_cost = price_sensor_interval(
        start_utc=datetime(2026, 8, 13, 20, tzinfo=UTC),
        end_utc=datetime(2026, 8, 13, 20, 1, tzinfo=UTC),
        energy_mwh=500_000,
        rate=rate,
        context=CostContext(),
    ).total_microdollars
    assert first_cost == second_cost


@pytest.mark.asyncio
async def test_bill_upload_route_creates_zero_usage_history_or_rollups(
    owner_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = extract_rate_plan_from_text(SCHEDULE, "c" * 64)
    monkeypatch.setattr(
        "backend.app.routes.billing.extract_rate_plan_from_pdf",
        lambda _data: (draft, ("BILL_USAGE", "BILL_TOTAL", "CUSTOMER_IDENTITY")),
    )
    response = await owner_client.post(
        "/api/v1/bill-rate-imports",
        files={"document": ("rates.pdf", b"%PDF-1.7 sanitized", "application/pdf")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "usage_source_notice" in body
    invalid_rate = await owner_client.patch(
        f"/api/v1/bill-rate-imports/{body['extraction']['id']}",
        json={"field": "baseline_credit_rate", "corrected_value": "NaN"},
    )
    assert invalid_rate.status_code == 422, invalid_rate.text
    assert invalid_rate.json()["code"] == "VALIDATION_ERROR"
    async with session_factory() as session:
        assert await session.scalar(select(func.count(RawReading.id))) == 0
        assert await session.scalar(select(func.count(NormalizedInterval.id))) == 0
        assert await session.scalar(select(func.count(Rollup.id))) == 0


@pytest.mark.asyncio
async def test_bill_publish_rejects_day_sensitive_schedule_without_holiday_calendar(
    owner_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = extract_rate_plan_from_text(SCHEDULE, "f" * 64)
    monkeypatch.setattr(
        "backend.app.routes.billing.extract_rate_plan_from_pdf",
        lambda _data: (draft, ()),
    )
    uploaded = await owner_client.post(
        "/api/v1/bill-rate-imports",
        files={"document": ("rates.pdf", b"%PDF-1.7 sanitized", "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    published = await owner_client.post(
        f"/api/v1/bill-rate-imports/{uploaded.json()['extraction']['id']}/publish",
        json={
            "effective_start": "2026-08-01T07:00:00Z",
            "effective_end": None,
            "administrator_confirmed_effective_date": True,
        },
    )
    assert published.status_code == 422, published.text
    assert published.json()["code"] == "RATE_HOLIDAY_CALENDAR_REQUIRED"


def test_database_has_no_prohibited_bill_columns() -> None:
    prohibited = {
        "customer_name",
        "account_number",
        "service_address",
        "meter_number",
        "current_meter_reading",
        "previous_meter_reading",
        "total_kwh",
        "total_bill",
        "amount_due",
        "current_balance",
        "previous_balance",
        "payment_history",
        "payment_method",
    }
    all_columns = {
        column.name.lower() for table in Base.metadata.tables.values() for column in table.columns
    }
    assert prohibited.isdisjoint(all_columns)
    assert not any("historical_bill" in table for table in Base.metadata.tables)


def test_rate_draft_rejects_a_missing_season_or_day_type() -> None:
    complete = extract_rate_plan_from_text(SCHEDULE, "d" * 64)
    payload = complete.model_dump()
    payload["periods"] = tuple(
        period
        for period in complete.periods
        if not (period.season == "summer" and period.day_type == "holiday")
    )
    with pytest.raises(ValueError, match="resolve exactly once"):
        RatePlanDraft.model_validate(payload)


def test_rate_draft_rejects_months_that_do_not_partition_the_year() -> None:
    complete = extract_rate_plan_from_text(SCHEDULE, "e" * 64)
    payload = complete.model_dump()
    payload["winter_months"] = (1, 2, 3)
    with pytest.raises(ValueError, match="partition all twelve months"):
        RatePlanDraft.model_validate(payload)
