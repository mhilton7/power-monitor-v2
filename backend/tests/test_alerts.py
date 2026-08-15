from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from backend.app.main import session_factory
from backend.app.models import (
    Alert,
    AlertEvent,
    AlertMaintenanceWindow,
    Device,
    DeviceHeartbeat,
    Home,
    RateCandidate,
    RateCandidateReview,
    RateSource,
    RateSourceRevision,
    RateSyncRun,
    User,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from worker.app.jobs import (
    REQUIRED_ALERT_TYPES,
    evaluate_operational_alerts,
    evaluate_sensor_alerts,
)

EXPECTED_ALERT_TYPES = {
    "sensor_offline",
    "heartbeat_delayed",
    "reading_backlog",
    "pzem_unavailable",
    "microsd_missing",
    "microsd_read_only",
    "microsd_nearly_full",
    "microsd_corrupt_segment",
    "time_untrusted",
    "tls_validation_failure",
    "wifi_repeated_failure",
    "ota_failed_or_rolled_back",
    "rate_source_changed",
    "rate_sync_failed",
    "backup_failed",
    "restore_test_failed",
}


async def _device(session: AsyncSession, now: datetime) -> tuple[Home, Device]:
    home = Home(id=str(uuid.uuid4()), name="Alert Test Home")
    session.add(home)
    await session.flush()
    device = Device(
        id=str(uuid.uuid4()),
        home_id=home.id,
        friendly_name="Main Panel",
        pzem_variant="PZEM-004T-v4-candidate",
        ct_rating_a=Decimal("100"),
        created_at=now - timedelta(minutes=5),
    )
    session.add(device)
    await session.flush()
    return home, device


def _heartbeat(device_id: str, received_at: datetime, *, healthy: bool) -> DeviceHeartbeat:
    return DeviceHeartbeat(
        device_id=device_id,
        boot_id=str(uuid.uuid4()),
        received_at=received_at,
        pzem_status="ok" if healthy else "timeout",
        storage_status="ok" if healthy else "missing",
        time_status="trusted" if healthy else "untrusted",
        backlog=0 if healthy else 20,
        health_flags=[] if healthy else ["tls_validation_failure", "wifi_repeated_failure"],
    )


def test_required_alert_type_set_is_complete() -> None:
    assert REQUIRED_ALERT_TYPES == EXPECTED_ALERT_TYPES


@pytest.mark.asyncio
async def test_device_alerts_debounce_then_resolve_with_evidence() -> None:
    now = datetime(2026, 8, 13, 20, 0, tzinfo=UTC)
    async with session_factory() as session:
        _home, device = await _device(session, now)
        session.add(_heartbeat(device.id, now, healthy=False))
        await session.flush()

        assert await evaluate_sensor_alerts(session, now=now) == 0
        session.add(_heartbeat(device.id, now + timedelta(seconds=31), healthy=False))
        await session.flush()
        assert await evaluate_sensor_alerts(session, now=now + timedelta(seconds=31)) == 6

        active = (await session.scalars(select(Alert).where(Alert.state == "open"))).all()
        assert {alert.alert_type for alert in active} == {
            "reading_backlog",
            "pzem_unavailable",
            "microsd_missing",
            "time_untrusted",
            "tls_validation_failure",
            "wifi_repeated_failure",
        }
        assert all(alert.evidence["debounce"]["observation_count"] == 2 for alert in active)

        session.add(_heartbeat(device.id, now + timedelta(seconds=62), healthy=True))
        await session.flush()
        assert await evaluate_sensor_alerts(session, now=now + timedelta(seconds=62)) == 6
        await session.flush()
        assert not (await session.scalars(select(Alert).where(Alert.state == "open"))).all()
        event_codes = set((await session.scalars(select(AlertEvent.event_code))).all())
        assert event_codes == {"OPENED", "RESOLVED"}


@pytest.mark.asyncio
async def test_maintenance_window_suppresses_then_allows_persistent_alert() -> None:
    now = datetime(2026, 8, 13, 21, 0, tzinfo=UTC)
    async with session_factory() as session:
        home, device = await _device(session, now)
        operator_id = str(uuid.uuid4())
        operator = User(
            id=operator_id,
            email="operator@example.test",
            display_name="Operator",
            password_hash="not-used-in-this-test",
        )
        session.add(operator)
        await session.flush()
        session.add(
            AlertMaintenanceWindow(
                home_id=home.id,
                device_id=device.id,
                alert_type="pzem_unavailable",
                starts_at=now - timedelta(minutes=1),
                ends_at=now + timedelta(seconds=60),
                reason="Qualified electrical maintenance",
                created_by_user_id=operator_id,
            )
        )
        session.add(_heartbeat(device.id, now, healthy=False))
        await session.flush()
        await evaluate_sensor_alerts(session, now=now)
        session.add(_heartbeat(device.id, now + timedelta(seconds=31), healthy=False))
        await session.flush()
        await evaluate_sensor_alerts(session, now=now + timedelta(seconds=31))
        assert (
            await session.scalar(select(Alert.id).where(Alert.alert_type == "pzem_unavailable"))
            is None
        )

        session.add(_heartbeat(device.id, now + timedelta(seconds=62), healthy=False))
        await session.flush()
        await evaluate_sensor_alerts(session, now=now + timedelta(seconds=62))
        alert = await session.scalar(select(Alert).where(Alert.alert_type == "pzem_unavailable"))
        assert alert is not None
        assert alert.state == "open"


@pytest.mark.asyncio
async def test_backup_failure_status_opens_and_verified_status_resolves() -> None:
    now = datetime(2026, 8, 13, 22, 0, tzinfo=UTC)
    status_dir = Path(".test-runtime") / f"alert-status-{uuid.uuid4()}"
    status_dir.mkdir(parents=True)
    (status_dir / "last-backup-attempt.json").write_text(
        json.dumps({"format": "pm-backup/1.0.0", "state": "failed", "run_id": "b-1"}),
        encoding="utf-8",
    )
    (status_dir / "last-restore-test-attempt.json").write_text(
        json.dumps({"format": "pm-backup/1.0.0", "state": "verified", "run_id": "r-1"}),
        encoding="utf-8",
    )
    async with session_factory() as session:
        session.add(Home(name="Operational Alert Home"))
        await session.flush()
        assert await evaluate_operational_alerts(session, status_dir=status_dir, now=now) == 1
        alert = await session.scalar(select(Alert).where(Alert.alert_type == "backup_failed"))
        assert alert is not None and alert.state == "open"

        (status_dir / "last-backup-attempt.json").write_text(
            json.dumps({"format": "pm-backup/1.0.0", "state": "verified", "run_id": "b-2"}),
            encoding="utf-8",
        )
        assert (
            await evaluate_operational_alerts(
                session, status_dir=status_dir, now=now + timedelta(seconds=15)
            )
            == 1
        )
        assert alert.state == "resolved"


@pytest.mark.asyncio
async def test_operational_alerts_include_sensorless_home_when_another_home_has_device() -> None:
    now = datetime(2026, 8, 13, 22, 30, tzinfo=UTC)
    status_dir = Path(".test-runtime") / f"mixed-home-alert-status-{uuid.uuid4()}"
    status_dir.mkdir(parents=True)
    (status_dir / "last-backup-attempt.json").write_text(
        json.dumps({"format": "pm-backup/1.0.0", "state": "failed", "run_id": "mixed-b-1"}),
        encoding="utf-8",
    )
    (status_dir / "last-restore-test-attempt.json").write_text(
        json.dumps({"format": "pm-backup/1.0.0", "state": "verified", "run_id": "mixed-r-1"}),
        encoding="utf-8",
    )
    async with session_factory() as session:
        sensor_home, _device_row = await _device(session, now)
        sensorless_home = Home(name="Sensorless Operational Alert Home")
        session.add(sensorless_home)
        await session.flush()

        assert await evaluate_operational_alerts(session, status_dir=status_dir, now=now) == 2
        alerts = (
            await session.scalars(select(Alert).where(Alert.alert_type == "backup_failed"))
        ).all()
        assert {alert.home_id for alert in alerts} == {sensor_home.id, sensorless_home.id}


@pytest.mark.asyncio
async def test_rejected_rate_candidate_resolves_exact_home_review_alert() -> None:
    now = datetime(2026, 8, 13, 23, 0, tzinfo=UTC)
    status_dir = Path(".test-runtime") / f"rejected-rate-alert-status-{uuid.uuid4()}"
    status_dir.mkdir(parents=True)
    async with session_factory() as session:
        home = Home(name="Rejected Rate Candidate Home")
        reviewer = User(
            email="rate-reviewer@example.test",
            display_name="Rate Reviewer",
            password_hash="not-used-in-this-test",
        )
        source = RateSource(
            name="SCE official candidate alert test",
            source_type="official_https",
            https_url="https://www.sce.com/regulatory/tariff-books",
            enabled=True,
        )
        session.add_all((home, reviewer, source))
        await session.flush()
        revision = RateSourceRevision(
            source_id=source.id,
            artifact_sha256="a" * 64,
            parser_version="sce-tou-public-html-v1",
        )
        session.add(revision)
        await session.flush()
        candidate = RateCandidate(
            source_revision_id=revision.id,
            normalized_rates={"schema": "sce-rate-candidate/1.0.0", "plans": []},
            validation_evidence={"coverage": "complete"},
            state="review_required",
        )
        session.add(candidate)
        await session.flush()
        session.add(
            RateSyncRun(
                source_id=source.id,
                home_id=home.id,
                state="review_required",
                event_code="RATE_SOURCE_CHANGED",
                correlation_id="rejected-alert-candidate",
                requested_url=source.https_url or "https://www.sce.com/",
                revision_id=revision.id,
                completed_at=now,
                evidence={"candidate_id": candidate.id, "initiator": "scheduled_worker"},
            )
        )
        await session.flush()

        assert await evaluate_operational_alerts(session, status_dir=status_dir, now=now) == 1
        alert = await session.scalar(
            select(Alert).where(
                Alert.home_id == home.id,
                Alert.alert_type == "rate_source_changed",
            )
        )
        assert alert is not None and alert.state == "open"

        session.add(
            RateCandidateReview(
                candidate_id=candidate.id,
                home_id=home.id,
                selected_plan_name=None,
                effective_start=None,
                state="rejected",
                reviewed_by_user_id=reviewer.id,
                reviewed_at=now + timedelta(seconds=1),
            )
        )
        await session.flush()
        assert (
            await evaluate_operational_alerts(
                session,
                status_dir=status_dir,
                now=now + timedelta(seconds=1),
            )
            == 1
        )
        assert alert.state == "resolved"
