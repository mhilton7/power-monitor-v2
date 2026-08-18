from __future__ import annotations

from decimal import Decimal

import pytest
from backend.app.main import session_factory
from backend.app.models import Circuit, Device, Home
from backend.app.services.cutover_audit import redacted_cutover_snapshot
from httpx import AsyncClient
from sqlalchemy import select


@pytest.mark.asyncio
async def test_cutover_snapshot_is_read_only_redacted_and_captures_main_membership(
    owner_client: AsyncClient,
) -> None:
    async with session_factory() as session:
        home = await session.scalar(select(Home))
        assert home is not None
        main = Circuit(
            home_id=home.id,
            name="Main service",
            purpose="whole_home_total",
            is_home_total=True,
            is_billing_source=True,
            non_overlapping_confirmed=True,
            aggregate_mode="verified_sum",
        )
        session.add(main)
        await session.flush()
        members = [
            Device(
                home_id=home.id,
                circuit_id=main.id,
                friendly_name=f"Sensor {number}",
                pzem_variant="pzem004t-v4-classic-candidate",
                ct_rating_a=Decimal("100"),
                measurement_scope="full_account",
            )
            for number in (1, 2)
        ]
        session.add_all(members)
        await session.commit()

    async with session_factory() as session:
        before_counts = {
            "homes": await session.scalar(select(Home.id)),
            "devices": tuple((await session.scalars(select(Device.id).order_by(Device.id))).all()),
        }
        snapshot = await redacted_cutover_snapshot(session, home.id)
        after_counts = {
            "homes": await session.scalar(select(Home.id)),
            "devices": tuple((await session.scalars(select(Device.id).order_by(Device.id))).all()),
        }
    assert after_counts == before_counts
    assert snapshot["home"] == {"id": home.id, "name": "Test Home"}
    assert snapshot["main_service"] == {
        "id": main.id,
        "name": "Main service",
        "member_device_ids": sorted(member.id for member in members),
    }
    assert snapshot["accepted_history"] == {
        "count": 0,
        "earliest_utc": None,
        "latest_utc": None,
    }
    assert snapshot["user_count"] == 1
    assert snapshot["ota"] == {"release_count": 0, "deployment_count": 0}
    assert set(snapshot) == {
        "snapshot_at",
        "home",
        "sensors",
        "accepted_history",
        "rates",
        "user_count",
        "ota",
        "main_service",
    }
