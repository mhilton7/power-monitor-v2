from __future__ import annotations

import base64
import secrets
import time
from datetime import UTC, datetime, timedelta

import orjson
import pytest
from backend.app.main import session_factory
from backend.app.models import (
    DeviceTelemetryState,
    Home,
    HomeTelemetrySetting,
    NormalizedInterval,
    StatelessTelemetrySample,
    TelemetryEnergyEvent,
)
from backend.app.schemas.device import StatelessTelemetryRequest
from backend.app.security.protocol import (
    body_sha256,
    canonical_request,
    derive_directional_key,
    sign_request,
)
from backend.app.services.stateless_telemetry import (
    apply_stateless_history_retention,
    finalize_stateless_history,
    ingest_stateless_sample,
)
from httpx import AsyncClient, Response
from sqlalchemy import select

PATH = "/api/v1/device/telemetry/v2"
BOOT_ID = "123e4567-e89b-12d3-a456-426614174000"


def _headers(device_id: str, secret: bytes, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    digest = body_sha256(body)
    canonical = canonical_request("POST", PATH, "", timestamp, nonce, digest)
    return {
        "X-PM-Protocol": "pm-protocol/1.0.0",
        "X-PM-Device-ID": device_id,
        "X-PM-Timestamp": timestamp,
        "X-PM-Nonce": nonce,
        "X-PM-Content-SHA256": digest,
        "X-PM-Signature": sign_request(
            derive_directional_key(secret, device_id, "device-to-server"), canonical
        ),
        "Content-Type": "application/json",
    }


async def _enroll(
    owner_client: AsyncClient, *, name: str = "Stateless target", fingerprint: str = "fixture"
) -> tuple[str, bytes]:
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
    assert home_id is not None
    token = await owner_client.post(
        "/api/v1/enrollment-tokens",
        json={
            "home_id": home_id,
            "friendly_name": name,
            "ct_rating_a": "100",
            "pzem_variant": "pzem004t-v4-classic-candidate",
            "expires_minutes": 15,
        },
    )
    enrolled = await owner_client.post(
        "/api/v1/devices/enroll",
        json={
            "enrollment_token": token.json()["token"],
            "protocol_id": "pm-protocol/1.0.0",
            "firmware_version": "0.1.0-rc.17",
            "hardware_fingerprint": f"stateless-v2-{fingerprint}",
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    return enrolled.json()["device_id"], base64.b64decode(enrolled.json()["device_secret"])


def _payload(
    device_id: str,
    sequence: int,
    *,
    sampled_at: datetime | None = None,
    energy_wh: int | None = 1000,
    status: str = "ok",
    power_w: str = "120.000",
    firmware_version: str = "0.1.0-rc.17",
) -> dict[str, object]:
    good = status == "ok"
    return {
        "telemetry_protocol": "pm-telemetry/2.0.0",
        "sensor_id": device_id,
        "boot_id": BOOT_ID,
        "sample_sequence": sequence,
        "sampled_at": sampled_at,
        "uptime_ms": sequence * 5000,
        "voltage_v": "240.000" if good else None,
        "current_a": "0.5000" if good else None,
        "active_power_w": power_w if good else None,
        "frequency_hz": "60.000" if good else None,
        "power_factor": "0.9000" if good else None,
        "pzem_energy_wh": energy_wh if good else None,
        "pzem_status": status,
        "firmware_version": firmware_version,
        "firmware_build_id": f"{sequence:064x}",
        "time_status": "trusted" if sampled_at is not None else "untrusted",
        "wifi_rssi": -55,
        "command_results": [],
    }


async def _post(
    client: AsyncClient, device_id: str, secret: bytes, payload: dict[str, object]
) -> Response:
    body = orjson.dumps(payload)
    return await client.post(PATH, content=body, headers=_headers(device_id, secret, body))


@pytest.mark.parametrize(
    "firmware_build_id",
    (
        "A" * 64,
        "g" * 64,
        "a" * 63,
        "a" * 65,
    ),
)
def test_stateless_build_identity_is_exact_lowercase_elf_sha256(
    firmware_build_id: str,
) -> None:
    payload = _payload("123e4567-e89b-12d3-a456-426614174000", 1)
    assert StatelessTelemetryRequest.model_validate(payload).firmware_build_id == f"{1:064x}"
    payload["firmware_build_id"] = firmware_build_id
    with pytest.raises(ValueError, match="firmware_build_id"):
        StatelessTelemetryRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_authenticated_samples_are_independent_idempotent_and_have_no_ack_contract(
    owner_client: AsyncClient,
) -> None:
    device_id, secret = await _enroll(owner_client)
    first = await _post(owner_client, device_id, secret, _payload(device_id, 10))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "accepted"
    assert first.json()["telemetry_protocol"] == "pm-telemetry/2.0.0"
    assert not ({"highest_contiguous_sequence", "gaps", "backlog"} & first.json().keys())

    noncontiguous = await _post(
        owner_client, device_id, secret, _payload(device_id, 500, energy_wh=1001)
    )
    assert noncontiguous.status_code == 200, noncontiguous.text
    assert noncontiguous.json()["status"] == "accepted"

    duplicate = await _post(
        owner_client, device_id, secret, _payload(device_id, 500, energy_wh=1001)
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["status"] == "duplicate"

    changed = await _post(owner_client, device_id, secret, _payload(device_id, 500, energy_wh=1002))
    assert changed.status_code == 409

    listed = await owner_client.get("/api/v1/devices")
    assert listed.status_code == 200, listed.text
    device = listed.json()["devices"][0]
    assert device["protocol"] == "pm-telemetry/2.0.0"
    assert device["backlog"] is None
    assert device["acknowledgement"] is None
    assert device["storage_status"] == "not_applicable_stateless"
    assert device["synchronization"]["mode"] == "stateless_delivery"
    assert device["synchronization"]["queued_records"] is None

    health = await owner_client.get("/api/v1/system/health")
    assert health.status_code == 200, health.text
    sensor = next(item for item in health.json()["sensors"] if item["device_id"] == device_id)
    assert sensor["protocol"] == "pm-telemetry/2.0.0"
    assert sensor["backlog"] is None
    assert sensor["acknowledgement"] is None
    assert sensor["server_delivery_status"] in {"accepted", "delayed"}
    assert sensor["last_server_received_at"] is not None
    assert sensor["last_sensor_sampled_at"] is None
    assert sensor["sensor_time_trusted"] is False
    assert sensor["latest_stored_history_interval_at"] is not None
    assert sensor["recent_accepted_sample_count"] == 2
    assert sensor["recent_acceptance_window_seconds"] == 3600
    assert sensor["synchronization"]["mode"] == "stateless_delivery"

    async with session_factory() as session:
        samples = (
            await session.scalars(
                select(StatelessTelemetrySample).where(
                    StatelessTelemetrySample.device_id == device_id
                )
            )
        ).all()
    assert len(samples) == 2


@pytest.mark.asyncio
async def test_invalid_intermediate_and_out_of_order_samples_do_not_regress_live_state(
    owner_client: AsyncClient,
) -> None:
    device_id, _secret = await _enroll(owner_client)
    base = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        valid_1 = StatelessTelemetryRequest.model_validate(
            _payload(device_id, 1, sampled_at=base, energy_wh=1000)
        )
        await ingest_stateless_sample(session, device_id, valid_1, now=base)
        await session.commit()
    async with session_factory() as session:
        invalid = StatelessTelemetryRequest.model_validate(
            _payload(device_id, 2, sampled_at=base + timedelta(seconds=5), status="timeout")
        )
        await ingest_stateless_sample(session, device_id, invalid, now=base + timedelta(seconds=5))
        await session.commit()
    async with session_factory() as session:
        valid_2 = StatelessTelemetryRequest.model_validate(
            _payload(
                device_id,
                3,
                sampled_at=base + timedelta(seconds=10),
                energy_wh=1001,
                power_w="140.000",
            )
        )
        await ingest_stateless_sample(session, device_id, valid_2, now=base + timedelta(seconds=10))
        await session.commit()
    async with session_factory() as session:
        interval_energy = await session.scalar(
            select(NormalizedInterval.energy_mwh).where(
                NormalizedInterval.device_id == device_id,
                NormalizedInterval.source_kind == "stateless_v2",
            )
        )
        state = await session.get(DeviceTelemetryState, device_id)
        assert state is not None
        current_sample_id = state.latest_sample_id
    assert interval_energy == 1000

    async with session_factory() as session:
        late = StatelessTelemetryRequest.model_validate(
            _payload(
                device_id,
                4,
                sampled_at=base + timedelta(seconds=2),
                energy_wh=999,
                power_w="0.000",
                firmware_version="0.1.0-rc.16",
            )
        )
        result = await ingest_stateless_sample(
            session, device_id, late, now=base + timedelta(seconds=12)
        )
        assert result.advances_live_state is False
        await session.commit()
    async with session_factory() as session:
        state = await session.get(DeviceTelemetryState, device_id)
        assert state is not None
        assert state.latest_sample_id == current_sample_id
        assert state.firmware_version == "0.1.0-rc.17"
        resets = await session.scalar(
            select(TelemetryEnergyEvent.id).where(
                TelemetryEnergyEvent.device_id == device_id,
                TelemetryEnergyEvent.event_type == "counter_reset",
            )
        )
    assert resets is None


@pytest.mark.asyncio
async def test_invalid_first_reconnect_does_not_hide_recovered_gap_energy(
    owner_client: AsyncClient,
) -> None:
    device_id, _secret = await _enroll(owner_client)
    base = datetime(2026, 8, 17, 14, 0, tzinfo=UTC)
    samples = (
        StatelessTelemetryRequest.model_validate(
            _payload(device_id, 1, sampled_at=base, energy_wh=1000)
        ),
        StatelessTelemetryRequest.model_validate(
            _payload(
                device_id,
                2,
                sampled_at=base + timedelta(seconds=60),
                status="timeout",
            )
        ),
        StatelessTelemetryRequest.model_validate(
            _payload(
                device_id,
                3,
                sampled_at=base + timedelta(seconds=65),
                energy_wh=1010,
            )
        ),
    )
    async with session_factory() as session:
        for offset, sample in enumerate(samples):
            await ingest_stateless_sample(
                session,
                device_id,
                sample,
                now=base + timedelta(seconds=(0, 60, 65)[offset]),
            )
            await session.commit()
    async with session_factory() as session:
        events = (
            await session.scalars(
                select(TelemetryEnergyEvent).where(TelemetryEnergyEvent.device_id == device_id)
            )
        ).all()
        intervals = (
            await session.scalars(
                select(NormalizedInterval).where(NormalizedInterval.device_id == device_id)
            )
        ).all()
    assert len(events) == 1
    assert events[0].event_type == "connection_gap_recovered"
    assert events[0].recovered_energy_mwh == 10_000
    assert events[0].billing_status == "included"
    assert all(interval.energy_mwh is None for interval in intervals)


@pytest.mark.asyncio
async def test_measured_zero_is_preserved_and_missing_initial_energy_remains_null(
    owner_client: AsyncClient,
) -> None:
    device_id, _secret = await _enroll(owner_client)
    base = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)
    async with session_factory() as session:
        initial = StatelessTelemetryRequest.model_validate(
            _payload(device_id, 1, sampled_at=base, energy_wh=0, power_w="50.000")
        )
        await ingest_stateless_sample(session, device_id, initial, now=base)
        zero = StatelessTelemetryRequest.model_validate(
            _payload(
                device_id,
                2,
                sampled_at=base + timedelta(seconds=5),
                energy_wh=0,
                power_w="0.000",
            )
        )
        await ingest_stateless_sample(session, device_id, zero, now=base + timedelta(seconds=5))
        await session.commit()
    async with session_factory() as session:
        interval = await session.scalar(
            select(NormalizedInterval).where(NormalizedInterval.device_id == device_id)
        )
    assert interval is not None
    assert interval.minimum_power_mw == 0
    assert interval.energy_mwh == 0


@pytest.mark.asyncio
async def test_retention_removes_only_expired_derived_history_not_accepted_samples(
    owner_client: AsyncClient,
) -> None:
    device_id, _secret = await _enroll(owner_client)
    accepted_at = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        payload = StatelessTelemetryRequest.model_validate(
            _payload(device_id, 1, sampled_at=accepted_at, energy_wh=1000)
        )
        await ingest_stateless_sample(session, device_id, payload, now=accepted_at)
        settings = await session.scalar(select(HomeTelemetrySetting))
        assert settings is not None
        settings.retention_days = 30
        await finalize_stateless_history(session, now=accepted_at + timedelta(minutes=2))
        await session.commit()
    async with session_factory() as session:
        removed = await apply_stateless_history_retention(
            session, now=accepted_at + timedelta(days=31)
        )
        await session.commit()
    assert removed == 1
    async with session_factory() as session:
        assert await session.scalar(select(StatelessTelemetrySample.id)) is not None
        assert await session.scalar(select(NormalizedInterval.id)) is None


@pytest.mark.asyncio
async def test_two_sensors_share_one_seeded_home_configuration_on_first_samples(
    owner_client: AsyncClient,
) -> None:
    first_id, _first_secret = await _enroll(
        owner_client, name="First stateless sensor", fingerprint="first"
    )
    second_id, _second_secret = await _enroll(
        owner_client, name="Second stateless sensor", fingerprint="second"
    )
    instant = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)
    for device_id in (first_id, second_id):
        async with session_factory() as session:
            result = await ingest_stateless_sample(
                session,
                device_id,
                StatelessTelemetryRequest.model_validate(
                    _payload(device_id, 1, sampled_at=instant, energy_wh=1000)
                ),
                now=instant,
            )
            assert result.config_version == 1
            assert result.telemetry_interval_seconds == 5
            await session.commit()
    async with session_factory() as session:
        settings = (await session.scalars(select(HomeTelemetrySetting))).all()
    assert len(settings) == 1
