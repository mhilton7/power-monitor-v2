from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from backend.app.main import app, session_factory
from backend.app.models import (
    Alert,
    AlertDismissal,
    AuditEvent,
    Home,
    Role,
    User,
    user_home_scopes,
    user_roles,
)
from backend.app.security.passwords import hash_password
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

PASSWORD = "correct horse battery staple 2026!"


async def _owner_scope() -> tuple[str, str]:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(User.id, user_home_scopes.c.home_id)
                .join(user_home_scopes, user_home_scopes.c.user_id == User.id)
                .where(User.email == "owner@example.com")
            )
        ).one()
        return row[0], row[1]


async def _shared_viewer(home_id: str) -> str:
    async with session_factory() as session:
        viewer_role_id = await session.scalar(select(Role.id).where(Role.name == "Viewer"))
        assert viewer_role_id is not None
        viewer = User(
            email="alert-viewer@example.com",
            display_name="Alert viewer",
            password_hash=hash_password(PASSWORD),
        )
        session.add(viewer)
        await session.flush()
        await session.execute(user_roles.insert().values(user_id=viewer.id, role_id=viewer_role_id))
        await session.execute(user_home_scopes.insert().values(user_id=viewer.id, home_id=home_id))
        await session.commit()
        return viewer.id


async def _logged_in_viewer() -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="https://powermeter.test")
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "alert-viewer@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = client.cookies["pm_csrf"]
    return client


@pytest.mark.asyncio
async def test_dismissal_is_per_user_idempotent_and_preserves_alert_evidence(
    owner_client: AsyncClient,
) -> None:
    owner_id, home_id = await _owner_scope()
    viewer_id = await _shared_viewer(home_id)
    opened_at = datetime.now(UTC) - timedelta(days=14)
    async with session_factory() as session:
        alert = Alert(
            home_id=home_id,
            alert_type="sensor_offline",
            severity="warning",
            state="acknowledged",
            evidence={"last_received_at": opened_at.isoformat(), "retained": True},
            opened_at=opened_at,
            acknowledged_at=opened_at + timedelta(minutes=1),
        )
        session.add(alert)
        await session.commit()
        alert_id = alert.id

    listed = await owner_client.get("/api/v1/alerts")
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["alerts"]] == [alert_id]

    dismissed = await owner_client.delete(f"/api/v1/alerts/{alert_id}/notification")
    assert dismissed.status_code == 200, dismissed.text
    first_dismissed_at = dismissed.json()["dismissed_at"]
    repeated = await owner_client.delete(f"/api/v1/alerts/{alert_id}/notification")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["dismissed_at"] == first_dismissed_at
    assert (await owner_client.get("/api/v1/alerts")).json()["alerts"] == []

    viewer_client = await _logged_in_viewer()
    try:
        viewer_alerts = await viewer_client.get("/api/v1/alerts")
        assert viewer_alerts.status_code == 200, viewer_alerts.text
        assert [row["id"] for row in viewer_alerts.json()["alerts"]] == [alert_id]
    finally:
        await viewer_client.aclose()

    async with session_factory() as session:
        retained = await session.get(Alert, alert_id)
        assert retained is not None
        assert retained.state == "acknowledged"
        assert retained.evidence == {"last_received_at": opened_at.isoformat(), "retained": True}
        dismissals = (
            await session.scalars(select(AlertDismissal).where(AlertDismissal.alert_id == alert_id))
        ).all()
        assert [(row.user_id, row.alert_id) for row in dismissals] == [(owner_id, alert_id)]
        assert viewer_id not in {row.user_id for row in dismissals}
        assert (
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.event_code == "ALERT_NOTIFICATION_DISMISSED",
                    AuditEvent.target_id == alert_id,
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_clear_all_dismisses_only_visible_lifecycle_states_and_is_idempotent(
    owner_client: AsyncClient,
) -> None:
    owner_id, home_id = await _owner_scope()
    other_home = Home(name="Out-of-scope alert home")
    async with session_factory() as session:
        session.add(other_home)
        await session.flush()
        visible = [
            Alert(
                home_id=home_id,
                alert_type="backup_failed",
                severity="critical",
                state="open",
                evidence={"ordinal": 1},
            ),
            Alert(
                home_id=home_id,
                alert_type="rate_source_changed",
                severity="info",
                state="acknowledged",
                evidence={"ordinal": 2},
            ),
        ]
        resolved = Alert(
            home_id=home_id,
            alert_type="rate_sync_failed",
            severity="warning",
            state="resolved",
            evidence={"ordinal": 3},
            resolved_at=datetime.now(UTC),
        )
        hidden = Alert(
            home_id=other_home.id,
            alert_type="restore_test_failed",
            severity="critical",
            state="open",
            evidence={"ordinal": 4},
        )
        session.add_all([*visible, resolved, hidden])
        await session.commit()
        visible_ids = {row.id for row in visible}
        hidden_id = hidden.id

    cleared = await owner_client.delete("/api/v1/alerts/notifications")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"dismissed_count": 2}
    assert (await owner_client.delete("/api/v1/alerts/notifications")).json() == {
        "dismissed_count": 0
    }
    assert (await owner_client.get("/api/v1/alerts")).json()["alerts"] == []
    out_of_scope = await owner_client.delete(f"/api/v1/alerts/{hidden_id}/notification")
    assert out_of_scope.status_code == 404

    async with session_factory() as session:
        dismissed_ids = set(
            (
                await session.scalars(
                    select(AlertDismissal.alert_id).where(AlertDismissal.user_id == owner_id)
                )
            ).all()
        )
        assert dismissed_ids == visible_ids
        assert await session.scalar(select(func.count(Alert.id))) == 4
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.event_code == "ALERT_NOTIFICATIONS_DISMISSED")
        )
        assert audit is not None
        assert audit.details == {"dismissed_count": 2, "alert_evidence_preserved": True}
