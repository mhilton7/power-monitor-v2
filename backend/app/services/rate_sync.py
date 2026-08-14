from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import (
    AuditEvent,
    RateCandidate,
    RateSource,
    RateSourceArtifact,
    RateSourceRevision,
    RateSyncRun,
    aware_utc,
)
from .rate_sources import SourceFetch, SourceFetchError, fetch_official_source
from .sce_rate_parser import (
    PARSER_VERSION,
    SourceParseError,
    parse_sce_tou_public_page,
    side_by_side_diff,
)

SCE_TOU_URL = (
    "https://www.sce.com/save-money/rates-financing/residential-rate-plans/time-of-use-plans"
)
SCE_SOURCE_NAME = "SCE residential TOU public page"

FetchCallable = Callable[..., Awaitable[SourceFetch]]


@dataclass(frozen=True)
class RateSyncResult:
    run_id: str
    state: str
    event_code: str
    revision_id: str | None
    candidate_id: str | None
    error_code: str | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def ensure_default_sce_source(session: AsyncSession) -> RateSource:
    source = await session.scalar(select(RateSource).where(RateSource.https_url == SCE_TOU_URL))
    if source is not None:
        return source
    candidate = RateSource(
        name=SCE_SOURCE_NAME,
        source_type="official_https",
        https_url=SCE_TOU_URL,
        enabled=True,
        check_interval_hours=168,
    )
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(RateSource).where(RateSource.https_url == SCE_TOU_URL)
        )
        if existing is None:
            raise
        return existing
    return candidate


def _fetch_evidence(fetched: SourceFetch) -> dict[str, object]:
    return {
        "network_policy": "prevalidated-public-ip-pinned-tls-hostname-verified",
        "redirect_count": max(0, len(fetched.hops) - 1),
        "hops": [asdict(hop) for hop in fetched.hops],
        "response_etag": fetched.etag,
        "response_last_modified": fetched.last_modified,
        "media_type": fetched.media_type,
        "artifact_sha256": fetched.sha256,
    }


def _write_immutable_artifact(directory: Path, digest: str, body: bytes) -> Path:
    if hashlib.sha256(body).hexdigest() != digest:
        raise OSError("artifact digest changed before storage")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.source"
    if target.exists():
        existing = target.read_bytes()
        if len(existing) != len(body) or hashlib.sha256(existing).hexdigest() != digest:
            raise OSError("content-addressed artifact is corrupt")
        return target
    temporary = directory / f".{digest}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise OSError("stored artifact failed digest verification")
    return target


async def _latest_revision(session: AsyncSession, source_id: str) -> RateSourceRevision | None:
    return cast(
        RateSourceRevision | None,
        await session.scalar(
            select(RateSourceRevision)
            .where(RateSourceRevision.source_id == source_id)
            .order_by(RateSourceRevision.retrieved_at.desc(), RateSourceRevision.id.desc())
            .limit(1)
        ),
    )


async def _get_or_create_revision(
    session: AsyncSession,
    *,
    source: RateSource,
    fetched: SourceFetch,
    target: Path,
) -> tuple[RateSourceRevision, bool]:
    assert fetched.sha256 is not None
    existing = await session.scalar(
        select(RateSourceRevision).where(
            RateSourceRevision.source_id == source.id,
            RateSourceRevision.artifact_sha256 == fetched.sha256,
        )
    )
    created = False
    if existing is None:
        revision = RateSourceRevision(
            source_id=source.id,
            artifact_sha256=fetched.sha256,
            etag=fetched.etag,
            last_modified=fetched.last_modified,
            parser_version=PARSER_VERSION,
        )
        try:
            async with session.begin_nested():
                session.add(revision)
                await session.flush()
            existing = revision
            created = True
        except IntegrityError:
            existing = await session.scalar(
                select(RateSourceRevision).where(
                    RateSourceRevision.source_id == source.id,
                    RateSourceRevision.artifact_sha256 == fetched.sha256,
                )
            )
            if existing is None:
                raise
    artifact = await session.scalar(
        select(RateSourceArtifact).where(RateSourceArtifact.revision_id == existing.id)
    )
    if artifact is None:
        artifact = RateSourceArtifact(
            revision_id=existing.id,
            storage_path=str(target),
            media_type=fetched.media_type or "application/octet-stream",
            byte_count=fetched.byte_count,
        )
        try:
            async with session.begin_nested():
                session.add(artifact)
                await session.flush()
        except IntegrityError:
            artifact = await session.scalar(
                select(RateSourceArtifact).where(RateSourceArtifact.revision_id == existing.id)
            )
            if artifact is None:
                raise
    return existing, created


async def _prior_candidate(
    session: AsyncSession,
    *,
    source_id: str,
    excluding_revision_id: str,
) -> RateCandidate | None:
    approved = await session.scalar(
        select(RateCandidate)
        .join(
            RateSourceRevision,
            RateSourceRevision.id == RateCandidate.source_revision_id,
        )
        .where(
            RateSourceRevision.source_id == source_id,
            RateCandidate.source_revision_id != excluding_revision_id,
            RateCandidate.state.in_(("approved", "published", "activated")),
        )
        .order_by(RateCandidate.created_at.desc(), RateCandidate.id.desc())
        .limit(1)
    )
    if approved is not None:
        return approved
    return cast(
        RateCandidate | None,
        await session.scalar(
            select(RateCandidate)
            .join(
                RateSourceRevision,
                RateSourceRevision.id == RateCandidate.source_revision_id,
            )
            .where(
                RateSourceRevision.source_id == source_id,
                RateCandidate.source_revision_id != excluding_revision_id,
            )
            .order_by(RateCandidate.created_at.desc(), RateCandidate.id.desc())
            .limit(1)
        ),
    )


async def _get_or_create_candidate(
    session: AsyncSession,
    *,
    source: RateSource,
    revision: RateSourceRevision,
    normalized_rates: dict[str, object],
    validation_evidence: dict[str, object],
) -> tuple[RateCandidate, bool]:
    existing = await session.scalar(
        select(RateCandidate).where(RateCandidate.source_revision_id == revision.id)
    )
    if existing is not None:
        return existing, False
    prior = await _prior_candidate(
        session,
        source_id=source.id,
        excluding_revision_id=revision.id,
    )
    diff = side_by_side_diff(
        prior.normalized_rates if prior is not None else None,
        normalized_rates,
        previous_candidate_id=prior.id if prior is not None else None,
    )
    candidate = RateCandidate(
        source_revision_id=revision.id,
        normalized_rates=normalized_rates,
        diff=diff,
        validation_evidence=validation_evidence,
        state="review_required",
    )
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(RateCandidate).where(RateCandidate.source_revision_id == revision.id)
        )
        if existing is None:
            raise
        return existing, False
    return candidate, True


def _complete_run(
    session: AsyncSession,
    *,
    run: RateSyncRun,
    source: RateSource,
    state: str,
    event_code: str,
    completed_at: datetime,
    actor_user_id: str | None,
    error_code: str | None = None,
    revision_id: str | None = None,
    candidate_id: str | None = None,
    extra_evidence: dict[str, object] | None = None,
) -> RateSyncResult:
    run.state = state
    run.event_code = event_code
    run.error_code = error_code
    run.revision_id = revision_id
    run.completed_at = completed_at
    run.evidence = {
        **run.evidence,
        **(extra_evidence or {}),
        "result": state,
        "event_code": event_code,
        "candidate_id": candidate_id,
    }
    source.last_checked_at = completed_at
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            event_code=event_code,
            target_type="rate_sync_run",
            target_id=run.id,
            correlation_id=run.correlation_id,
            details={
                "source_id": source.id,
                "home_id": run.home_id,
                "state": state,
                "revision_id": revision_id,
                "candidate_id": candidate_id,
                "error_code": error_code,
            },
        )
    )
    return RateSyncResult(
        run_id=run.id,
        state=state,
        event_code=event_code,
        revision_id=revision_id,
        candidate_id=candidate_id,
        error_code=error_code,
    )


async def sync_official_rate_source(
    session: AsyncSession,
    settings: Settings,
    source: RateSource,
    *,
    home_id: str | None,
    actor_user_id: str | None,
    correlation_id: str,
    fetcher: FetchCallable | None = None,
    now: datetime | None = None,
) -> RateSyncResult:
    if not source.enabled or source.source_type != "official_https" or not source.https_url:
        raise ValueError("only enabled official HTTPS rate sources can be synchronized")
    started_at = aware_utc(now or _utc_now())
    run = RateSyncRun(
        source_id=source.id,
        home_id=home_id,
        state="running",
        event_code="RATE_SYNC_STARTED",
        started_at=started_at,
        correlation_id=correlation_id[:80],
        requested_url=source.https_url,
        evidence={
            "initiator": "user" if actor_user_id else "scheduled_worker",
            "request_etag": source.current_etag,
            "request_last_modified": source.current_last_modified,
        },
    )
    session.add(run)
    await session.flush()
    fetch = fetcher or fetch_official_source
    try:
        fetched = await fetch(
            source.https_url,
            allowed_hosts=settings.allowed_sce_hosts,
            etag=source.current_etag,
            last_modified=source.current_last_modified,
            max_bytes=settings.rate_source_max_bytes,
            max_redirects=settings.rate_source_max_redirects,
            connect_timeout_seconds=settings.rate_source_connect_timeout_seconds,
            read_timeout_seconds=settings.rate_source_read_timeout_seconds,
            total_timeout_seconds=settings.rate_source_total_timeout_seconds,
            max_header_bytes=settings.rate_source_max_header_bytes,
            max_header_count=settings.rate_source_max_header_count,
        )
    except SourceFetchError as exc:
        evidence = {"failure_phase": "fetch", **exc.evidence}
        hops = evidence.get("hops")
        if isinstance(hops, list) and hops:
            final_hop = hops[-1]
            if isinstance(final_hop, dict):
                run.final_url = str(final_hop.get("url") or source.https_url)
                status = final_hop.get("status_code")
                run.http_status = status if isinstance(status, int) else None
        return _complete_run(
            session,
            run=run,
            source=source,
            state="failed",
            event_code="RATE_SYNC_FAILED",
            error_code=exc.error_code,
            completed_at=_utc_now(),
            actor_user_id=actor_user_id,
            extra_evidence=evidence,
        )
    except Exception:
        return _complete_run(
            session,
            run=run,
            source=source,
            state="failed",
            event_code="RATE_SYNC_FAILED",
            error_code="FETCH_INTERNAL_ERROR",
            completed_at=_utc_now(),
            actor_user_id=actor_user_id,
            extra_evidence={"failure_phase": "fetch"},
        )

    run.final_url = fetched.url
    run.http_status = fetched.status_code
    run.response_bytes = fetched.byte_count
    run.evidence = {**run.evidence, **_fetch_evidence(fetched)}
    source.current_etag = fetched.etag
    source.current_last_modified = fetched.last_modified
    latest = await _latest_revision(session, source.id)
    if fetched.status_code == 304:
        if latest is None:
            return _complete_run(
                session,
                run=run,
                source=source,
                state="failed",
                event_code="RATE_SYNC_FAILED",
                error_code="NOT_MODIFIED_WITHOUT_SNAPSHOT",
                completed_at=_utc_now(),
                actor_user_id=actor_user_id,
                extra_evidence={"failure_phase": "conditional_response"},
            )
        return _complete_run(
            session,
            run=run,
            source=source,
            state="unchanged",
            event_code="RATE_SOURCE_NOT_MODIFIED",
            revision_id=latest.id,
            completed_at=_utc_now(),
            actor_user_id=actor_user_id,
        )

    if fetched.body is None or fetched.sha256 is None or fetched.media_type is None:
        return _complete_run(
            session,
            run=run,
            source=source,
            state="failed",
            event_code="RATE_SYNC_FAILED",
            error_code="FETCH_EVIDENCE_INCOMPLETE",
            completed_at=_utc_now(),
            actor_user_id=actor_user_id,
            extra_evidence={"failure_phase": "response_validation"},
        )
    try:
        target = await asyncio.to_thread(
            _write_immutable_artifact,
            settings.rate_artifact_dir,
            fetched.sha256,
            fetched.body,
        )
    except OSError:
        return _complete_run(
            session,
            run=run,
            source=source,
            state="failed",
            event_code="RATE_SYNC_FAILED",
            error_code="ARTIFACT_STORAGE_FAILED",
            completed_at=_utc_now(),
            actor_user_id=actor_user_id,
            extra_evidence={"failure_phase": "artifact_storage"},
        )

    revision, revision_created = await _get_or_create_revision(
        session,
        source=source,
        fetched=fetched,
        target=target,
    )
    run.revision_id = revision.id
    existing_candidate = await session.scalar(
        select(RateCandidate).where(RateCandidate.source_revision_id == revision.id)
    )
    if not revision_created and existing_candidate is not None:
        return _complete_run(
            session,
            run=run,
            source=source,
            state="unchanged",
            event_code="RATE_SOURCE_CONTENT_UNCHANGED",
            revision_id=revision.id,
            candidate_id=existing_candidate.id,
            completed_at=_utc_now(),
            actor_user_id=actor_user_id,
            extra_evidence={"duplicate_http_200": True},
        )
    try:
        parsed = parse_sce_tou_public_page(fetched.body, fetched.media_type)
    except SourceParseError as exc:
        return _complete_run(
            session,
            run=run,
            source=source,
            state="failed",
            event_code="RATE_SYNC_PARSE_FAILED",
            error_code=exc.error_code,
            revision_id=revision.id,
            completed_at=_utc_now(),
            actor_user_id=actor_user_id,
            extra_evidence={
                "failure_phase": "parsing",
                "parser_version": PARSER_VERSION,
                "validation": exc.evidence,
                "manual_review_required": True,
            },
        )
    except Exception:
        return _complete_run(
            session,
            run=run,
            source=source,
            state="failed",
            event_code="RATE_SYNC_PARSE_FAILED",
            error_code="PARSER_INTERNAL_ERROR",
            revision_id=revision.id,
            completed_at=_utc_now(),
            actor_user_id=actor_user_id,
            extra_evidence={
                "failure_phase": "parsing",
                "parser_version": PARSER_VERSION,
                "manual_review_required": True,
            },
        )
    validation = {
        **parsed.validation_evidence,
        "source_artifact_sha256": revision.artifact_sha256,
        "source_revision_id": revision.id,
    }
    candidate, candidate_created = await _get_or_create_candidate(
        session,
        source=source,
        revision=revision,
        normalized_rates=parsed.normalized_rates,
        validation_evidence=validation,
    )
    return _complete_run(
        session,
        run=run,
        source=source,
        state="review_required" if candidate_created else "unchanged",
        event_code=(
            "RATE_SOURCE_CHANGED" if candidate_created else "RATE_SOURCE_CONTENT_UNCHANGED"
        ),
        revision_id=revision.id,
        candidate_id=candidate.id,
        completed_at=_utc_now(),
        actor_user_id=actor_user_id,
        extra_evidence={
            "parser_version": PARSER_VERSION,
            "candidate_validation": validation,
            "manual_approval_required": True,
        },
    )


async def sync_due_rate_sources(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
    fetcher: FetchCallable | None = None,
) -> dict[str, int]:
    checked_at = aware_utc(now or _utc_now())
    await ensure_default_sce_source(session)
    sources = (
        await session.scalars(
            select(RateSource)
            .where(
                RateSource.enabled.is_(True),
                RateSource.source_type == "official_https",
            )
            .order_by(RateSource.last_checked_at.asc().nullsfirst(), RateSource.id.asc())
            .limit(settings.rate_source_due_limit)
        )
    ).all()
    due = [
        source
        for source in sources
        if source.last_checked_at is None
        or aware_utc(source.last_checked_at)
        <= checked_at - timedelta(hours=source.check_interval_hours)
    ]
    stats = {"checked": 0, "failed": 0, "review_required": 0, "unchanged": 0}
    for source in due:
        result = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id=f"rate-sync-{uuid.uuid4()}",
            fetcher=fetcher,
            now=checked_at,
        )
        stats["checked"] += 1
        stats[result.state if result.state in stats else "failed"] += 1
    return stats


__all__ = [
    "SCE_SOURCE_NAME",
    "SCE_TOU_URL",
    "RateSyncResult",
    "ensure_default_sce_source",
    "sync_due_rate_sources",
    "sync_official_rate_source",
]
