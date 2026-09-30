"""Repeatable real-route SQL-work check, not a production latency claim."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from statistics import median
from time import perf_counter
from typing import Any

import pytest
from backend.app.main import engine, session_factory
from backend.app.models import Device, DeviceHeartbeat, NormalizedInterval
from backend.tests.test_live_pricing import CYCLE_START, NOW, _seed
from httpx import AsyncClient
from sqlalchemy import delete, event, insert


@pytest.mark.asyncio
@pytest.mark.parametrize("sensor_count", [1, 8, 32])
async def test_pricing_route_bounds_rate_queries_and_skips_large_history_materialization(
    owner_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    sensor_count: int,
) -> None:
    from backend.app.routes import dashboard

    monkeypatch.setattr(dashboard, "_dashboard_now", lambda: NOW)
    ids = await _seed()
    member_ids = [ids["meter"]]
    async with session_factory() as session:
        for index in range(1, sensor_count):
            device = Device(
                home_id=ids["home"],
                circuit_id=ids["circuit"],
                friendly_name=f"Synthetic meter {index}",
                pzem_variant="pzem004t-v4-classic-candidate",
                ct_rating_a=Decimal("100"),
                include_in_aggregate=True,
            )
            session.add(device)
            await session.flush()
            member_ids.append(device.id)
            session.add(
                DeviceHeartbeat(
                    device_id=device.id,
                    boot_id=f"00000000-0000-0000-0000-{index:012}",
                    received_at=NOW,
                    measured_at=NOW,
                    active_power_w=Decimal("100"),
                    pzem_status="ok",
                    storage_status="healthy",
                    time_status="trusted",
                )
            )
        await session.execute(delete(NormalizedInterval))
        await session.execute(
            insert(NormalizedInterval),
            [
                {
                    "device_id": device_id,
                    "source_kind": "stateless_v2",
                    "start_utc": CYCLE_START + timedelta(minutes=5 * bucket),
                    "end_utc": CYCLE_START + timedelta(minutes=5 * (bucket + 1)),
                    "energy_mwh": 5000,
                    "completeness": Decimal("1"),
                    "energy_selection": "synthetic_test",
                    "algorithm_version": "test",
                }
                for device_id in member_ids
                for bucket in range(288)
            ],
        )
        await session.commit()
    evidence: dict[str, object] = {
        "sensor_count": sensor_count,
        "interval_count": sensor_count * 288,
        "database": "disposable SQLite",
        "samples_per_route": 3,
    }
    for path in ("/api/v1/home", "/api/v1/home/pricing"):
        await owner_client.get(path)  # Same warm application/database condition.
        timings: list[float] = []
        queries: list[str] = []

        def record_statement(
            _connection: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _many: Any,
            *,
            _queries: list[str] = queries,
        ) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                _queries.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
        try:
            for _ in range(3):
                started = perf_counter()
                response = await owner_client.get(path)
                timings.append((perf_counter() - started) * 1000)
                assert response.status_code == 200, response.text
                assert response.json()["current_rate"]["estimated_cost_per_hour"] is not None
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record_statement)
        rate_queries = sum("FROM rate_assignments" in query for query in queries) // 3
        evidence[path] = {
            "median_ms": round(median(timings), 2),
            "samples_ms": [round(t, 2) for t in timings],
            "select_count": len(queries) // 3,
            "assignment_select_count": rate_queries,
        }
        assert rate_queries <= 2  # Constant, not one complete rate calculation per sensor.
        if path.endswith("pricing"):
            assert not any("FROM interval_costs" in query for query in queries)
            assert not any("SELECT normalized_intervals.id" in query for query in queries)
    print("LIVE_PRICING_WORK " + json.dumps(evidence, sort_keys=True))
