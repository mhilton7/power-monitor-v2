from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from backend.app.bill_rate_import.parser import extract_rate_plan_from_text
from backend.app.main import app, session_factory
from backend.app.models import (
    Alert,
    ApplicationLog,
    Device,
    DeviceHeartbeat,
    Home,
    RateSource,
    RateSyncRun,
    Role,
    User,
    UtilityAccount,
    UtilityBillRateExtraction,
    user_home_scopes,
    user_roles,
)
from backend.app.security.passwords import hash_password
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

PASSWORD = "correct horse battery staple 2026!"
RATE_SCHEDULE = """
Rate plan: TOU-D-4-9PM
Summer Weekday Off-Peak 00:00-16:00 $0.34/kWh
Summer Weekday On-Peak 16:00-21:00 $0.58/kWh
Summer Weekday Off-Peak 21:00-24:00 $0.34/kWh
Summer Weekend Off-Peak 00:00-24:00 $0.34/kWh
Summer Holiday Off-Peak 00:00-24:00 $0.34/kWh
Winter All Off-Peak 00:00-24:00 $0.37/kWh
"""


async def _owner_home() -> tuple[str, str]:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(User.id, user_home_scopes.c.home_id)
                .join(user_home_scopes, user_home_scopes.c.user_id == User.id)
                .where(User.email == "owner@example.com")
            )
        ).one()
        return row[0], row[1]


async def _create_home_owner(email: str) -> tuple[str, str, str]:
    async with session_factory() as session:
        owner_role_id = await session.scalar(select(Role.id).where(Role.name == "Owner"))
        assert owner_role_id is not None
        home = Home(name=f"Home for {email}", timezone="America/Los_Angeles")
        user = User(
            email=email,
            display_name=email.split("@", maxsplit=1)[0],
            password_hash=hash_password(PASSWORD),
        )
        session.add_all((home, user))
        await session.flush()
        account = UtilityAccount(
            home_id=home.id,
            utility_name="Southern California Edison",
            timezone="America/Los_Angeles",
            billing_day=1,
            cost_scope="energy_only",
        )
        session.add(account)
        await session.execute(user_roles.insert().values(user_id=user.id, role_id=owner_role_id))
        await session.execute(user_home_scopes.insert().values(user_id=user.id, home_id=home.id))
        await session.commit()
        return user.id, home.id, account.id


async def _logged_in_client(email: str) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="https://powermeter.test")
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = client.cookies["pm_csrf"]
    return client


@pytest.mark.asyncio
async def test_users_are_visible_by_overlap_but_global_mutations_require_full_scope(
    owner_client: AsyncClient,
) -> None:
    owner_id, owner_home_id = await _owner_home()
    other_user_id, other_home_id, _account_id = await _create_home_owner("other-owner@example.com")
    async with session_factory() as session:
        member_role_id = await session.scalar(select(Role.id).where(Role.name == "Member"))
        assert member_role_id is not None
        shared = User(
            email="shared@example.com",
            display_name="Shared user",
            password_hash=hash_password(PASSWORD),
        )
        session.add(shared)
        await session.flush()
        await session.execute(user_roles.insert().values(user_id=shared.id, role_id=member_role_id))
        for home_id in (owner_home_id, other_home_id):
            await session.execute(
                user_home_scopes.insert().values(user_id=shared.id, home_id=home_id)
            )
        await session.commit()
        shared_id = shared.id

    listed = await owner_client.get("/api/v1/users")
    assert listed.status_code == 200, listed.text
    by_id = {row["id"]: row for row in listed.json()["users"]}
    assert owner_id in by_id
    assert other_user_id not in by_id
    assert by_id[shared_id]["home_ids"] == [owner_home_id]
    assert by_id[shared_id]["manageable"] is False

    for target_id in (other_user_id, shared_id):
        response = await owner_client.patch(f"/api/v1/users/{target_id}", json={"enabled": False})
        assert response.status_code == 404, response.text

    outside_create = await owner_client.post(
        "/api/v1/users",
        json={
            "email": "outside@example.com",
            "display_name": "Outside",
            "password": PASSWORD,
            "role_names": ["Viewer"],
            "home_ids": [other_home_id],
        },
    )
    assert outside_create.status_code == 404

    # A globally unrelated Owner must not defeat last-owner protection for the
    # actor's home.
    disable_last_home_owner = await owner_client.patch(
        f"/api/v1/users/{owner_id}", json={"enabled": False}
    )
    assert disable_last_home_owner.status_code == 409


@pytest.mark.asyncio
async def test_scoped_user_manager_cannot_grant_permissions_they_do_not_have(
    owner_client: AsyncClient,
) -> None:
    role = await owner_client.post(
        "/api/v1/roles",
        json={
            "name": "Scoped User Manager",
            "description": "Identity administration without product administration",
            "permissions": ["users.view", "users.manage"],
        },
    )
    assert role.status_code == 201, role.text
    created = await owner_client.post(
        "/api/v1/users",
        json={
            "email": "manager@example.com",
            "display_name": "Manager",
            "password": PASSWORD,
            "role_names": ["Scoped User Manager"],
        },
    )
    assert created.status_code == 201, created.text
    manager = await _logged_in_client("manager@example.com")
    try:
        escalation = await manager.patch(
            f"/api/v1/users/{created.json()['id']}", json={"role_names": ["Owner"]}
        )
        assert escalation.status_code == 403, escalation.text
        role_escalation = await manager.post(
            "/api/v1/roles",
            json={
                "name": "Escalated Role",
                "description": "must be rejected",
                "permissions": ["users.manage", "system.manage"],
            },
        )
        assert role_escalation.status_code == 403, role_escalation.text
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_bill_rate_drafts_are_home_owned_and_cross_home_hashes_do_not_leak(
    owner_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_id, owner_home_id = await _owner_home()
    _other_user_id, other_home_id, other_account_id = await _create_home_owner(
        "bill-owner@example.com"
    )
    draft = extract_rate_plan_from_text(RATE_SCHEDULE, "d" * 64)
    monkeypatch.setattr(
        "backend.app.routes.billing.extract_rate_plan_from_pdf",
        lambda _data: (draft, ("BILL_USAGE", "CUSTOMER_IDENTITY")),
    )
    document = {"document": ("rates.pdf", b"%PDF-1.7 sanitized", "application/pdf")}
    first = await owner_client.post("/api/v1/bill-rate-imports", files=document)
    assert first.status_code == 201, first.text
    assert first.json()["extraction"]["home_id"] == owner_home_id
    owner_extraction_id = first.json()["extraction"]["id"]

    other = await _logged_in_client("bill-owner@example.com")
    try:
        # The same artifact is independently importable in a disjoint home;
        # neither status nor message reveals the first home's record.
        second = await other.post("/api/v1/bill-rate-imports", files=document)
        assert second.status_code == 201, second.text
        assert second.json()["extraction"]["home_id"] == other_home_id
        other_extraction_id = second.json()["extraction"]["id"]
        duplicate_same_home = await other.post("/api/v1/bill-rate-imports", files=document)
        assert duplicate_same_home.status_code == 422

        owner_list = await owner_client.get("/api/v1/bill-rate-imports")
        other_list = await other.get("/api/v1/bill-rate-imports")
        assert {row["id"] for row in owner_list.json()["extractions"]} == {owner_extraction_id}
        assert {row["id"] for row in other_list.json()["extractions"]} == {other_extraction_id}

        assert (
            await owner_client.get(f"/api/v1/bill-rate-imports/{other_extraction_id}")
        ).status_code == 404
        assert (
            await owner_client.patch(
                f"/api/v1/bill-rate-imports/{other_extraction_id}",
                json={
                    "field": "rate_class",
                    "corrected_value": "residential_time_of_use",
                },
            )
        ).status_code == 404
        assert (
            await owner_client.post(
                f"/api/v1/bill-rate-imports/{other_extraction_id}/publish",
                json={
                    "effective_start": datetime(2026, 8, 13, tzinfo=UTC).isoformat(),
                    "effective_end": None,
                    "administrator_confirmed_effective_date": True,
                },
            )
        ).status_code == 404
        assert (
            await owner_client.delete(f"/api/v1/bill-rate-imports/{other_extraction_id}")
        ).status_code == 404

        # Even an actor who can access both homes cannot assign Home A's draft
        # to Home B's utility account.
        async with session_factory() as session:
            await session.execute(
                user_home_scopes.insert().values(user_id=owner_id, home_id=other_home_id)
            )
            await session.commit()
        cross_assignment = await owner_client.post(
            f"/api/v1/bill-rate-imports/{owner_extraction_id}/publish",
            json={
                "effective_start": datetime(2026, 8, 13, tzinfo=UTC).isoformat(),
                "effective_end": None,
                "administrator_confirmed_effective_date": True,
                "assign_to_utility_account_id": other_account_id,
            },
        )
        assert cross_assignment.status_code == 404, cross_assignment.text

        async with session_factory() as session:
            other_extraction = await session.scalar(
                select(UtilityBillRateExtraction).where(
                    UtilityBillRateExtraction.id == other_extraction_id
                )
            )
            assert other_extraction is not None
            assert other_extraction.state == "review_required"
    finally:
        await other.aclose()


@pytest.mark.asyncio
async def test_health_and_diagnostics_expose_only_actor_home_evidence(
    owner_client: AsyncClient,
) -> None:
    _owner_id, owner_home_id = await _owner_home()
    _other_user_id, other_home_id, _account_id = await _create_home_owner("ops-owner@example.com")
    now = datetime.now(UTC)
    async with session_factory() as session:
        owner_device = Device(
            home_id=owner_home_id,
            friendly_name="Owner sensor",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        other_device = Device(
            home_id=other_home_id,
            friendly_name="Other sensor",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        source = RateSource(
            name="SCE public rates",
            source_type="official_https",
            https_url="https://www.sce.com/rates",
        )
        session.add_all((owner_device, other_device, source))
        await session.flush()
        owner_sync = RateSyncRun(
            source_id=source.id,
            home_id=owner_home_id,
            state="complete",
            event_code="OWNER_SYNC",
            started_at=now - timedelta(minutes=2),
            completed_at=now - timedelta(minutes=1),
        )
        other_sync = RateSyncRun(
            source_id=source.id,
            home_id=other_home_id,
            state="complete",
            event_code="OTHER_SYNC",
            started_at=now,
            completed_at=now,
        )
        session.add_all(
            (
                DeviceHeartbeat(
                    device_id=owner_device.id,
                    boot_id="00000000-0000-0000-0000-000000000001",
                    received_at=now,
                    pzem_status="connected",
                    storage_status="healthy",
                    time_status="trusted",
                ),
                DeviceHeartbeat(
                    device_id=other_device.id,
                    boot_id="00000000-0000-0000-0000-000000000002",
                    received_at=now,
                    pzem_status="connected",
                    storage_status="healthy",
                    time_status="trusted",
                ),
                Alert(
                    home_id=owner_home_id,
                    device_id=owner_device.id,
                    alert_type="reading_backlog",
                    severity="warning",
                    state="open",
                ),
                Alert(
                    home_id=other_home_id,
                    device_id=other_device.id,
                    alert_type="reading_backlog",
                    severity="warning",
                    state="open",
                ),
                owner_sync,
                other_sync,
            )
        )
        await session.flush()
        session.add_all(
            (
                ApplicationLog(event_code="A_HOME", level="info", home_id=owner_home_id),
                ApplicationLog(event_code="B_HOME", level="info", home_id=other_home_id),
                ApplicationLog(event_code="A_DEVICE", level="info", device_id=owner_device.id),
                ApplicationLog(event_code="B_DEVICE", level="info", device_id=other_device.id),
                ApplicationLog(event_code="A_SYNC", level="info", sync_id=owner_sync.id),
                ApplicationLog(event_code="B_SYNC", level="info", sync_id=other_sync.id),
                ApplicationLog(event_code="UNSCOPED", level="info"),
                ApplicationLog(
                    event_code="CONFLICTING_SCOPE",
                    level="info",
                    home_id=owner_home_id,
                    device_id=other_device.id,
                ),
            )
        )
        await session.commit()
        owner_device_id = owner_device.id

    health = await owner_client.get("/api/v1/system/health")
    assert health.status_code == 200, health.text
    body = health.json()
    assert [sensor["device_id"] for sensor in body["sensors"]] == [owner_device_id]
    assert body["open_alert_count"] == 1
    assert body["last_rate_sync"]["event_code"] == "OWNER_SYNC"

    bundle = await owner_client.get("/api/v1/diagnostics/bundle")
    assert bundle.status_code == 200, bundle.text
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        events = {
            json.loads(line)["event_code"]
            for line in archive.read("application-logs.jsonl").splitlines()
        }
    assert {"A_HOME", "A_DEVICE", "A_SYNC"}.issubset(events)
    assert {
        "B_HOME",
        "B_DEVICE",
        "B_SYNC",
        "UNSCOPED",
        "CONFLICTING_SCOPE",
    }.isdisjoint(events)
