from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import orjson
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import MAX_FUTURE_TIME_SECONDS
from ..errors import IntegrityConflict, NotFound
from ..models import (
    BillingEstimateSelection,
    Device,
    IntervalCostSelection,
    NormalizedInterval,
    RawReading,
    UnavailableSequenceRange,
    UtilityAccount,
)
from ..schemas.device import DurableReading, PermanentLossRange, ReadingBatchRequest


def reading_payload_hash(record: DurableReading) -> str:
    data = orjson.dumps(record.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class IngestionResult:
    accepted: int
    identical_retries: int
    highest_contiguous_sequence: int
    gaps: tuple[tuple[int, int], ...]


async def _get_device_for_update(session: AsyncSession, device_id: str) -> Device:
    device = await session.scalar(
        select(Device).where(Device.id == device_id, Device.revoked_at.is_(None)).with_for_update()
    )
    if device is None:
        raise NotFound("active device does not exist")
    return device


async def ingest_batch(
    session: AsyncSession,
    device_id: str,
    batch: ReadingBatchRequest,
    *,
    now: datetime | None = None,
) -> IngestionResult:
    device = await _get_device_for_update(session, device_id)
    accepted_now = (now or datetime.now(UTC)).astimezone(UTC)
    sequences = [record.sequence for record in batch.records]
    existing_rows = (
        await session.scalars(
            select(RawReading).where(
                RawReading.device_id == device_id, RawReading.sequence.in_(sequences)
            )
        )
    ).all()
    existing = {row.sequence: row for row in existing_rows}
    loss_ranges = (
        await session.scalars(
            select(UnavailableSequenceRange).where(
                UnavailableSequenceRange.device_id == device_id,
                UnavailableSequenceRange.first_sequence <= max(sequences),
                UnavailableSequenceRange.last_sequence >= min(sequences),
            )
        )
    ).all()
    accepted = 0
    identical = 0
    earliest_new_interval_start: datetime | None = None

    for record in batch.records:
        if record.interval_end_utc is not None and record.interval_end_utc.astimezone(
            UTC
        ) > accepted_now + timedelta(seconds=MAX_FUTURE_TIME_SECONDS):
            raise IntegrityConflict("reading timestamp is unacceptably far in the future")
        payload_hash = reading_payload_hash(record)
        prior = existing.get(record.sequence)
        if prior is not None:
            if prior.payload_sha256 != payload_hash:
                raise IntegrityConflict(
                    f"sequence {record.sequence} was previously committed with different content"
                )
            identical += 1
            continue
        if record.reset_generation != device.reset_generation:
            raise IntegrityConflict(
                f"sequence {record.sequence} belongs to reset generation "
                f"{record.reset_generation}; expected {device.reset_generation}"
            )
        if any(
            loss.first_sequence <= record.sequence <= loss.last_sequence for loss in loss_ranges
        ):
            raise IntegrityConflict(
                f"sequence {record.sequence} is covered by authenticated permanent-loss evidence"
            )
        row = RawReading(
            device_id=device_id,
            sequence=record.sequence,
            reset_generation=record.reset_generation,
            interval_start_utc=record.interval_start_utc,
            interval_end_utc=record.interval_end_utc,
            monotonic_start_us=record.monotonic_start_us,
            monotonic_end_us=record.monotonic_end_us,
            sample_count=record.sample_count,
            expected_sample_count=record.expected_sample_count,
            voltage_mv=record.voltage_mv,
            current_ma=record.current_ma,
            active_power_mw=record.active_power_mw,
            frequency_mhz=record.frequency_mhz,
            power_factor_milli=record.power_factor_milli,
            pzem_energy_wh=record.pzem_energy_wh,
            interval_energy_mwh=record.interval_energy_mwh,
            energy_selection=record.energy_selection,
            pzem_status=record.pzem_status,
            time_trusted=record.time_trusted,
            flags=record.flags,
            record_crc32=record.record_crc32,
            payload_sha256=payload_hash,
        )
        session.add(row)
        await session.flush()
        accepted += 1

        if (
            record.time_trusted
            and record.interval_energy_mwh is not None
            and record.interval_start_utc is not None
            and record.interval_end_utc is not None
            and not record.energy_selection.startswith("unavailable")
        ):
            duration = record.interval_end_utc - record.interval_start_utc
            duration_us = (
                (duration.days * 86_400) + duration.seconds
            ) * 1_000_000 + duration.microseconds
            average_power_mw = (
                record.interval_energy_mwh * 3_600_000_000 // duration_us
                if duration_us > 0
                else None
            )
            session.add(
                NormalizedInterval(
                    device_id=device_id,
                    raw_reading_id=row.id,
                    start_utc=record.interval_start_utc,
                    end_utc=record.interval_end_utc,
                    energy_mwh=record.interval_energy_mwh,
                    average_power_mw=average_power_mw,
                    completeness=Decimal(record.sample_count)
                    / Decimal(record.expected_sample_count),
                    energy_selection=record.energy_selection,
                    algorithm_version="normalize-v1",
                    source_authenticated=True,
                )
            )
            normalized_start = record.interval_start_utc.astimezone(UTC)
            if (
                earliest_new_interval_start is None
                or normalized_start < earliest_new_interval_start
            ):
                earliest_new_interval_start = normalized_start

    if sequences:
        device.maximum_sequence = max(device.maximum_sequence, max(sequences))
    await session.flush()
    if earliest_new_interval_start is not None:
        # Backlog records can arrive after newer intervals have already been
        # priced. Remove only the mutable selections at and after the newly
        # inserted point so the worker rebuilds tier progression chronologically.
        # Immutable cost runs, cost rows, readings, and rate versions remain.
        affected_intervals = (
            select(NormalizedInterval.id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(
                Device.home_id == device.home_id,
                NormalizedInterval.start_utc >= earliest_new_interval_start,
            )
        )
        await session.execute(
            delete(IntervalCostSelection)
            .where(IntervalCostSelection.normalized_interval_id.in_(affected_intervals))
            .execution_options(synchronize_session=False)
        )
        account_ids = select(UtilityAccount.id).where(UtilityAccount.home_id == device.home_id)
        await session.execute(
            delete(BillingEstimateSelection)
            .where(BillingEstimateSelection.utility_account_id.in_(account_ids))
            .execution_options(synchronize_session=False)
        )
    acknowledgement = await advance_contiguous_cursor(session, device)
    return IngestionResult(
        accepted, identical, acknowledgement, tuple(await find_gaps(session, device))
    )


async def record_permanent_loss(
    session: AsyncSession, device_id: str, ranges: list[PermanentLossRange]
) -> IngestionResult:
    device = await _get_device_for_update(session, device_id)
    accepted = 0
    for item in ranges:
        conflict = await session.scalar(
            select(RawReading.sequence).where(
                RawReading.device_id == device_id,
                RawReading.sequence.between(item.first_sequence, item.last_sequence),
            )
        )
        if conflict is not None:
            raise IntegrityConflict("permanent-loss range overlaps a committed reading")
        prior = await session.scalar(
            select(UnavailableSequenceRange).where(
                UnavailableSequenceRange.device_id == device_id,
                UnavailableSequenceRange.first_sequence == item.first_sequence,
                UnavailableSequenceRange.last_sequence == item.last_sequence,
            )
        )
        if prior is not None:
            if (
                prior.evidence_sha256 != item.evidence_sha256
                or prior.reason_code != item.reason_code
            ):
                raise IntegrityConflict("permanent-loss range conflicts with prior evidence")
            continue
        overlap = await session.scalar(
            select(UnavailableSequenceRange.id).where(
                UnavailableSequenceRange.device_id == device_id,
                UnavailableSequenceRange.first_sequence <= item.last_sequence,
                UnavailableSequenceRange.last_sequence >= item.first_sequence,
            )
        )
        if overlap is not None:
            raise IntegrityConflict("permanent-loss range overlaps prior evidence")
        session.add(
            UnavailableSequenceRange(
                device_id=device_id,
                first_sequence=item.first_sequence,
                last_sequence=item.last_sequence,
                reason_code=item.reason_code,
                evidence_sha256=item.evidence_sha256,
            )
        )
        device.maximum_sequence = max(device.maximum_sequence, item.last_sequence)
        accepted += 1
    await session.flush()
    acknowledgement = await advance_contiguous_cursor(session, device)
    return IngestionResult(accepted, 0, acknowledgement, tuple(await find_gaps(session, device)))


async def _coverage(session: AsyncSession, device_id: str, after: int) -> list[tuple[int, int]]:
    readings = (
        await session.scalars(
            select(RawReading.sequence)
            .where(RawReading.device_id == device_id, RawReading.sequence > after)
            .order_by(RawReading.sequence)
        )
    ).all()
    losses = (
        await session.scalars(
            select(UnavailableSequenceRange)
            .where(
                UnavailableSequenceRange.device_id == device_id,
                UnavailableSequenceRange.last_sequence > after,
            )
            .order_by(UnavailableSequenceRange.first_sequence)
        )
    ).all()
    ranges = [(sequence, sequence) for sequence in readings]
    ranges.extend((row.first_sequence, row.last_sequence) for row in losses)
    return sorted(ranges)


async def advance_contiguous_cursor(session: AsyncSession, device: Device) -> int:
    cursor = device.contiguous_ack
    for start, end in await _coverage(session, device.id, cursor):
        if end <= cursor:
            continue
        if start > cursor + 1:
            break
        cursor = max(cursor, end)
    if cursor < device.contiguous_ack:
        raise IntegrityConflict("acknowledgement regression is forbidden")
    device.contiguous_ack = cursor
    return cursor


async def find_gaps(
    session: AsyncSession, device: Device, limit: int = 100
) -> list[tuple[int, int]]:
    cursor = device.contiguous_ack
    if cursor >= device.maximum_sequence:
        return []
    ranges = await _coverage(session, device.id, cursor)
    gaps: list[tuple[int, int]] = []
    expected = cursor + 1
    for start, end in ranges:
        if start > expected:
            gaps.append((expected, start - 1))
            if len(gaps) >= limit:
                return gaps
        expected = max(expected, end + 1)
    if expected <= device.maximum_sequence and len(gaps) < limit:
        gaps.append((expected, device.maximum_sequence))
    return gaps
