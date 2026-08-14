from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from backend.app.errors import IntegrityConflict
from backend.app.main import session_factory
from backend.app.models import Device, DeviceCommand, Home, NormalizedInterval, RawReading
from backend.app.schemas.device import CommandResult
from backend.app.services.commands import apply_command_results, expire_prepare_tokens
from httpx import AsyncClient
from sqlalchemy import select


async def _device() -> Device:
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        assert home_id is not None
        device = Device(
            home_id=home_id,
            friendly_name="Destructive command target",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=100,
            maximum_sequence=42,
            contiguous_ack=42,
        )
        session.add(device)
        await session.commit()
        await session.refresh(device)
        return device


def _request(
    command_type: str,
    idempotency_key: str,
    *,
    prepare_id: str | None = None,
    token: str | None = None,
    phrase: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "command_type": command_type,
        "idempotency_key": idempotency_key,
        "payload": {},
    }
    if prepare_id is not None:
        body.update(
            {
                "prepare_command_id": prepare_id,
                "confirmation_token": token,
                "typed_confirmation": phrase,
            }
        )
    return body


@pytest.mark.asyncio
async def test_destructive_commit_requires_exact_phrase_and_one_time_prepare(
    owner_client: AsyncClient,
) -> None:
    device = await _device()
    prepared = await owner_client.post(
        f"/api/v1/devices/{device.id}/commands",
        json=_request("data_reset_prepare", "reset-prepare-0001"),
    )
    assert prepared.status_code == 202, prepared.text
    prepare_id = prepared.json()["command"]["id"]
    token = prepared.json()["confirmation_token"]
    assert isinstance(token, str) and len(token) == 32

    retry = await owner_client.post(
        f"/api/v1/devices/{device.id}/commands",
        json=_request("data_reset_prepare", "reset-prepare-0001"),
    )
    assert retry.status_code == 202, retry.text
    assert retry.json()["command"]["id"] == prepare_id
    assert retry.json()["confirmation_token"] == token

    async with session_factory() as session:
        command = await session.get(DeviceCommand, prepare_id)
        assert command is not None
        assert command.required_firmware_capability == "destructive_commands_v1"
        assert command.expires_at - command.issued_at == timedelta(minutes=10)
        assert command.payload == {
            "confirmation_token": token,
            "reset_generation": 1,
            "server_sequence_floor": 42,
        }
        command.state = "succeeded"
        command.progress_percent = 100
        command.last_result = {
            "result_code": "PREPARED",
            "evidence": {
                "prepare_command_id": prepare_id,
                "reset_generation": 1,
                "server_sequence_floor": 42,
                "sequence_floor": 47,
                "ready": True,
            },
        }
        await session.commit()

    wrong_phrase = await owner_client.post(
        f"/api/v1/devices/{device.id}/commands",
        json=_request(
            "data_reset_commit",
            "reset-commit-wrong-phrase",
            prepare_id=prepare_id,
            token=token,
            phrase="clear readings",
        ),
    )
    assert wrong_phrase.status_code == 422

    committed = await owner_client.post(
        f"/api/v1/devices/{device.id}/commands",
        json=_request(
            "data_reset_commit",
            "reset-commit-0001",
            prepare_id=prepare_id,
            token=token,
            phrase="CLEAR READINGS",
        ),
    )
    assert committed.status_code == 202, committed.text
    commit_id = committed.json()["command"]["id"]

    exact_retry = await owner_client.post(
        f"/api/v1/devices/{device.id}/commands",
        json=_request(
            "data_reset_commit",
            "reset-commit-0001",
            prepare_id=prepare_id,
            token=token,
            phrase="CLEAR READINGS",
        ),
    )
    assert exact_retry.status_code == 202, exact_retry.text
    assert exact_retry.json()["command"]["id"] == commit_id

    replay = await owner_client.post(
        f"/api/v1/devices/{device.id}/commands",
        json=_request(
            "data_reset_commit",
            "reset-commit-replay-0002",
            prepare_id=prepare_id,
            token=token,
            phrase="CLEAR READINGS",
        ),
    )
    assert replay.status_code == 409

    async with session_factory() as session:
        prepare = await session.get(DeviceCommand, prepare_id)
        commit = await session.get(DeviceCommand, commit_id)
        assert prepare is not None and prepare.prepare_token_hash is None
        assert prepare.payload["confirmation_token"] == "[consumed]"
        assert commit is not None
        assert commit.required_firmware_capability == "destructive_commands_v1"
        assert commit.payload == {
            "prepare_command_id": prepare_id,
            "confirmation_token": token,
            "reset_generation": 1,
            "sequence_floor": 47,
        }

    async with session_factory() as session:
        with pytest.raises(IntegrityConflict, match="commit completion evidence"):
            await apply_command_results(
                session,
                device.id,
                [
                    CommandResult(
                        command_id=commit_id,
                        state="succeeded",
                        progress_percent=100,
                        result_code="RESET_COMPLETE",
                        evidence={
                            "prepare_command_id": prepare_id,
                            "reset_generation": 1,
                            "sequence_floor": 46,
                        },
                    )
                ],
            )
        await session.rollback()

    async with session_factory() as session:
        await apply_command_results(
            session,
            device.id,
            [
                CommandResult(
                    command_id=commit_id,
                    state="succeeded",
                    progress_percent=100,
                    result_code="RESET_COMPLETE",
                    evidence={
                        "prepare_command_id": prepare_id,
                        "reset_generation": 1,
                        "sequence_floor": 47,
                    },
                )
            ],
        )
        await session.commit()
    async with session_factory() as session:
        updated = await session.get(Device, device.id)
        assert updated is not None
        assert (updated.reset_generation, updated.maximum_sequence, updated.contiguous_ack) == (
            1,
            47,
            47,
        )


@pytest.mark.asyncio
async def test_prepare_type_expiry_and_payload_are_fail_closed(
    owner_client: AsyncClient,
) -> None:
    device = await _device()
    unexpected = await owner_client.post(
        f"/api/v1/devices/{device.id}/commands",
        json={
            **_request("format_storage_prepare", "format-payload-reject"),
            "payload": {"confirmation_token": "caller-controlled"},
        },
    )
    assert unexpected.status_code == 422

    prepared = await owner_client.post(
        f"/api/v1/devices/{device.id}/commands",
        json=_request("format_storage_prepare", "format-prepare-expiry"),
    )
    assert prepared.status_code == 202, prepared.text
    prepare_id = prepared.json()["command"]["id"]
    token = prepared.json()["confirmation_token"]
    async with session_factory() as session:
        command = await session.get(DeviceCommand, prepare_id)
        assert command is not None
        assert command.payload == {"confirmation_token": token}
        command.state = "succeeded"
        command.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    expired_commit = await owner_client.post(
        f"/api/v1/devices/{device.id}/commands",
        json=_request(
            "format_storage_commit",
            "format-commit-expired",
            prepare_id=prepare_id,
            token=token,
            phrase="FORMAT STORAGE",
        ),
    )
    assert expired_commit.status_code == 409

    async with session_factory() as session:
        assert await expire_prepare_tokens(session) == 1
        await session.commit()
    async with session_factory() as session:
        command = await session.get(DeviceCommand, prepare_id)
        assert command is not None
        assert command.prepare_token_hash is None
        assert command.payload["confirmation_token"] == "[expired]"

    reset = await owner_client.post(
        f"/api/v1/devices/{device.id}/commands",
        json=_request("data_reset_prepare", "reset-prepare-cancel"),
    )
    assert reset.status_code == 202, reset.text
    reset_id = reset.json()["command"]["id"]
    cancelled = await owner_client.post(
        f"/api/v1/devices/{device.id}/commands",
        json={
            **_request("data_reset_cancel", "reset-cancel-0001"),
            "payload": {"prepare_command_id": reset_id},
        },
    )
    assert cancelled.status_code == 202, cancelled.text
    cancel_id = cancelled.json()["command"]["id"]
    retry_cancel = await owner_client.post(
        f"/api/v1/devices/{device.id}/commands",
        json={
            **_request("data_reset_cancel", "reset-cancel-0001"),
            "payload": {"prepare_command_id": reset_id},
        },
    )
    assert retry_cancel.status_code == 202, retry_cancel.text
    assert retry_cancel.json()["command"]["id"] == cancel_id
    async with session_factory() as session:
        cancelled_prepare = await session.get(DeviceCommand, reset_id)
        cancel_command = await session.get(DeviceCommand, cancel_id)
        assert cancelled_prepare is not None
        assert cancelled_prepare.state == "cancelled"
        assert cancelled_prepare.prepare_token_hash is None
        assert cancelled_prepare.payload["confirmation_token"] == "[cancelled]"
        assert cancel_command is not None
        assert cancel_command.payload == {"prepare_command_id": reset_id}


@pytest.mark.asyncio
async def test_old_generation_is_retained_as_evidence_but_hidden_from_history(
    owner_client: AsyncClient,
) -> None:
    device = await _device()
    end = datetime.now(UTC) - timedelta(minutes=2)
    start = end - timedelta(minutes=1)
    async with session_factory() as session:
        raw = RawReading(
            device_id=device.id,
            sequence=42,
            reset_generation=0,
            interval_start_utc=start,
            interval_end_utc=end,
            monotonic_start_us=1_000_000,
            monotonic_end_us=61_000_000,
            sample_count=60,
            expected_sample_count=60,
            voltage_mv=120_000,
            current_ma=1_000,
            active_power_mw=120_000,
            frequency_mhz=60_000,
            power_factor_milli=1_000,
            pzem_energy_wh=10,
            interval_energy_mwh=2_000,
            energy_selection="pzem_register_delta",
            pzem_status="ok",
            time_trusted=True,
            flags=[],
            record_crc32=1,
            payload_sha256="a" * 64,
        )
        session.add(raw)
        await session.flush()
        session.add(
            NormalizedInterval(
                device_id=device.id,
                raw_reading_id=raw.id,
                start_utc=start,
                end_utc=end,
                energy_mwh=2_000,
                average_power_mw=120_000,
                completeness=1,
                energy_selection="pzem_register_delta",
                algorithm_version="normalize-v1",
                source_authenticated=True,
            )
        )
        await session.commit()

    params = {
        "from": (start - timedelta(minutes=1)).isoformat(),
        "to": (end + timedelta(minutes=1)).isoformat(),
        "metric": "energy",
        "device_id": device.id,
        "resolution_seconds": "60",
    }
    before = await owner_client.get("/api/v1/history", params=params)
    assert before.status_code == 200, before.text
    assert [point["value"] for point in before.json()["points"] if point["value"] is not None] == [
        "0.002"
    ]

    async with session_factory() as session:
        row = await session.get(Device, device.id)
        assert row is not None
        row.reset_generation = 1
        await session.commit()

    after = await owner_client.get("/api/v1/history", params=params)
    assert after.status_code == 200, after.text
    assert after.json()["points"]
    assert all(point["value"] is None for point in after.json()["points"])
    exported = await owner_client.get(
        "/api/v1/history/export.csv",
        params={"from": params["from"], "to": params["to"]},
    )
    assert exported.status_code == 200, exported.text
    assert exported.text.splitlines() == [
        "device_id,sequence,start_utc,end_utc,energy_kwh,completeness,source"
    ]
    async with session_factory() as session:
        assert len((await session.scalars(select(RawReading))).all()) == 1
