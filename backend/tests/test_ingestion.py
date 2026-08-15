from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from backend.app.errors import IntegrityConflict
from backend.app.main import session_factory
from backend.app.models import (
    Device,
    Home,
    NormalizedInterval,
    RawReading,
    UnavailableSequenceRange,
)
from backend.app.schemas.device import (
    DurableReading,
    PermanentLossRange,
    PermanentLossRequest,
    ReadingBatchRequest,
)
from backend.app.services.ingestion import ingest_batch, record_permanent_loss
from pydantic import ValidationError
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


def test_reading_rejects_completeness_above_one() -> None:
    payload = record(1).model_dump()
    payload["sample_count"] = 61
    payload["expected_sample_count"] = 60

    with pytest.raises(ValidationError, match="sample count cannot exceed"):
        DurableReading.model_validate(payload)


def test_permanent_loss_request_rejects_overlapping_ranges() -> None:
    with pytest.raises(ValidationError, match="must not overlap"):
        PermanentLossRequest(
            protocol_id="pm-protocol/1.0.0",
            ranges=[
                PermanentLossRange(
                    first_sequence=4,
                    last_sequence=8,
                    reason_code="storage_failure",
                    evidence_sha256="a" * 64,
                ),
                PermanentLossRange(
                    first_sequence=1,
                    last_sequence=4,
                    reason_code="record_crc",
                    evidence_sha256="b" * 64,
                ),
            ],
        )


@pytest.mark.asyncio
async def test_committed_permanent_loss_rejects_later_reading() -> None:
    device_id = await make_device()
    loss = PermanentLossRange(
        first_sequence=1,
        last_sequence=2,
        reason_code="storage_failure",
        evidence_sha256="a" * 64,
    )
    async with session_factory() as session:
        result = await record_permanent_loss(session, device_id, [loss])
        await session.commit()
        assert result.accepted == 1
        assert result.highest_contiguous_sequence == 2

    async with session_factory() as session:
        with pytest.raises(IntegrityConflict, match="permanent-loss evidence"):
            await ingest_batch(
                session,
                device_id,
                ReadingBatchRequest(protocol_id="pm-protocol/1.0.0", records=[record(1)]),
            )
        await session.rollback()
        assert await session.scalar(select(func.count(RawReading.id))) == 0
        assert await session.scalar(select(func.count(UnavailableSequenceRange.id))) == 1


@pytest.mark.asyncio
async def test_permanent_loss_is_idempotent_but_rejects_any_overlap() -> None:
    device_id = await make_device()
    loss = PermanentLossRange(
        first_sequence=2,
        last_sequence=4,
        reason_code="segment_corrupt",
        evidence_sha256="b" * 64,
    )
    async with session_factory() as session:
        first = await record_permanent_loss(session, device_id, [loss])
        await session.commit()
        assert first.accepted == 1

    async with session_factory() as session:
        retry = await record_permanent_loss(session, device_id, [loss])
        await session.commit()
        assert retry.accepted == 0

    overlapping = PermanentLossRange(
        first_sequence=4,
        last_sequence=6,
        reason_code="segment_corrupt",
        evidence_sha256="b" * 64,
    )
    async with session_factory() as session:
        with pytest.raises(IntegrityConflict, match="overlaps prior evidence"):
            await record_permanent_loss(session, device_id, [overlapping])


@pytest.mark.asyncio
async def test_permanent_loss_evidence_is_immutable_and_acknowledgement_stays_monotonic() -> None:
    device_id = await make_device()
    loss = PermanentLossRange(
        first_sequence=1,
        last_sequence=2,
        reason_code="storage_failure",
        evidence_sha256="d" * 64,
    )
    async with session_factory() as session:
        result = await record_permanent_loss(session, device_id, [loss])
        await session.commit()
        assert result.highest_contiguous_sequence == 2

    async with session_factory() as session:
        stored = await session.scalar(
            select(UnavailableSequenceRange).where(UnavailableSequenceRange.device_id == device_id)
        )
        assert stored is not None
        stored.reason_code = "record_crc"
        with pytest.raises(ValueError, match="UnavailableSequenceRange records are immutable"):
            await session.flush()
        await session.rollback()

    async with session_factory() as session:
        stored = await session.scalar(
            select(UnavailableSequenceRange).where(UnavailableSequenceRange.device_id == device_id)
        )
        assert stored is not None
        await session.delete(stored)
        with pytest.raises(ValueError, match="UnavailableSequenceRange records are immutable"):
            await session.flush()
        await session.rollback()

    async with session_factory() as session:
        stored = await session.scalar(
            select(UnavailableSequenceRange).where(UnavailableSequenceRange.device_id == device_id)
        )
        device = await session.get(Device, device_id)
        assert stored is not None
        assert stored.reason_code == "storage_failure"
        assert stored.first_sequence == 1
        assert stored.last_sequence == 2
        assert device is not None
        assert device.contiguous_ack == 2
        assert device.maximum_sequence == 2


@pytest.mark.asyncio
async def test_permanent_loss_rejects_an_already_committed_reading() -> None:
    device_id = await make_device()
    async with session_factory() as session:
        await ingest_batch(
            session,
            device_id,
            ReadingBatchRequest(protocol_id="pm-protocol/1.0.0", records=[record(3)]),
        )
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(IntegrityConflict, match="overlaps a committed reading"):
            await record_permanent_loss(
                session,
                device_id,
                [
                    PermanentLossRange(
                        first_sequence=2,
                        last_sequence=4,
                        reason_code="storage_failure",
                        evidence_sha256="c" * 64,
                    )
                ],
            )
