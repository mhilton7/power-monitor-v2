from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from backend.app.errors import IntegrityConflict
from backend.app.main import session_factory
from backend.app.models import Device, Home, NormalizedInterval, RawReading
from backend.app.schemas.device import DurableReading, ReadingBatchRequest
from backend.app.services.ingestion import ingest_batch
from sqlalchemy import func, select


def record(sequence: int, *, trusted: bool = True, energy_mwh: int | None = 1000) -> DurableReading:
    start = datetime(2026, 8, 13, 12, sequence % 60, tzinfo=UTC) if trusted else None
    return DurableReading(
        sequence=sequence,
        reset_generation=0,
        interval_start_utc=start,
        interval_end_utc=start + timedelta(minutes=1) if start else None,
        monotonic_start_us=sequence * 60_000_000,
        monotonic_end_us=(sequence + 1) * 60_000_000,
        sample_count=60,
        expected_sample_count=60,
        voltage_mv=120_000,
        current_ma=1_000,
        active_power_mw=120_000,
        frequency_mhz=60_000,
        power_factor_milli=995,
        pzem_energy_wh=sequence,
        interval_energy_mwh=energy_mwh,
        energy_selection="pzem_delta" if energy_mwh is not None else "unavailable_invalid",
        pzem_status="ok",
        time_trusted=trusted,
        flags=[],
        record_crc32=123,
    )


async def make_device() -> str:
    async with session_factory() as session:
        home = Home(name="Ingestion Home")
        session.add(home)
        await session.flush()
        device = Device(
            home_id=home.id,
            friendly_name="Main panel",
            pzem_variant="pzem004t-v4-classic-candidate",
            ct_rating_a=100,
        )
        session.add(device)
        await session.commit()
        return device.id


@pytest.mark.asyncio
async def test_immutable_ingestion_dedupes_and_advances_only_contiguous() -> None:
    device_id = await make_device()
    async with session_factory() as session:
        result = await ingest_batch(
            session,
            device_id,
            ReadingBatchRequest(protocol_id="pm-protocol/1.0.0", records=[record(1), record(3)]),
        )
        await session.commit()
        assert result.accepted == 2
        assert result.highest_contiguous_sequence == 1
        assert result.gaps == ((2, 2),)
    async with session_factory() as session:
        retry = await ingest_batch(
            session,
            device_id,
            ReadingBatchRequest(protocol_id="pm-protocol/1.0.0", records=[record(1), record(2)]),
        )
        await session.commit()
        assert retry.identical_retries == 1
        assert retry.highest_contiguous_sequence == 3
        assert not retry.gaps
        assert await session.scalar(select(func.count(RawReading.id))) == 3
        assert await session.scalar(select(func.count(NormalizedInterval.id))) == 3


@pytest.mark.asyncio
async def test_conflicting_sequence_is_rejected() -> None:
    device_id = await make_device()
    async with session_factory() as session:
        await ingest_batch(
            session,
            device_id,
            ReadingBatchRequest(protocol_id="pm-protocol/1.0.0", records=[record(1)]),
        )
        await session.commit()
    changed = record(1).model_copy(update={"active_power_mw": 121_000})
    async with session_factory() as session:
        with pytest.raises(IntegrityConflict):
            await ingest_batch(
                session,
                device_id,
                ReadingBatchRequest(protocol_id="pm-protocol/1.0.0", records=[changed]),
            )


@pytest.mark.asyncio
async def test_untrusted_time_never_becomes_history() -> None:
    device_id = await make_device()
    async with session_factory() as session:
        await ingest_batch(
            session,
            device_id,
            ReadingBatchRequest(
                protocol_id="pm-protocol/1.0.0", records=[record(1, trusted=False)]
            ),
        )
        await session.commit()
        assert await session.scalar(select(func.count(RawReading.id))) == 1
        assert await session.scalar(select(func.count(NormalizedInterval.id))) == 0
