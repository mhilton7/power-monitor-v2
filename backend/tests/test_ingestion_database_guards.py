from __future__ import annotations

import asyncio
import runpy
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from backend.app.main import engine, session_factory
from backend.app.models import Device, Home, RawReading, UnavailableSequenceRange
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError


def _require_postgres() -> None:
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL trigger contract requires the CI PostgreSQL database")


async def _device(session) -> Device:  # type: ignore[no-untyped-def]
    home = Home(name="Database ingestion guard")
    session.add(home)
    await session.flush()
    device = Device(
        home_id=home.id,
        friendly_name="Guarded sensor",
        pzem_variant="pzem004t-v4-classic-candidate",
        ct_rating_a=100,
    )
    session.add(device)
    await session.flush()
    return device


def _raw_values(device_id: str, sequence: int, *, samples: int = 60) -> dict[str, object]:
    return {
        "id": str(uuid.uuid4()),
        "device_id": device_id,
        "sequence": sequence,
        "reset_generation": 0,
        "interval_start_utc": None,
        "interval_end_utc": None,
        "monotonic_start_us": sequence * 60_000_000,
        "monotonic_end_us": (sequence + 1) * 60_000_000,
        "sample_count": samples,
        "expected_sample_count": 60,
        "voltage_mv": None,
        "current_ma": None,
        "active_power_mw": None,
        "frequency_mhz": None,
        "power_factor_milli": None,
        "pzem_energy_wh": None,
        "interval_energy_mwh": None,
        "energy_selection": "unavailable_invalid",
        "pzem_status": "timeout",
        "time_trusted": False,
        "flags": [],
        "record_crc32": sequence,
        "payload_sha256": f"{sequence:x}".zfill(64),
        "received_at": datetime.now(UTC),
    }


def _loss_values(device_id: str, first: int, last: int) -> dict[str, object]:
    return {
        "id": str(uuid.uuid4()),
        "device_id": device_id,
        "first_sequence": first,
        "last_sequence": last,
        "reason_code": "storage_failure",
        "evidence_sha256": f"{first:x}{last:x}".zfill(64),
        "authenticated_at": datetime.now(UTC),
    }


def _database_error(exc: DBAPIError) -> BaseException:
    if exc.orig is None or exc.orig.__cause__ is None:
        raise AssertionError("expected a wrapped database-driver diagnostic")
    return exc.orig.__cause__


def _assert_named_check(exc: pytest.ExceptionInfo[IntegrityError], name: str) -> None:
    database_error = _database_error(exc.value)
    assert getattr(database_error, "sqlstate", None) == "23514"
    assert getattr(database_error, "constraint_name", None) == name


class _PreflightConnection:
    def __init__(self, results: Sequence[object | None]) -> None:
        self.results = iter(results)
        self.statements: list[str] = []

    def scalar(self, statement: object) -> object | None:
        self.statements.append(str(statement))
        return next(self.results)


def _run_migration_preflight(
    monkeypatch: pytest.MonkeyPatch, results: list[int | None]
) -> _PreflightConnection:
    migration = runpy.run_path(
        str(
            Path(__file__).parents[1]
            / "alembic/versions/20260815_0008_ingestion_evidence_integrity.py"
        )
    )
    check = migration["_assert_no_existing_postgres_ingestion_conflicts"]
    connection = _PreflightConnection(results)
    monkeypatch.setattr(check.__globals__["op"], "get_bind", lambda: connection)
    check()
    return connection


def _run_bill_artifact_preflight(
    monkeypatch: pytest.MonkeyPatch, result: int | None
) -> _PreflightConnection:
    migration = runpy.run_path(
        str(
            Path(__file__).parents[1]
            / "alembic/versions/20260815_0008_ingestion_evidence_integrity.py"
        )
    )
    check = migration["_assert_no_retained_original_bill_artifacts"]
    connection = _PreflightConnection([result])
    monkeypatch.setattr(check.__globals__["op"], "get_bind", lambda: connection)
    check()
    return connection


def _run_raw_immutability_preflight(
    monkeypatch: pytest.MonkeyPatch, definition: str | None
) -> _PreflightConnection:
    migration = runpy.run_path(
        str(
            Path(__file__).parents[1]
            / "alembic/versions/20260815_0010_permanent_loss_evidence_immutability.py"
        )
    )
    check = migration["_assert_postgres_raw_immutability_guard"]
    connection = _PreflightConnection([definition])
    monkeypatch.setattr(check.__globals__["op"], "get_bind", lambda: connection)
    check()
    return connection


def test_migration_preflight_rejects_existing_ingestion_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean = _run_migration_preflight(monkeypatch, [None, None])
    assert "JOIN public.unavailable_sequence_ranges AS loss" in clean.statements[0]
    assert "later.id > earlier.id" in clean.statements[1]

    with pytest.raises(RuntimeError, match="raw readings overlap"):
        _run_migration_preflight(monkeypatch, [1])
    with pytest.raises(RuntimeError, match="evidence ranges overlap"):
        _run_migration_preflight(monkeypatch, [None, 1])

    migration_source = (
        Path(__file__).parents[1] / "alembic/versions/20260815_0008_ingestion_evidence_integrity.py"
    ).read_text(encoding="utf-8")
    assert "public.utility_bill_rate_uploads" in migration_source
    upgrade_source = migration_source[migration_source.index("def upgrade()") :]
    assert upgrade_source.index("LOCK TABLE public.raw_readings") < upgrade_source.index(
        "_assert_no_existing_postgres_ingestion_conflicts()"
    )
    assert upgrade_source.index("LOCK TABLE public.raw_readings") < upgrade_source.index(
        "_assert_no_retained_original_bill_artifacts()"
    )


def test_migration_preflight_rejects_retained_original_bill_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean = _run_bill_artifact_preflight(monkeypatch, None)
    assert "encrypted_artifact_path IS NOT NULL" in clean.statements[0]

    with pytest.raises(RuntimeError, match="retained original bill documents"):
        _run_bill_artifact_preflight(monkeypatch, 1)


def test_loss_immutability_migration_requires_the_existing_raw_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_definition = (
        "CREATE TRIGGER raw_readings_immutable BEFORE DELETE OR UPDATE "
        "ON public.raw_readings FOR EACH ROW "
        "EXECUTE FUNCTION pm_reject_immutable_change()"
    )
    clean = _run_raw_immutability_preflight(monkeypatch, valid_definition)
    assert "tgenabled = 'O'" in clean.statements[0]

    for invalid_definition in (
        None,
        "CREATE TRIGGER raw_readings_immutable BEFORE DELETE ON public.raw_readings",
        valid_definition.replace("pm_reject_immutable_change", "unexpected_function"),
    ):
        with pytest.raises(RuntimeError, match="raw-reading UPDATE/DELETE immutability"):
            _run_raw_immutability_preflight(monkeypatch, invalid_definition)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migrated_ingestion_guards_are_installed_at_head() -> None:
    _require_postgres()
    async with engine.connect() as connection:
        sample_check = await connection.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_catalog.pg_constraint "
                "WHERE conrelid = 'public.raw_readings'::regclass "
                "AND conname = 'ck_raw_readings_sample_count'"
            )
        )
        triggers = set(
            (
                await connection.scalars(
                    text(
                        "SELECT tgname FROM pg_catalog.pg_trigger "
                        "WHERE tgrelid IN ("
                        "'public.raw_readings'::regclass, "
                        "'public.unavailable_sequence_ranges'::regclass"
                        ") AND NOT tgisinternal"
                    )
                )
            ).all()
        )
        loss_guard = await connection.scalar(
            text(
                "SELECT pg_get_functiondef("
                "'public.pm_guard_permanent_loss_overlap()'::regprocedure)"
            )
        )
        immutability_trigger = await connection.scalar(
            text(
                "SELECT pg_get_triggerdef(oid) FROM pg_catalog.pg_trigger "
                "WHERE tgrelid = 'public.unavailable_sequence_ranges'::regclass "
                "AND tgname = 'unavailable_sequence_ranges_immutable' "
                "AND NOT tgisinternal"
            )
        )
        raw_overlap_trigger = await connection.scalar(
            text(
                "SELECT pg_get_triggerdef(oid) FROM pg_catalog.pg_trigger "
                "WHERE tgrelid = 'public.raw_readings'::regclass "
                "AND tgname = 'raw_readings_loss_overlap_guard' "
                "AND NOT tgisinternal"
            )
        )
        immutability_guard = await connection.scalar(
            text(
                "SELECT pg_get_functiondef("
                "'public.pm_reject_permanent_loss_evidence_change()'::regprocedure)"
            )
        )

    assert sample_check is not None
    normalized_check = " ".join(str(sample_check).lower().split())
    assert "sample_count <= expected_sample_count" in normalized_check
    assert {
        "raw_readings_loss_overlap_guard",
        "unavailable_sequence_ranges_overlap_guard",
        "unavailable_sequence_ranges_immutable",
    }.issubset(triggers)
    assert loss_guard is not None
    assert "prior.id is distinct from new.id" in " ".join(str(loss_guard).lower().split())
    assert immutability_trigger is not None
    normalized_trigger = " ".join(str(immutability_trigger).lower().split())
    assert "before delete or update on" in normalized_trigger
    assert "pm_reject_permanent_loss_evidence_change" in normalized_trigger
    assert immutability_guard is not None
    normalized_guard = " ".join(str(immutability_guard).lower().split())
    assert "authenticated permanent-loss evidence is immutable" in normalized_guard
    assert "ck_unavailable_sequence_ranges_immutable" in normalized_guard
    assert "23514" in normalized_guard
    assert raw_overlap_trigger is not None
    normalized_raw_overlap_trigger = " ".join(str(raw_overlap_trigger).lower().split())
    assert "before insert on" in normalized_raw_overlap_trigger
    assert "before insert or update" not in normalized_raw_overlap_trigger


@pytest.mark.integration
@pytest.mark.asyncio
async def test_direct_sql_cannot_bypass_ingestion_evidence_guards() -> None:
    _require_postgres()
    async with session_factory() as session:
        device = await _device(session)

        with pytest.raises(IntegrityError) as sample_error:
            async with session.begin_nested():
                await session.execute(
                    insert(RawReading).values(_raw_values(device.id, 1, samples=61))
                )
        _assert_named_check(sample_error, "ck_raw_readings_sample_count")

        await session.execute(
            insert(UnavailableSequenceRange).values(_loss_values(device.id, 10, 12))
        )
        immutable_loss = _loss_values(device.id, 50, 52)
        await session.execute(insert(UnavailableSequenceRange).values(immutable_loss))
        with pytest.raises(IntegrityError) as loss_update:
            async with session.begin_nested():
                await session.execute(
                    update(UnavailableSequenceRange)
                    .where(UnavailableSequenceRange.id == immutable_loss["id"])
                    .values(reason_code="record_crc")
                )
        _assert_named_check(loss_update, "ck_unavailable_sequence_ranges_immutable")

        with pytest.raises(IntegrityError) as loss_delete:
            async with session.begin_nested():
                await session.execute(
                    delete(UnavailableSequenceRange).where(
                        UnavailableSequenceRange.id == immutable_loss["id"]
                    )
                )
        _assert_named_check(loss_delete, "ck_unavailable_sequence_ranges_immutable")

        preserved_loss = (
            await session.execute(
                select(
                    UnavailableSequenceRange.first_sequence,
                    UnavailableSequenceRange.last_sequence,
                    UnavailableSequenceRange.reason_code,
                    UnavailableSequenceRange.evidence_sha256,
                ).where(UnavailableSequenceRange.id == immutable_loss["id"])
            )
        ).one()
        assert preserved_loss == (
            immutable_loss["first_sequence"],
            immutable_loss["last_sequence"],
            immutable_loss["reason_code"],
            immutable_loss["evidence_sha256"],
        )
        with pytest.raises(IntegrityError) as reading_overlap:
            async with session.begin_nested():
                await session.execute(insert(RawReading).values(_raw_values(device.id, 11)))
        _assert_named_check(reading_overlap, "ck_raw_readings_no_permanent_loss_overlap")

        await session.execute(insert(RawReading).values(_raw_values(device.id, 20)))
        with pytest.raises(IntegrityError) as loss_reading_overlap:
            async with session.begin_nested():
                await session.execute(
                    insert(UnavailableSequenceRange).values(_loss_values(device.id, 19, 21))
                )
        _assert_named_check(loss_reading_overlap, "ck_unavailable_sequence_ranges_no_raw_overlap")

        with pytest.raises(IntegrityError) as loss_range_overlap:
            async with session.begin_nested():
                await session.execute(
                    insert(UnavailableSequenceRange).values(_loss_values(device.id, 12, 14))
                )
        _assert_named_check(loss_range_overlap, "ck_unavailable_sequence_ranges_no_range_overlap")

        movable_reading = _raw_values(device.id, 5)
        await session.execute(insert(RawReading).values(movable_reading))
        with pytest.raises(DBAPIError) as reading_update_overlap:
            async with session.begin_nested():
                await session.execute(
                    update(RawReading)
                    .where(RawReading.id == movable_reading["id"])
                    .values(sequence=10)
                )
        raw_database_error = _database_error(reading_update_overlap.value)
        assert getattr(raw_database_error, "sqlstate", None) == "P0001"
        assert "immutable PowerMeter evidence cannot be changed" in str(raw_database_error)

        movable_loss = _loss_values(device.id, 30, 31)
        await session.execute(insert(UnavailableSequenceRange).values(movable_loss))
        with pytest.raises(IntegrityError) as loss_update_reading_overlap:
            async with session.begin_nested():
                await session.execute(
                    update(UnavailableSequenceRange)
                    .where(UnavailableSequenceRange.id == movable_loss["id"])
                    .values(first_sequence=20, last_sequence=21)
                )
        _assert_named_check(
            loss_update_reading_overlap,
            "ck_unavailable_sequence_ranges_immutable",
        )

        second_movable_loss = _loss_values(device.id, 40, 41)
        await session.execute(insert(UnavailableSequenceRange).values(second_movable_loss))
        with pytest.raises(IntegrityError) as loss_update_range_overlap:
            async with session.begin_nested():
                await session.execute(
                    update(UnavailableSequenceRange)
                    .where(UnavailableSequenceRange.id == second_movable_loss["id"])
                    .values(first_sequence=31, last_sequence=32)
                )
        _assert_named_check(
            loss_update_range_overlap,
            "ck_unavailable_sequence_ranges_immutable",
        )

        counts = (
            await session.scalar(select(RawReading.id).where(RawReading.device_id == device.id)),
            await session.scalar(
                select(UnavailableSequenceRange.id).where(
                    UnavailableSequenceRange.device_id == device.id
                )
            ),
        )
        assert all(counts)
        await session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_raw_and_loss_writes_are_serialized_per_device() -> None:
    _require_postgres()
    async with session_factory() as setup:
        device = await _device(setup)
        device_id = device.id
        await setup.commit()

    raw_ready = asyncio.Event()
    loss_ready = asyncio.Event()

    async def attempt_raw() -> tuple[str | None, str | None] | None:
        async with session_factory() as session:
            raw_ready.set()
            await loss_ready.wait()
            try:
                await session.execute(insert(RawReading).values(_raw_values(device_id, 100)))
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                database_error = _database_error(exc)
                return (
                    getattr(database_error, "sqlstate", None),
                    getattr(database_error, "constraint_name", None),
                )
        return None

    async def attempt_loss() -> tuple[str | None, str | None] | None:
        async with session_factory() as session:
            loss_ready.set()
            await raw_ready.wait()
            try:
                await session.execute(
                    insert(UnavailableSequenceRange).values(_loss_values(device_id, 100, 100))
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                database_error = _database_error(exc)
                return (
                    getattr(database_error, "sqlstate", None),
                    getattr(database_error, "constraint_name", None),
                )
        return None

    results = await asyncio.wait_for(asyncio.gather(attempt_raw(), attempt_loss()), timeout=15)
    failures = [result for result in results if result is not None]
    assert len(failures) == 1
    assert failures[0][0] == "23514"
    assert failures[0][1] in (
        "ck_raw_readings_no_permanent_loss_overlap",
        "ck_unavailable_sequence_ranges_no_raw_overlap",
    )

    async with session_factory() as verification:
        reading_count = int(
            await verification.scalar(
                select(func.count(RawReading.id)).where(
                    RawReading.device_id == device_id,
                    RawReading.sequence == 100,
                )
            )
            or 0
        )
        loss_count = int(
            await verification.scalar(
                select(func.count(UnavailableSequenceRange.id)).where(
                    UnavailableSequenceRange.device_id == device_id,
                    UnavailableSequenceRange.first_sequence <= 100,
                    UnavailableSequenceRange.last_sequence >= 100,
                )
            )
            or 0
        )
    assert reading_count + loss_count == 1
