from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from backend.app.main import session_factory
from backend.app.models import (
    Circuit,
    Device,
    DeviceHeartbeat,
    Home,
    RateAssignment,
    RatePeriod,
    RatePlan,
    RatePlanVersion,
    User,
    UtilityAccount,
    user_home_scopes,
)
from httpx import AsyncClient
from sqlalchemy import delete, select


async def _owner_scope() -> tuple[str, str]:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(User.id, user_home_scopes.c.home_id)
                .join(user_home_scopes, user_home_scopes.c.user_id == User.id)
                .where(User.email == "owner@example.com")
            )
        ).one()
        return str(row[0]), str(row[1])


SCOPED_GET_PATHS = (
    "/api/v1/home",
    "/api/v1/history",
    "/api/v1/history/export.csv",
    "/api/v1/billing",
    "/api/v1/bill-rate-imports",
    "/api/v1/devices",
    "/api/v1/circuits",
    "/api/v1/settings/home-utility",
)


def _feature_params(path: str, home_id: str | None = None) -> dict[str, str]:
    params = {"home_id": home_id} if home_id is not None else {}
    if path.startswith("/api/v1/history"):
        start = datetime(2026, 8, 1, tzinfo=UTC)
        params.update(
            {
                "from": start.isoformat(),
                "to": (start + timedelta(hours=1)).isoformat(),
            }
        )
    return params


@pytest.mark.asyncio
async def test_home_scopes_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/home-scopes")

    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_FAILED"


@pytest.mark.asyncio
async def test_home_scopes_returns_zero_or_one_authorized_scope(
    owner_client: AsyncClient,
) -> None:
    owner_id, home_id = await _owner_scope()

    one = await owner_client.get("/api/v1/home-scopes")
    assert one.status_code == 200, one.text
    assert one.json() == {"home_scopes": [{"id": home_id, "name": "Test Home"}]}

    async with session_factory() as session:
        await session.execute(
            delete(user_home_scopes).where(
                user_home_scopes.c.user_id == owner_id,
                user_home_scopes.c.home_id == home_id,
            )
        )
        await session.commit()

    zero = await owner_client.get("/api/v1/home-scopes")
    assert zero.status_code == 200, zero.text
    assert zero.json() == {"home_scopes": []}
    for path in SCOPED_GET_PATHS:
        no_implicit_home = await owner_client.get(path, params=_feature_params(path))
        assert no_implicit_home.status_code == 404, (path, no_implicit_home.text)
        assert no_implicit_home.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_home_utility_auto_resolves_exactly_one_scope_for_get_and_patch(
    owner_client: AsyncClient,
) -> None:
    _owner_id, home_id = await _owner_scope()

    fetched = await owner_client.get("/api/v1/settings/home-utility")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["home"]["id"] == home_id

    updated = await owner_client.patch("/api/v1/settings/home-utility", json={"billing_day": 7})
    assert updated.status_code == 200, updated.text
    assert updated.json()["home"]["id"] == home_id
    assert updated.json()["utility"]["billing_day"] == 7

    for path in SCOPED_GET_PATHS:
        response = await owner_client.get(path, params=_feature_params(path))
        assert response.status_code == 200, (path, response.text)


@pytest.mark.asyncio
async def test_multi_home_discovery_and_home_utility_selection_are_id_exact(
    owner_client: AsyncClient,
) -> None:
    owner_id, owner_home_id = await _owner_scope()
    async with session_factory() as session:
        second = Home(name="Test Home", timezone="America/New_York")
        hidden = Home(name="Test Home", timezone="Pacific/Honolulu")
        session.add_all((second, hidden))
        await session.flush()
        second_account = UtilityAccount(
            home_id=second.id,
            utility_name="Southern California Edison",
            timezone=second.timezone,
            billing_day=9,
            cost_scope="energy_only",
        )
        hidden_account = UtilityAccount(
            home_id=hidden.id,
            utility_name="Southern California Edison",
            timezone=hidden.timezone,
            billing_day=19,
            cost_scope="energy_only",
        )
        second_circuit = Circuit(
            home_id=second.id, name="Shared name", aggregate_mode="verified_sum"
        )
        hidden_circuit = Circuit(
            home_id=hidden.id, name="Shared name", aggregate_mode="verified_sum"
        )
        session.add_all((second_account, hidden_account, second_circuit, hidden_circuit))
        await session.flush()
        second_device = Device(
            home_id=second.id,
            circuit_id=second_circuit.id,
            friendly_name="Second sensor",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        hidden_device = Device(
            home_id=hidden.id,
            circuit_id=hidden_circuit.id,
            friendly_name="Hidden sensor",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        session.add_all((second_device, hidden_device))
        await session.execute(user_home_scopes.insert().values(user_id=owner_id, home_id=second.id))
        await session.commit()
        second_home_id = second.id
        hidden_home_id = hidden.id
        second_account_id = second_account.id
        second_circuit_id = second_circuit.id
        hidden_circuit_id = hidden_circuit.id
        second_device_id = second_device.id
        hidden_device_id = hidden_device.id

    scopes = await owner_client.get("/api/v1/home-scopes")
    assert scopes.status_code == 200, scopes.text
    expected_ids = sorted((owner_home_id, second_home_id))
    assert scopes.json() == {
        "home_scopes": [{"id": home_id, "name": "Test Home"} for home_id in expected_ids]
    }
    assert hidden_home_id not in expected_ids

    for path in SCOPED_GET_PATHS:
        ambiguous = await owner_client.get(path, params=_feature_params(path))
        assert ambiguous.status_code == 422, ambiguous.text
        assert ambiguous.json()["code"] == "INVALID_REQUEST"
        assert ambiguous.json()["detail"] == (
            "home_id is required when the actor can access multiple homes"
        )
    ambiguous_patch = await owner_client.patch(
        "/api/v1/settings/home-utility", json={"billing_day": 6}
    )
    assert ambiguous_patch.status_code == 422, ambiguous_patch.text
    assert ambiguous_patch.json()["code"] == "INVALID_REQUEST"

    selected = await owner_client.get(
        "/api/v1/settings/home-utility", params={"home_id": second_home_id}
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["home"] == {
        "id": second_home_id,
        "name": "Test Home",
        "timezone": "America/New_York",
    }
    assert selected.json()["utility"]["billing_day"] == 9

    selected_responses = {
        path: await owner_client.get(path, params=_feature_params(path, second_home_id))
        for path in SCOPED_GET_PATHS
    }
    assert all(response.status_code == 200 for response in selected_responses.values()), {
        path: response.text for path, response in selected_responses.items()
    }
    assert {row["id"] for row in selected_responses["/api/v1/home"].json()["devices"]} == {
        second_device_id
    }
    assert selected_responses["/api/v1/home"].json()["summary_scope"]["device_id"] == (
        second_device_id
    )
    assert selected_responses["/api/v1/history"].json()["scope"]["device_ids"] == [second_device_id]
    assert {row["id"] for row in selected_responses["/api/v1/devices"].json()["devices"]} == {
        second_device_id
    }
    assert {row["id"] for row in selected_responses["/api/v1/circuits"].json()["circuits"]} == {
        second_circuit_id
    }
    assert {
        row["utility_account_id"]
        for row in selected_responses["/api/v1/billing"].json()["accounts"]
    } == {second_account_id}

    updated = await owner_client.patch(
        "/api/v1/settings/home-utility",
        params={"home_id": second_home_id},
        json={"billing_day": 12},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["home"]["id"] == second_home_id
    assert updated.json()["utility"]["billing_day"] == 12

    for path in SCOPED_GET_PATHS:
        rejected = await owner_client.get(path, params=_feature_params(path, hidden_home_id))
        assert rejected.status_code == 404, rejected.text
        assert rejected.json()["code"] == "NOT_FOUND"
    rejected_patch = await owner_client.patch(
        "/api/v1/settings/home-utility",
        params={"home_id": hidden_home_id},
        json={"billing_day": 2},
    )
    assert rejected_patch.status_code == 404, rejected_patch.text
    assert rejected_patch.json()["code"] == "NOT_FOUND"

    for path in ("/api/v1/home", "/api/v1/history"):
        params = _feature_params(path, second_home_id)
        params["device_id"] = hidden_device_id
        wrong_device = await owner_client.get(path, params=params)
        assert wrong_device.status_code == 404, wrong_device.text
        assert wrong_device.json()["code"] == "NOT_FOUND"
        params = _feature_params(path, second_home_id)
        params["aggregate_circuit_id"] = hidden_circuit_id
        wrong_circuit = await owner_client.get(path, params=params)
        assert wrong_circuit.status_code == 404, wrong_circuit.text
        assert wrong_circuit.json()["code"] == "NOT_FOUND"

    owner = await owner_client.get(
        "/api/v1/settings/home-utility", params={"home_id": owner_home_id}
    )
    assert owner.status_code == 200, owner.text
    assert owner.json()["utility"]["billing_day"] == 1
    async with session_factory() as session:
        hidden_billing_day = await session.scalar(
            select(UtilityAccount.billing_day).where(UtilityAccount.home_id == hidden_home_id)
        )
    assert hidden_billing_day == 19


@pytest.mark.asyncio
async def test_dashboard_rate_timezone_and_card_cost_are_bound_to_selected_home(
    owner_client: AsyncClient,
) -> None:
    owner_id, owner_home_id = await _owner_scope()
    now = datetime.now(UTC)
    async with session_factory() as session:
        owner_account = await session.scalar(
            select(UtilityAccount).where(UtilityAccount.home_id == owner_home_id)
        )
        assert owner_account is not None
        second = Home(name="Test Home", timezone="America/New_York")
        session.add(second)
        await session.flush()
        second_account = UtilityAccount(
            home_id=second.id,
            utility_name="Southern California Edison",
            timezone="America/New_York",
            billing_day=15,
            cost_scope="energy_only",
        )
        owner_device = Device(
            home_id=owner_home_id,
            friendly_name="Pacific sensor",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        second_device = Device(
            home_id=second.id,
            friendly_name="Eastern sensor",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        owner_plan = RatePlan(name="Pacific rate", utility_name="SCE", rate_class="test")
        second_plan = RatePlan(name="Eastern rate", utility_name="SCE", rate_class="test")
        session.add_all((second_account, owner_device, second_device, owner_plan, second_plan))
        await session.flush()
        owner_version = RatePlanVersion(
            rate_plan_id=owner_plan.id,
            version=1,
            effective_start=datetime(2020, 1, 1, tzinfo=UTC),
            timezone="America/Los_Angeles",
            pricing_model="time_of_use",
            source_hash="a" * 64,
            algorithm_version="cost-v1",
            state="draft",
        )
        second_version = RatePlanVersion(
            rate_plan_id=second_plan.id,
            version=1,
            effective_start=datetime(2020, 1, 1, tzinfo=UTC),
            timezone="America/New_York",
            pricing_model="time_of_use",
            source_hash="b" * 64,
            algorithm_version="cost-v1",
            state="draft",
        )
        session.add_all((owner_version, second_version))
        await session.flush()
        session.add_all(
            (
                RatePeriod(
                    rate_plan_version_id=owner_version.id,
                    season="all",
                    day_type="all",
                    period_name="Pacific flat",
                    start_minute=0,
                    end_minute=1440,
                    price_per_kwh=Decimal("0.10"),
                ),
                RatePeriod(
                    rate_plan_version_id=second_version.id,
                    season="all",
                    day_type="all",
                    period_name="Eastern flat",
                    start_minute=0,
                    end_minute=1440,
                    price_per_kwh=Decimal("0.40"),
                ),
            )
        )
        await session.flush()
        owner_version.state = "published"
        second_version.state = "published"
        session.add_all(
            (
                RateAssignment(
                    utility_account_id=owner_account.id,
                    rate_plan_version_id=owner_version.id,
                    effective_start=owner_version.effective_start,
                    assigned_by_user_id=owner_id,
                ),
                RateAssignment(
                    utility_account_id=second_account.id,
                    rate_plan_version_id=second_version.id,
                    effective_start=second_version.effective_start,
                    assigned_by_user_id=owner_id,
                ),
                DeviceHeartbeat(
                    device_id=owner_device.id,
                    boot_id="00000000-0000-0000-0000-000000000001",
                    received_at=now,
                    measured_at=now,
                    active_power_w=Decimal("1000"),
                    pzem_status="ok",
                    storage_status="healthy",
                    time_status="trusted",
                ),
                DeviceHeartbeat(
                    device_id=second_device.id,
                    boot_id="00000000-0000-0000-0000-000000000002",
                    received_at=now,
                    measured_at=now,
                    active_power_w=Decimal("1000"),
                    pzem_status="ok",
                    storage_status="healthy",
                    time_status="trusted",
                ),
            )
        )
        await session.execute(user_home_scopes.insert().values(user_id=owner_id, home_id=second.id))
        await session.commit()
        second_home_id = second.id
        owner_device_id = owner_device.id
        second_device_id = second_device.id

    ambiguous = await owner_client.get("/api/v1/home")
    assert ambiguous.status_code == 422, ambiguous.text

    selections = (
        (
            owner_home_id,
            owner_device_id,
            "Pacific rate",
            Decimal("0.10"),
            "America/Los_Angeles",
        ),
        (
            second_home_id,
            second_device_id,
            "Eastern rate",
            Decimal("0.40"),
            "America/New_York",
        ),
    )
    for home_id, device_id, plan_name, price, timezone in selections:
        response = await owner_client.get(
            "/api/v1/home", params={"home_id": home_id, "device_id": device_id}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert {row["home_id"] for row in body["devices"]} == {home_id}
        assert body["summary_scope"]["device_id"] == device_id
        assert body["current_rate"]["plan_name"] == plan_name
        assert Decimal(str(body["current_rate"]["price_per_kwh"])) == price
        assert Decimal(str(body["devices"][0]["estimated_cost_per_hour"])) == price

        generated_at = datetime.fromisoformat(body["generated_at"].replace("Z", "+00:00"))
        local = generated_at.astimezone(ZoneInfo(timezone))
        expected_next_change = (
            local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        ).astimezone(UTC)
        actual_next_change = datetime.fromisoformat(
            body["current_rate"]["next_change_at"].replace("Z", "+00:00")
        )
        assert actual_next_change == expected_next_change
