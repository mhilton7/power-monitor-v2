from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from backend.app.main import session_factory
from backend.app.models import Circuit, Device, Home, NormalizedInterval, RawReading
from httpx import AsyncClient
from sqlalchemy import select


async def _aggregate(
    *, include_second: bool, first_power_mw: int = 100_000
) -> tuple[str, tuple[str, str], datetime, datetime]:
    start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=5)
    end = start + timedelta(minutes=1)
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        assert home_id is not None
        circuit = Circuit(
            home_id=home_id,
            name="Main service",
            purpose="whole_home_total",
            is_home_total=True,
            is_billing_source=True,
            aggregate_mode="verified_sum",
            non_overlapping_confirmed=True,
        )
        session.add(circuit)
        await session.flush()
        first = Device(
            home_id=home_id,
            circuit_id=circuit.id,
            friendly_name="Branch A",
            display_order=1,
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=100,
        )
        second = Device(
            home_id=home_id,
            circuit_id=circuit.id,
            friendly_name="Branch B",
            display_order=2,
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=100,
        )
        session.add_all((first, second))
        await session.flush()
        for index, (device, power_mw, energy_mwh) in enumerate(
            ((first, first_power_mw, 1_667), (second, 200_000, 3_333)), start=1
        ):
            if index == 2 and not include_second:
                continue
            raw = RawReading(
                device_id=device.id,
                sequence=1,
                reset_generation=0,
                interval_start_utc=start,
                interval_end_utc=end,
                monotonic_start_us=1,
                monotonic_end_us=60_000_001,
                sample_count=60,
                expected_sample_count=60,
                voltage_mv=120_000,
                current_ma=index * 1_000,
                active_power_mw=power_mw,
                frequency_mhz=60_000,
                power_factor_milli=1_000,
                pzem_energy_wh=index,
                interval_energy_mwh=energy_mwh,
                energy_selection="pzem_register_delta",
                pzem_status="ok",
                time_trusted=True,
                flags=[],
                record_crc32=index,
                payload_sha256=f"{index:x}" * 64,
            )
            session.add(raw)
            await session.flush()
            session.add(
                NormalizedInterval(
                    device_id=device.id,
                    raw_reading_id=raw.id,
                    start_utc=start,
                    end_utc=end,
                    energy_mwh=energy_mwh,
                    average_power_mw=power_mw,
                    completeness=Decimal("1"),
                    energy_selection="pzem_register_delta",
                    algorithm_version="normalize-v1",
                    source_authenticated=True,
                )
            )
        await session.commit()
        return circuit.id, (first.id, second.id), start, end


@pytest.mark.asyncio
async def test_verified_aggregate_sums_per_device_power_not_row_average(
    owner_client: AsyncClient,
) -> None:
    circuit_id, _devices, start, end = await _aggregate(include_second=True)
    response = await owner_client.get(
        "/api/v1/history",
        params={
            "from": start.isoformat(),
            "to": end.isoformat(),
            "metric": "power",
            "aggregate_circuit_id": circuit_id,
            "resolution_seconds": 60,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["points"] == [
        {
            "timestamp": start.isoformat().replace("+00:00", "Z"),
            "value": "0.3",
            "cost": None,
            "quality": "1",
        }
    ]
    assert response.json()["aggregation"]["power"] == "sum_of_per_device_time_weighted_means"


@pytest.mark.asyncio
async def test_verified_aggregate_never_bridges_a_missing_member(
    owner_client: AsyncClient,
) -> None:
    circuit_id, devices, start, end = await _aggregate(include_second=False)
    response = await owner_client.get(
        "/api/v1/history",
        params={
            "from": start.isoformat(),
            "to": end.isoformat(),
            "metric": "energy",
            "aggregate_circuit_id": circuit_id,
            "resolution_seconds": 60,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["points"][0]["value"] is None
    assert response.json()["energy_kwh"] is None
    assert Decimal(response.json()["completeness"]) == Decimal("0.5")
    assert any(item.get("device_id") == devices[1] for item in response.json()["missing_ranges"])


@pytest.mark.asyncio
async def test_history_defaults_to_main_service_and_never_sums_non_additive_measurements(
    owner_client: AsyncClient,
) -> None:
    circuit_id, devices, start, end = await _aggregate(include_second=True)
    default_response = await owner_client.get(
        "/api/v1/history",
        params={
            "from": start.isoformat(),
            "to": end.isoformat(),
            "metric": "power",
            "resolution_seconds": 60,
        },
    )
    assert default_response.status_code == 200, default_response.text
    assert default_response.json()["scope"] == {
        "device_ids": list(devices),
        "aggregate": True,
        "circuit_id": circuit_id,
    }
    assert default_response.json()["points"][0]["value"] == "0.3"

    for metric in ("voltage", "current", "frequency", "power_factor"):
        response = await owner_client.get(
            "/api/v1/history",
            params={
                "from": start.isoformat(),
                "to": end.isoformat(),
                "metric": metric,
                "aggregate_circuit_id": circuit_id,
                "resolution_seconds": 60,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["points"][0]["value"] is None


@pytest.mark.asyncio
async def test_partial_individual_history_and_measured_zero_remain_visible(
    owner_client: AsyncClient,
) -> None:
    _circuit_id, devices, start, _end = await _aggregate(include_second=True, first_power_mw=0)
    response = await owner_client.get(
        "/api/v1/history",
        params={
            "from": start.isoformat(),
            "to": (start + timedelta(minutes=5)).isoformat(),
            "metric": "power",
            "device_id": devices[0],
            "resolution_seconds": 300,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["points"][0]["value"] == "0"
    assert Decimal(response.json()["completeness"]) == Decimal("0.2")
