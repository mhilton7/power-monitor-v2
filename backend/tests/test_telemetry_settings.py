from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_telemetry_history_and_retention_settings_are_independent_and_versioned(
    owner_client: AsyncClient,
) -> None:
    current = await owner_client.get("/api/v1/settings/telemetry")
    assert current.status_code == 200, current.text
    assert current.json() == {
        "home_id": current.json()["home_id"],
        "version": 1,
        "config_version": 1,
        "telemetry_interval_seconds": 5,
        "history_interval_seconds": 60,
        "retention_days": 365,
        "updated_at": current.json()["updated_at"],
    }

    rejected = await owner_client.patch("/api/v1/settings/telemetry", json={"retention_days": 30})
    assert rejected.status_code == 422

    updated = await owner_client.patch(
        "/api/v1/settings/telemetry",
        json={
            "telemetry_interval_seconds": 10,
            "history_interval_seconds": 300,
            "retention_days": 30,
            "retention_confirmation": "DELETE EXPIRED SAVED HISTORY",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["config_version"] == updated.json()["version"] == 2
    assert updated.json()["telemetry_interval_seconds"] == 10
    assert updated.json()["history_interval_seconds"] == 300
    assert updated.json()["retention_days"] == 30

    forever = await owner_client.patch("/api/v1/settings/telemetry", json={"retention_days": None})
    assert forever.status_code == 200, forever.text
    assert forever.json()["config_version"] == forever.json()["version"] == 3
    assert forever.json()["retention_days"] is None
