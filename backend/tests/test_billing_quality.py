from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from backend.app.main import session_factory
from backend.app.models import (
    Device,
    Home,
    NormalizedInterval,
    StatelessTelemetrySample,
    TelemetryEnergyEvent,
)
from backend.app.routes.billing import _estimate_short_gap_energy
from httpx import AsyncClient
from sqlalchemy import func, select


def _interval(device_id: str, start: datetime, energy_mwh: int) -> NormalizedInterval:
    return NormalizedInterval(
        device_id=device_id,
        start_utc=start,
        end_utc=start + timedelta(minutes=1),
        energy_mwh=energy_mwh,
        average_power_mw=energy_mwh,
        completeness=Decimal("1"),
        energy_selection="pzem_register_delta",
        algorithm_version="billing-quality-test",
        source_authenticated=True,
        source_kind="stateless_v2",
    )


@pytest.mark.asyncio
async def test_short_gap_estimate_requires_adjacent_authenticated_neighbors_and_writes_no_history(
    owner_client: AsyncClient,
) -> None:
    gap_start = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    gap_end = gap_start + timedelta(minutes=5)
    async with session_factory() as session:
        home = await session.scalar(select(Home))
        assert home is not None
        device = Device(
            home_id=home.id,
            friendly_name="Billing quality sensor",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=Decimal("100"),
        )
        session.add(device)
        await session.flush()
        sample = StatelessTelemetrySample(
            device_id=device.id,
            boot_id="00000000-0000-0000-0000-000000000001",
            sample_sequence=1,
            telemetry_protocol="pm-protocol/1.0.0",
            sampled_at=gap_end,
            received_at=gap_end,
            effective_at=gap_end,
            sensor_time_trusted=True,
            uptime_ms=300_000,
            pzem_energy_wh=None,
            pzem_status="unavailable",
            firmware_version="0.1.0-rc.24",
            firmware_build_id="1" * 64,
            time_status="trusted",
            payload_sha256="2" * 64,
        )
        session.add(sample)
        await session.flush()
        event = TelemetryEnergyEvent(
            home_id=home.id,
            device_id=device.id,
            sample_id=sample.id,
            event_type="connection_gap_unresolved",
            gap_start_utc=gap_start,
            gap_end_utc=gap_end,
            billing_status="unresolved",
            evidence={"crosses_billing_cycle": False},
        )
        session.add_all(
            (
                event,
                _interval(device.id, gap_start - timedelta(hours=2), 1_000),
                _interval(device.id, gap_end + timedelta(hours=2), 2_000),
            )
        )
        await session.flush()

        far_neighbors = await _estimate_short_gap_energy(
            session,
            events=[event],
            cycle_start=gap_start - timedelta(days=1),
            scope_end=gap_end + timedelta(days=1),
            reading_coverage=Decimal("0.9942"),
            minimum_coverage=Decimal("0.95"),
            maximum_gap_seconds=900,
            unresolved_counter_resets=0,
        )
        assert far_neighbors["estimated_mwh"] == Decimal("0")
        assert far_neighbors["unknown_count"] == 1
        assert far_neighbors["details"] == [
            {
                "event_id": event.id,
                "status": "unknown",
                "duration_seconds": 300,
                "reason": "neighboring_intervals_unavailable",
            }
        ]

        session.add_all(
            (
                _interval(device.id, gap_start - timedelta(minutes=1), 1_000),
                _interval(device.id, gap_end, 2_000),
            )
        )
        await session.flush()
        before_count = int(
            await session.scalar(
                select(func.count(NormalizedInterval.id)).where(
                    NormalizedInterval.device_id == device.id
                )
            )
            or 0
        )
        adjacent = await _estimate_short_gap_energy(
            session,
            events=[event],
            cycle_start=gap_start - timedelta(days=1),
            scope_end=gap_end + timedelta(days=1),
            reading_coverage=Decimal("0.9942"),
            minimum_coverage=Decimal("0.95"),
            maximum_gap_seconds=900,
            unresolved_counter_resets=0,
        )
        after_count = int(
            await session.scalar(
                select(func.count(NormalizedInterval.id)).where(
                    NormalizedInterval.device_id == device.id
                )
            )
            or 0
        )
        assert adjacent["estimated_mwh"] == Decimal("7500")
        assert adjacent["lower_mwh"] == Decimal("5000")
        assert adjacent["upper_mwh"] == Decimal("10000")
        assert adjacent["methods"] == ("short_gap_neighbor_interpolation",)
        assert adjacent["raw_history_modified"] is False
        assert after_count == before_count == 4
