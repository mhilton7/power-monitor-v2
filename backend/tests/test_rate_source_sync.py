from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from backend.app.config import Settings
from backend.app.main import session_factory
from backend.app.models import (
    Alert,
    AuditEvent,
    Home,
    RateCandidate,
    RateSource,
    RateSourceArtifact,
    RateSourceRevision,
    RateSyncRun,
)
from backend.app.services import rate_sync
from backend.app.services.rate_sources import SourceFetch, SourceHop
from backend.app.services.rate_sync import (
    SCE_SOURCE_NAME,
    SCE_TOU_URL,
    RateSyncResult,
    sync_due_rate_sources,
    sync_official_rate_source,
)
from backend.app.services.sce_rate_parser import SourceParseError, parse_sce_tou_public_page
from httpx import AsyncClient
from sqlalchemy import func, select
from worker.app.jobs import evaluate_operational_alerts


@pytest.fixture
def artifact_dir() -> Path:
    path = Path(".test-runtime") / f"rate-sync-{uuid.uuid4()}"
    path.mkdir(parents=True)
    return path


def _periods(
    names: tuple[str, ...],
    prices: tuple[int, ...],
    times: tuple[str, ...],
) -> str:
    values: list[str] = []
    for name, price, time_label in zip(names, prices, times, strict=True):
        values.append(f"<span>{name} {price}&#162;</span><span>{time_label}</span>")
    values.append("<span>12am</span>")
    return "".join(values)


def _plan(
    heading: str,
    *,
    baseline: bool,
    summer_weekday: tuple[int, int, int],
    summer_weekend: tuple[int, int, int],
    winter: tuple[int, int, int, int],
    peak_start: str,
    peak_end: str,
) -> str:
    baseline_rule = (
        "Baseline Credit: $0.10 per kWh up to your monthly baseline allocation"
        if baseline
        else "Baseline Credit: None"
    )
    after = "<p>After Baseline Credit</p>" if baseline else ""
    return f"""
      <section>
        <h2>{heading}</h2>
        <p>Base Services Charge: $0.79 per day</p>
        <p>{baseline_rule}</p>
        <p>The rates shown reflect pricing for customers receiving both delivery and generation
        services from SCE.</p>
        <h3>June - September</h3>
        <h4>Weekdays</h4>
        {
        _periods(
            ("Off-Peak", "On-Peak", "Off-Peak"),
            summer_weekday,
            ("12am", peak_start, peak_end),
        )
    }
        {after}
        <h4>Weekend</h4>
        {
        _periods(
            ("Off-Peak", "Mid-Peak", "Off-Peak"),
            summer_weekend,
            ("12am", peak_start, peak_end),
        )
    }
        {after}
        <h3>October - May</h3>
        <h4>Weekdays &amp; Weekend</h4>
        {
        _periods(
            ("Off-Peak", "Super-Off-Peak", "Mid-Peak", "Off-Peak"),
            winter,
            ("12am", "8am", peak_start, peak_end),
        )
    }
        {after}
      </section>
    """


def valid_sce_page(*, first_off_peak_cents: int = 34) -> bytes:
    html = f"""
    <html><body>
      <p>Holidays follow weekend rates.</p>
      {
        _plan(
            "TOU-D 4 PM to 9 PM",
            baseline=True,
            summer_weekday=(first_off_peak_cents, 58, first_off_peak_cents),
            summer_weekend=(first_off_peak_cents, 46, first_off_peak_cents),
            winter=(37, 33, 51, 37),
            peak_start="4pm",
            peak_end="9pm",
        )
    }
      {
        _plan(
            "TOU-D 5 PM to 8 PM",
            baseline=True,
            summer_weekday=(34, 74, 34),
            summer_weekend=(34, 54, 34),
            winter=(38, 32, 60, 38),
            peak_start="5pm",
            peak_end="8pm",
        )
    }
      {
        _plan(
            "TOU-D-PRIME",
            baseline=False,
            summer_weekday=(26, 59, 26),
            summer_weekend=(26, 40, 26),
            winter=(24, 24, 56, 24),
            peak_start="4pm",
            peak_end="9pm",
        )
    }
    </body></html>
    """
    return html.encode()


def _fetched(
    body: bytes,
    *,
    etag: str = '"rate-v1"',
    last_modified: str = "Thu, 13 Aug 2026 18:11:43 GMT",
    url: str = SCE_TOU_URL,
) -> SourceFetch:
    import hashlib

    return SourceFetch(
        requested_url=url,
        url=url,
        status_code=200,
        body=body,
        sha256=hashlib.sha256(body).hexdigest(),
        etag=etag,
        last_modified=last_modified,
        media_type="text/html",
        hops=(
            SourceHop(
                url=url,
                hostname="www.sce.com",
                resolved_ips=("93.184.216.34",),
                connected_ip="93.184.216.34",
                status_code=200,
            ),
        ),
    )


def _settings(artifact_dir: Path) -> Settings:
    return Settings(env="test", rate_artifact_dir=artifact_dir)


def test_immutable_artifact_fsyncs_parent_after_replace_and_on_reuse(
    artifact_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"durable official rate source"
    import hashlib

    digest = hashlib.sha256(body).hexdigest()
    events: list[object] = []
    directory_descriptor = 987_654
    module_os = vars(rate_sync)["os"]
    real_replace = module_os.replace

    def record_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        real_replace(source, target)
        events.append("replace")

    def open_directory(path: str | os.PathLike[str], flags: int) -> int:
        assert Path(path) == artifact_dir
        assert flags & 0x100000
        events.append("open-directory")
        return directory_descriptor

    def record_fsync(descriptor: int) -> None:
        events.append(("fsync", descriptor))

    def close_directory(descriptor: int) -> None:
        assert descriptor == directory_descriptor
        events.append("close-directory")

    monkeypatch.setattr(module_os, "O_DIRECTORY", 0x100000, raising=False)
    monkeypatch.setattr(module_os, "replace", record_replace)
    monkeypatch.setattr(module_os, "open", open_directory)
    monkeypatch.setattr(module_os, "fsync", record_fsync)
    monkeypatch.setattr(module_os, "close", close_directory)

    target = rate_sync._write_immutable_artifact(artifact_dir, digest, body)
    assert target.read_bytes() == body
    assert events.index("replace") < events.index(("fsync", directory_descriptor))

    events.clear()
    assert rate_sync._write_immutable_artifact(artifact_dir, digest, body) == target
    assert "replace" not in events
    assert ("fsync", directory_descriptor) in events


def test_strict_parser_validates_complete_candidate_without_effective_date_guess() -> None:
    parsed = parse_sce_tou_public_page(valid_sce_page(), "text/html")
    assert parsed.normalized_rates["effective_start"] is None
    assert parsed.normalized_rates["effective_date_confirmation_required"] is True
    assert parsed.validation_evidence["plan_count"] == 3
    assert parsed.validation_evidence["period_count"] == 30
    assert parsed.validation_evidence["coverage"] == "complete"


@pytest.mark.asyncio
async def test_manual_check_now_uses_shared_sync_service(
    owner_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None, str | None]] = []

    async def shared_service(
        _session: object,
        _settings: object,
        source: RateSource,
        *,
        home_id: str | None,
        actor_user_id: str | None,
        correlation_id: str,
    ) -> RateSyncResult:
        calls.append((source.https_url or "", home_id, actor_user_id))
        assert correlation_id
        return RateSyncResult(
            run_id="run-shared",
            state="unchanged",
            event_code="RATE_SOURCE_NOT_MODIFIED",
            revision_id="revision-shared",
            candidate_id=None,
            error_code=None,
        )

    monkeypatch.setattr(
        "backend.app.routes.billing.sync_official_rate_source",
        shared_service,
    )
    response = await owner_client.post("/api/v1/rate-sources/check-now")
    assert response.status_code == 202
    assert response.json() == {
        "run_id": "run-shared",
        "state": "unchanged",
        "event_code": "RATE_SOURCE_NOT_MODIFIED",
        "revision_id": "revision-shared",
        "candidate_id": None,
        "error_code": None,
    }
    assert len(calls) == 1
    assert calls[0][0] == SCE_TOU_URL
    assert calls[0][1] is not None
    assert calls[0][2] is not None


@pytest.mark.asyncio
async def test_changed_source_creates_immutable_snapshot_candidate_diff_and_audit(
    artifact_dir: Path,
) -> None:
    body = valid_sce_page()

    async def fetcher(_url: str, **_kwargs: object) -> SourceFetch:
        return _fetched(body)

    async with session_factory() as session:
        home = Home(name="Rate Review Home")
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
            check_interval_hours=168,
        )
        session.add_all([home, source])
        await session.flush()
        result = await sync_official_rate_source(
            session,
            _settings(artifact_dir),
            source,
            home_id=home.id,
            actor_user_id=None,
            correlation_id="candidate-test",
            fetcher=fetcher,
        )
        await session.commit()

        assert result.state == "review_required"
        assert result.event_code == "RATE_SOURCE_CHANGED"
        candidate = await session.get(RateCandidate, result.candidate_id)
        assert candidate is not None
        assert candidate.diff["before"] is None
        assert candidate.diff["after"] == candidate.normalized_rates
        assert candidate.diff["change_count"] > 0
        assert candidate.validation_evidence["source_artifact_sha256"]
        assert await session.scalar(select(func.count()).select_from(RateSourceRevision)) == 1
        artifact = await session.scalar(select(RateSourceArtifact))
        assert artifact is not None
        assert Path(artifact.storage_path).read_bytes() == body
        run = await session.get(RateSyncRun, result.run_id)
        assert run is not None
        assert run.correlation_id == "candidate-test"
        assert run.final_url == SCE_TOU_URL
        assert run.http_status == 200
        assert run.response_bytes == len(body)
        assert run.evidence["hops"][0]["connected_ip"] == "93.184.216.34"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.target_id == result.run_id)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_duplicate_200_reuses_snapshot_candidate_and_transaction(
    artifact_dir: Path,
) -> None:
    body = valid_sce_page()

    async def fetcher(_url: str, **_kwargs: object) -> SourceFetch:
        return _fetched(body)

    async with session_factory() as session:
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
            check_interval_hours=168,
        )
        session.add(source)
        await session.flush()
        first = await sync_official_rate_source(
            session,
            _settings(artifact_dir),
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="duplicate-1",
            fetcher=fetcher,
        )
        second = await sync_official_rate_source(
            session,
            _settings(artifact_dir),
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="duplicate-2",
            fetcher=fetcher,
        )
        await session.commit()

        assert first.state == "review_required"
        assert second.state == "unchanged"
        assert second.revision_id == first.revision_id
        assert second.candidate_id == first.candidate_id
        assert await session.scalar(select(func.count()).select_from(RateSourceRevision)) == 1
        assert await session.scalar(select(func.count()).select_from(RateSourceArtifact)) == 1
        assert await session.scalar(select(func.count()).select_from(RateCandidate)) == 1
        assert await session.scalar(select(func.count()).select_from(RateSyncRun)) == 2


@pytest.mark.asyncio
async def test_changed_prices_create_side_by_side_diff_against_prior_candidate(
    artifact_dir: Path,
) -> None:
    bodies = [valid_sce_page(), valid_sce_page(first_off_peak_cents=35)]
    calls = 0

    async def fetcher(_url: str, **_kwargs: object) -> SourceFetch:
        nonlocal calls
        body = bodies[calls]
        calls += 1
        return _fetched(body, etag=f'"rate-v{calls}"')

    async with session_factory() as session:
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
            check_interval_hours=168,
        )
        session.add(source)
        await session.flush()
        first = await sync_official_rate_source(
            session,
            _settings(artifact_dir),
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="diff-1",
            fetcher=fetcher,
        )
        second = await sync_official_rate_source(
            session,
            _settings(artifact_dir),
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="diff-2",
            fetcher=fetcher,
        )
        await session.commit()

        candidate = await session.get(RateCandidate, second.candidate_id)
        assert candidate is not None
        assert candidate.diff["previous_candidate_id"] == first.candidate_id
        assert candidate.diff["before"] is not None
        assert candidate.diff["after"] == candidate.normalized_rates
        assert any(
            change["before"] == "0.34000000" and change["after"] == "0.35000000"
            for change in candidate.diff["changes"]
        )


@pytest.mark.asyncio
async def test_304_reuses_prior_revision_and_never_persists_empty_snapshot(
    artifact_dir: Path,
) -> None:
    body = valid_sce_page()
    calls = 0

    async def fetcher(_url: str, **kwargs: object) -> SourceFetch:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _fetched(body)
        assert kwargs["etag"] == '"rate-v1"'
        assert kwargs["last_modified"] == "Thu, 13 Aug 2026 18:11:43 GMT"
        return SourceFetch(
            requested_url=SCE_TOU_URL,
            url=SCE_TOU_URL,
            status_code=304,
            body=None,
            sha256=None,
            etag='"rate-v1"',
            last_modified="Thu, 13 Aug 2026 18:11:43 GMT",
            media_type=None,
            hops=(
                SourceHop(
                    url=SCE_TOU_URL,
                    hostname="www.sce.com",
                    resolved_ips=("93.184.216.34",),
                    connected_ip="93.184.216.34",
                    status_code=304,
                ),
            ),
        )

    async with session_factory() as session:
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
            check_interval_hours=168,
        )
        session.add(source)
        await session.flush()
        first = await sync_official_rate_source(
            session,
            _settings(artifact_dir),
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="etag-1",
            fetcher=fetcher,
        )
        second = await sync_official_rate_source(
            session,
            _settings(artifact_dir),
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="etag-2",
            fetcher=fetcher,
        )
        await session.commit()

        assert second.state == "unchanged"
        assert second.event_code == "RATE_SOURCE_NOT_MODIFIED"
        assert second.revision_id == first.revision_id
        assert await session.scalar(select(func.count()).select_from(RateSourceRevision)) == 1
        assert await session.scalar(select(func.count()).select_from(RateSourceArtifact)) == 1


@pytest.mark.asyncio
async def test_storage_failure_does_not_advance_snapshot_validators(
    artifact_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_body = valid_sce_page()
    new_body = valid_sce_page(first_off_peak_cents=35)

    async def initial_fetch(_url: str, **_kwargs: object) -> SourceFetch:
        return _fetched(old_body, etag='"stored-v1"')

    async def changed_fetch(_url: str, **kwargs: object) -> SourceFetch:
        assert kwargs["etag"] == '"stored-v1"'
        return _fetched(new_body, etag='"unstored-v2"')

    async def not_modified_fetch(_url: str, **kwargs: object) -> SourceFetch:
        assert kwargs["etag"] == '"stored-v1"'
        return SourceFetch(
            requested_url=SCE_TOU_URL,
            url=SCE_TOU_URL,
            status_code=304,
            body=None,
            sha256=None,
            etag='"stored-v1"',
            last_modified="Thu, 13 Aug 2026 18:11:43 GMT",
            media_type=None,
            hops=(
                SourceHop(
                    url=SCE_TOU_URL,
                    hostname="www.sce.com",
                    resolved_ips=("93.184.216.34",),
                    connected_ip="93.184.216.34",
                    status_code=304,
                ),
            ),
        )

    def fail_artifact_write(_directory: Path, _digest: str, _body: bytes) -> Path:
        raise OSError("simulated durable storage failure")

    async with session_factory() as session:
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
            check_interval_hours=168,
        )
        session.add(source)
        await session.flush()
        stored = await sync_official_rate_source(
            session,
            _settings(artifact_dir),
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="validator-stored",
            fetcher=initial_fetch,
        )
        assert stored.revision_id is not None
        assert source.current_etag == '"stored-v1"'

        with monkeypatch.context() as patch:
            patch.setattr(
                "backend.app.services.rate_sync._write_immutable_artifact",
                fail_artifact_write,
            )
            failed = await sync_official_rate_source(
                session,
                _settings(artifact_dir),
                source,
                home_id=None,
                actor_user_id=None,
                correlation_id="validator-storage-failed",
                fetcher=changed_fetch,
            )
        assert failed.state == "failed"
        assert failed.error_code == "ARTIFACT_STORAGE_FAILED"
        assert source.current_etag == '"stored-v1"'

        unchanged = await sync_official_rate_source(
            session,
            _settings(artifact_dir),
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="validator-old-304",
            fetcher=not_modified_fetch,
        )
        await session.commit()

        assert unchanged.state == "unchanged"
        assert unchanged.revision_id == stored.revision_id
        assert source.current_etag == '"stored-v1"'
        assert await session.scalar(select(func.count()).select_from(RateSourceRevision)) == 1
        assert await session.scalar(select(func.count()).select_from(RateSourceArtifact)) == 1


@pytest.mark.asyncio
async def test_parse_failure_keeps_validators_and_304_forces_safe_full_retry(
    artifact_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_body = valid_sce_page()
    changed_body = valid_sce_page(first_off_peak_cents=35)
    old_modified = "Thu, 13 Aug 2026 18:11:43 GMT"
    changed_modified = "Fri, 14 Aug 2026 18:11:43 GMT"

    async def initial_fetch(_url: str, **_kwargs: object) -> SourceFetch:
        return _fetched(
            old_body,
            etag='"parsed-v1"',
            last_modified=old_modified,
        )

    async def changed_fetch(_url: str, **kwargs: object) -> SourceFetch:
        assert kwargs["etag"] == '"parsed-v1"'
        assert kwargs["last_modified"] == old_modified
        return _fetched(
            changed_body,
            etag='"failed-v2"',
            last_modified=changed_modified,
        )

    def fail_parser(_body: bytes, _media_type: str) -> object:
        raise SourceParseError(
            "SIMULATED_PARSE_FAILURE",
            "simulated parser failure",
        )

    recovery_calls = 0

    async def recovery_fetch(_url: str, **kwargs: object) -> SourceFetch:
        nonlocal recovery_calls
        recovery_calls += 1
        assert kwargs["allowed_hosts"] == _settings(artifact_dir).allowed_sce_hosts
        if recovery_calls == 1:
            assert kwargs["etag"] == '"failed-v2"'
            assert kwargs["last_modified"] == changed_modified
            return SourceFetch(
                requested_url=SCE_TOU_URL,
                url=SCE_TOU_URL,
                status_code=304,
                body=None,
                sha256=None,
                etag='"failed-v2"',
                last_modified=changed_modified,
                media_type=None,
                hops=(
                    SourceHop(
                        url=SCE_TOU_URL,
                        hostname="www.sce.com",
                        resolved_ips=("93.184.216.34",),
                        connected_ip="93.184.216.34",
                        status_code=304,
                    ),
                ),
            )
        assert kwargs["etag"] is None
        assert kwargs["last_modified"] is None
        return _fetched(
            changed_body,
            etag='"failed-v2"',
            last_modified=changed_modified,
        )

    settings = _settings(artifact_dir)
    async with session_factory() as session:
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
            check_interval_hours=168,
        )
        session.add(source)
        await session.flush()
        parsed = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="parsed-v1",
            fetcher=initial_fetch,
        )
        assert parsed.candidate_id is not None

        with monkeypatch.context() as patch:
            patch.setattr(rate_sync, "parse_sce_tou_public_page", fail_parser)
            failed = await sync_official_rate_source(
                session,
                settings,
                source,
                home_id=None,
                actor_user_id=None,
                correlation_id="failed-v2",
                fetcher=changed_fetch,
            )
        assert failed.state == "failed"
        assert failed.error_code == "SIMULATED_PARSE_FAILURE"
        assert source.current_etag == '"parsed-v1"'
        assert source.current_last_modified == old_modified

        # Simulate the pre-repair state in which a failed revision's validators
        # had already been committed. A 304 must trigger one unconditional fetch
        # through the same allowlist/SSRF-limited fetcher and retry parsing.
        source.current_etag = '"failed-v2"'
        source.current_last_modified = changed_modified
        recovered = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="recover-v2",
            fetcher=recovery_fetch,
        )
        await session.commit()

        assert recovery_calls == 2
        assert recovered.state == "review_required"
        assert recovered.candidate_id is not None
        assert recovered.revision_id == failed.revision_id
        assert source.current_etag == '"failed-v2"'
        assert source.current_last_modified == changed_modified
        assert await session.scalar(select(func.count()).select_from(RateSourceRevision)) == 2
        assert await session.scalar(select(func.count()).select_from(RateSourceArtifact)) == 2
        assert await session.scalar(select(func.count()).select_from(RateCandidate)) == 2
        run = await session.get(RateSyncRun, recovered.run_id)
        assert run is not None
        assert run.http_status == 200
        assert run.evidence["conditional_recovery"]["reason"] == (
            "latest_revision_has_no_parsed_candidate"
        )


@pytest.mark.asyncio
async def test_layout_failure_persists_snapshot_failure_evidence_and_alert(
    artifact_dir: Path,
) -> None:
    body = b"<html><body><h1>Unexpected layout</h1></body></html>"

    async def fetcher(_url: str, **_kwargs: object) -> SourceFetch:
        return _fetched(body)

    status_dir = artifact_dir / "status"
    status_dir.mkdir()
    verified = {"format": "pm-backup/1.0.0", "state": "verified", "run_id": "ok"}
    (status_dir / "last-backup-attempt.json").write_text(json.dumps(verified), encoding="utf-8")
    (status_dir / "last-restore-test-attempt.json").write_text(
        json.dumps(verified),
        encoding="utf-8",
    )
    async with session_factory() as session:
        home = Home(name="Sync Failure Home")
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
            check_interval_hours=168,
        )
        session.add_all([home, source])
        await session.flush()
        result = await sync_official_rate_source(
            session,
            _settings(artifact_dir / "artifacts"),
            source,
            home_id=home.id,
            actor_user_id=None,
            correlation_id="parse-failure",
            fetcher=fetcher,
        )
        await session.flush()
        await evaluate_operational_alerts(session, status_dir=status_dir)
        await session.commit()

        assert result.state == "failed"
        assert result.event_code == "RATE_SYNC_PARSE_FAILED"
        assert result.error_code == "RATE_PLAN_TYPE_UNRESOLVED"
        assert result.revision_id is not None
        assert result.candidate_id is None
        assert await session.scalar(select(func.count()).select_from(RateSourceRevision)) == 1
        assert await session.scalar(select(func.count()).select_from(RateSourceArtifact)) == 1
        assert await session.scalar(select(func.count()).select_from(RateCandidate)) == 0
        run = await session.get(RateSyncRun, result.run_id)
        assert run is not None
        assert run.evidence["failure_phase"] == "parsing"
        assert run.evidence["manual_review_required"] is True
        alert = await session.scalar(
            select(Alert).where(
                Alert.home_id == home.id,
                Alert.alert_type == "rate_sync_failed",
            )
        )
        assert alert is not None and alert.state == "open"
        assert alert.evidence["failed_runs"][0]["run_id"] == result.run_id


@pytest.mark.asyncio
async def test_weekly_scheduler_checks_only_due_enabled_sources(artifact_dir: Path) -> None:
    now = datetime.now(UTC)
    body = valid_sce_page()
    catalog_detail_url = (
        "https://www.sce.com/save-money/rates-financing/residential-rate-plans/tou-d-4-9"
    )
    catalog_root = (
        f'<html><body><a href="{catalog_detail_url}">TOU-D 4 PM to 9 PM</a></body></html>'
    ).encode()
    calls = 0

    async def fetcher(url: str, **_kwargs: object) -> SourceFetch:
        nonlocal calls
        calls += 1
        if url == rate_sync.SCE_CATALOG_URL:
            return _fetched(catalog_root, url=url)
        return _fetched(body, url=url)

    async with session_factory() as session:
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
            check_interval_hours=168,
            last_checked_at=now - timedelta(hours=167),
        )
        session.add(source)
        await session.flush()
        not_due = await sync_due_rate_sources(
            session,
            _settings(artifact_dir),
            now=now,
            fetcher=fetcher,
        )
        due = await sync_due_rate_sources(
            session,
            _settings(artifact_dir),
            now=now + timedelta(hours=2),
            fetcher=fetcher,
        )
        await session.commit()

        assert not_due == {"checked": 1, "failed": 0, "review_required": 0, "unchanged": 1}
        assert due == {"checked": 1, "failed": 0, "review_required": 1, "unchanged": 0}
        assert calls == 3
