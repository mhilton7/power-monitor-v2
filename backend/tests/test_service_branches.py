from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from backend.app.main import session_factory
from backend.app.models import AuditEvent, Circuit, Device, Home, UtilityAccount
from httpx import AsyncClient
from sqlalchemy import select


async def _devices(count: int = 4) -> tuple[str, list[str]]:
    async with session_factory() as session:
        home = await session.scalar(select(Home))
        assert home is not None
        devices = [
            Device(
                home_id=home.id,
                friendly_name=f"Meter {index}",
                pzem_variant="pzem004t-v4-classic-candidate",
                ct_rating_a=Decimal("100"),
                display_order=index,
            )
            for index in range(count)
        ]
        session.add_all(devices)
        await session.commit()
        return home.id, [device.id for device in devices]


@pytest.mark.asyncio
async def test_service_branch_crud_designates_one_home_total_and_audits(
    owner_client: AsyncClient,
) -> None:
    home_id, device_ids = await _devices()
    first = await owner_client.post(
        "/api/v1/circuits",
        json={
            "home_id": home_id,
            "name": "Main service",
            "description": "Complete home measurement",
            "purpose": "whole_home_total",
            "is_home_total": True,
            "device_ids": device_ids[:2],
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    assert first.status_code == 201, first.text
    first_body = first.json()
    assert first_body["name"] == "Main service"
    assert first_body["is_home_total"] is True
    assert first_body["non_overlapping_confirmed"] is True
    assert first_body["device_ids"] == device_ids[:2]
    assert first_body["created_at"] is not None
    assert first_body["updated_at"] is not None

    async with session_factory() as session:
        account = await session.scalar(
            select(UtilityAccount).where(UtilityAccount.home_id == home_id)
        )
        members = (await session.scalars(select(Device).where(Device.id.in_(device_ids[:2])))).all()
        assert account is not None and account.cost_scope == "full_account"
        assert {member.measurement_scope for member in members} == {"full_account"}

    second = await owner_client.post(
        "/api/v1/circuits",
        json={
            "home_id": home_id,
            "name": "Workshop",
            "purpose": "electrical_section",
            "is_home_total": False,
            "device_ids": device_ids[2:],
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]
    selected = await owner_client.patch(
        f"/api/v1/circuits/{second_id}",
        json={
            "name": "Replacement main",
            "description": "New designated total",
            "is_home_total": True,
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["purpose"] == "whole_home_total"
    assert selected.json()["is_home_total"] is True

    listed = await owner_client.get("/api/v1/circuits", params={"home_id": home_id})
    assert listed.status_code == 200, listed.text
    branches = {item["id"]: item for item in listed.json()["circuits"]}
    assert branches[first_body["id"]]["is_home_total"] is False
    assert branches[second_id]["is_home_total"] is True

    protected = await owner_client.delete(f"/api/v1/circuits/{second_id}")
    assert protected.status_code == 409, protected.text
    still_used = await owner_client.delete(f"/api/v1/circuits/{first_body['id']}")
    assert still_used.status_code == 409, still_used.text
    cleared = await owner_client.patch(
        f"/api/v1/circuits/{first_body['id']}",
        json={
            "device_ids": [],
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    assert cleared.status_code == 200, cleared.text
    deleted = await owner_client.delete(f"/api/v1/circuits/{first_body['id']}")
    assert deleted.status_code == 204, deleted.text

    async with session_factory() as session:
        assert await session.get(Circuit, first_body["id"]) is None
        events = set(
            (
                await session.scalars(
                    select(AuditEvent.event_code).where(
                        AuditEvent.event_code.in_(
                            (
                                "SERVICE_BRANCH_CREATED",
                                "SERVICE_BRANCH_UPDATED",
                                "SERVICE_BRANCH_DELETED",
                            )
                        )
                    )
                )
            ).all()
        )
        assert events == {
            "SERVICE_BRANCH_CREATED",
            "SERVICE_BRANCH_UPDATED",
            "SERVICE_BRANCH_DELETED",
        }


@pytest.mark.asyncio
async def test_home_total_member_removal_requires_replacement_branch(
    owner_client: AsyncClient,
) -> None:
    home_id, device_ids = await _devices(2)
    created = await owner_client.post(
        "/api/v1/circuits",
        json={
            "home_id": home_id,
            "name": "Main service",
            "purpose": "whole_home_total",
            "is_home_total": True,
            "device_ids": device_ids,
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    assert created.status_code == 201, created.text
    response = await owner_client.patch(
        f"/api/v1/circuits/{created.json()['id']}",
        json={
            "device_ids": device_ids[:1],
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    assert response.status_code == 409
    assert "required member" in response.json()["detail"]


@pytest.mark.asyncio
async def test_home_total_requires_two_members_and_matching_purpose(
    owner_client: AsyncClient,
) -> None:
    home_id, device_ids = await _devices(1)
    single = await owner_client.post(
        "/api/v1/circuits",
        json={
            "home_id": home_id,
            "name": "Invalid total",
            "purpose": "whole_home_total",
            "is_home_total": True,
            "device_ids": device_ids,
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    assert single.status_code == 409, single.text
    assert "at least two active sensors" in single.json()["detail"]

    empty = await owner_client.post(
        "/api/v1/circuits",
        json={
            "home_id": home_id,
            "name": "Empty",
            "device_ids": [],
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    assert empty.status_code == 422, empty.text

    mismatched = await owner_client.post(
        "/api/v1/circuits",
        json={
            "home_id": home_id,
            "name": "Misclassified",
            "purpose": "whole_home_total",
            "is_home_total": False,
            "device_ids": device_ids,
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    assert mismatched.status_code == 422, mismatched.text


@pytest.mark.asyncio
async def test_branch_membership_can_move_explicitly_but_not_from_home_total(
    owner_client: AsyncClient,
) -> None:
    home_id, device_ids = await _devices(4)
    source = await owner_client.post(
        "/api/v1/circuits",
        json={
            "home_id": home_id,
            "name": "Workshop",
            "device_ids": device_ids[:2],
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    target = await owner_client.post(
        "/api/v1/circuits",
        json={
            "home_id": home_id,
            "name": "Garage",
            "device_ids": device_ids[2:],
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    assert source.status_code == 201 and target.status_code == 201
    moved = await owner_client.patch(
        f"/api/v1/circuits/{target.json()['id']}",
        json={
            "device_ids": [*device_ids[2:], device_ids[0]],
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["device_ids"] == [device_ids[0], *device_ids[2:]]
    listed = await owner_client.get("/api/v1/circuits", params={"home_id": home_id})
    by_id = {item["id"]: item for item in listed.json()["circuits"]}
    assert by_id[source.json()["id"]]["device_ids"] == [device_ids[1]]

    home_total = await owner_client.patch(
        f"/api/v1/circuits/{source.json()['id']}",
        json={
            "device_ids": [device_ids[1], device_ids[3]],
            "is_home_total": True,
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    # device_ids[3] still belongs to target; it may move because target is not
    # the protected home-total branch.
    assert home_total.status_code == 200, home_total.text
    blocked = await owner_client.patch(
        f"/api/v1/circuits/{target.json()['id']}",
        json={
            "device_ids": [device_ids[2], device_ids[1]],
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert "home-total" in blocked.json()["detail"]


@pytest.mark.asyncio
async def test_branch_assignment_rejects_cross_home_and_revoked_sensors(
    owner_client: AsyncClient,
) -> None:
    home_id, device_ids = await _devices(2)
    async with session_factory() as session:
        hidden_home = Home(name="Hidden")
        session.add(hidden_home)
        await session.flush()
        hidden = Device(
            home_id=hidden_home.id,
            friendly_name="Other home",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        revoked = await session.get(Device, device_ids[1])
        assert revoked is not None
        revoked.revoked_at = datetime.now(UTC)
        session.add(hidden)
        await session.commit()
        hidden_id = hidden.id

    for rejected_id in (hidden_id, device_ids[1]):
        response = await owner_client.post(
            "/api/v1/circuits",
            json={
                "home_id": home_id,
                "name": f"Rejected {rejected_id}",
                "device_ids": [rejected_id],
                "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
            },
        )
        assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_revoked_member_remains_in_branch_topology_for_historical_gaps(
    owner_client: AsyncClient,
) -> None:
    home_id, device_ids = await _devices(2)
    created = await owner_client.post(
        "/api/v1/circuits",
        json={
            "home_id": home_id,
            "name": "Historical branch",
            "device_ids": device_ids,
            "confirmation": "I VERIFIED THESE NON-OVERLAPPING METERS",
        },
    )
    assert created.status_code == 201, created.text
    revoked = await owner_client.post(
        f"/api/v1/devices/{device_ids[1]}/revoke",
        json={"confirmation": "REVOKE SENSOR"},
    )
    assert revoked.status_code == 204, revoked.text
    listed = await owner_client.get("/api/v1/circuits", params={"home_id": home_id})
    branch = next(item for item in listed.json()["circuits"] if item["id"] == created.json()["id"])
    assert branch["device_ids"] == device_ids
