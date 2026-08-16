from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import cast

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import DEFAULT_SCE_RATE_SOURCE_URL, Settings
from ..errors import RateSyncBusy
from ..models import (
    AuditEvent,
    Home,
    RateCandidate,
    RateCandidateReview,
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

SCE_TOU_URL = DEFAULT_SCE_RATE_SOURCE_URL
SCE_SOURCE_NAME = "SCE residential rate-plan public page"

FetchCallable = Callable[..., Awaitable[SourceFetch]]
_LOCAL_SOURCE_LEASES: set[str] = set()
_LOCAL_SOURCE_LEASE_GUARD = Lock()
_TRANSIENT_FETCH_CODES = frozenset(
    {
        "TOTAL_TIMEOUT",
        "CONNECT_TIMEOUT",
        "READ_TIMEOUT",
        "WRITE_TIMEOUT",
        "POOL_TIMEOUT",
        "CONNECT_FAILED",
        "READ_FAILED",
        "WRITE_FAILED",
        "HTTP_PROTOCOL_ERROR",
        "NETWORK_FAILED",
        "DNS_FAILED",
    }
)
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


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


def _transient_fetch_failure(exc: SourceFetchError) -> bool:
    if exc.error_code in _TRANSIENT_FETCH_CODES:
        return True
    if exc.error_code != "HTTP_STATUS_REJECTED":
        return False
    hops = exc.evidence.get("hops")
    if not isinstance(hops, list) or not hops or not isinstance(hops[-1], dict):
        return False
    return hops[-1].get("status_code") in _TRANSIENT_HTTP_STATUSES


@asynccontextmanager
async def _rate_source_lease(session: AsyncSession, source_id: str) -> AsyncIterator[RateSource]:
    """Serialize network refreshes in-process and across PostgreSQL workers."""

    with _LOCAL_SOURCE_LEASE_GUARD:
        if source_id in _LOCAL_SOURCE_LEASES:
            raise RateSyncBusy("a refresh for this rate source is already running")
        _LOCAL_SOURCE_LEASES.add(source_id)
    try:
        if session.get_bind().dialect.name == "postgresql":
            acquired = await session.scalar(
                text(
                    "SELECT pg_try_advisory_xact_lock("
                    "hashtextextended('powermeter-rate-source:' || :source_id, 0))"
                ),
                {"source_id": source_id},
            )
            if acquired is not True:
                raise RateSyncBusy("a refresh for this rate source is already running")
        locked = await session.scalar(
            select(RateSource).where(RateSource.id == source_id).with_for_update()
        )
        if locked is None:
            raise ValueError("rate source does not exist")
        yield locked
    finally:
        with _LOCAL_SOURCE_LEASE_GUARD:
            _LOCAL_SOURCE_LEASES.discard(source_id)


async def ensure_default_sce_source(
    session: AsyncSession,
    source_url: str = SCE_TOU_URL,
) -> RateSource:
    source = await session.scalar(select(RateSource).where(RateSource.https_url == source_url))
    if source is not None:
        return source
    managed = await session.scalar(
        select(RateSource)
        .where(
            RateSource.name == SCE_SOURCE_NAME,
            RateSource.source_type == "official_https",
        )
        .order_by(RateSource.id)
        .with_for_update()
        .limit(1)
    )
    if managed is not None:
        managed.https_url = source_url
        managed.enabled = True
        await session.flush()
        return managed
    candidate = RateSource(
        name=SCE_SOURCE_NAME,
        source_type="official_https",
        https_url=source_url,
        enabled=True,
        check_interval_hours=168,
    )
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(RateSource).where(RateSource.https_url == source_url)
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
        _fsync_directory(directory)
        return target
    temporary = directory / f".{digest}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise OSError("stored artifact failed digest verification")
    return target


def _fsync_directory(directory: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        # Windows has no supported directory-fsync primitive. Production images
        # are Linux, where the renamed directory entry must be made durable.
        return
    descriptor = os.open(directory, os.O_RDONLY | directory_flag)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
    home_id: str | None,
) -> RateCandidate | None:
    approved_statement = (
        select(RateCandidate)
        .join(
            RateSourceRevision,
            RateSourceRevision.id == RateCandidate.source_revision_id,
        )
        .join(
            RateCandidateReview,
            RateCandidateReview.candidate_id == RateCandidate.id,
        )
        .where(
            RateSourceRevision.source_id == source_id,
            RateCandidate.source_revision_id != excluding_revision_id,
            RateCandidateReview.state.in_(("published", "activated")),
        )
        .order_by(
            RateCandidateReview.published_at.desc(),
            RateCandidate.created_at.desc(),
            RateCandidate.id.desc(),
        )
        .limit(1)
    )
    if home_id is not None:
        approved_statement = approved_statement.where(RateCandidateReview.home_id == home_id)
    approved = await session.scalar(approved_statement)
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
    home_id: str | None,
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
        home_id=home_id,
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


async def _sync_official_rate_source_locked(
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
    source_url = source.https_url
    started_at = aware_utc(now or _utc_now())
    run = RateSyncRun(
        source_id=source.id,
        home_id=home_id,
        state="running",
        event_code="RATE_SYNC_STARTED",
        started_at=started_at,
        correlation_id=correlation_id[:80],
        requested_url=source_url,
        evidence={
            "initiator": "user" if actor_user_id else "scheduled_worker",
            "request_etag": source.current_etag,
            "request_last_modified": source.current_last_modified,
        },
    )
    session.add(run)
    await session.flush()
    fetch = fetcher or fetch_official_source
    fetch_attempts = 0
    transient_retry_codes: list[str] = []

    async def fetch_with_validators(etag: str | None, last_modified: str | None) -> SourceFetch:
        nonlocal fetch_attempts
        for attempt in range(settings.rate_source_retry_attempts):
            fetch_attempts += 1
            try:
                return await fetch(
                    source_url,
                    allowed_hosts=settings.allowed_sce_hosts,
                    etag=etag,
                    last_modified=last_modified,
                    max_bytes=settings.rate_source_max_bytes,
                    max_redirects=settings.rate_source_max_redirects,
                    connect_timeout_seconds=settings.rate_source_connect_timeout_seconds,
                    read_timeout_seconds=settings.rate_source_read_timeout_seconds,
                    total_timeout_seconds=settings.rate_source_total_timeout_seconds,
                    max_header_bytes=settings.rate_source_max_header_bytes,
                    max_header_count=settings.rate_source_max_header_count,
                )
            except SourceFetchError as exc:
                if not _transient_fetch_failure(exc) or (
                    attempt + 1 >= settings.rate_source_retry_attempts
                ):
                    exc.evidence = {
                        **exc.evidence,
                        "fetch_attempts": fetch_attempts,
                        "transient_retry_codes": list(transient_retry_codes),
                    }
                    raise
                transient_retry_codes.append(exc.error_code)
                await asyncio.sleep(settings.rate_source_retry_backoff_seconds * (2**attempt))
        raise RuntimeError("bounded rate-source retry loop did not terminate")

    try:
        fetched = await fetch_with_validators(source.current_etag, source.current_last_modified)
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
            extra_evidence={
                "failure_phase": "fetch",
                "fetch_attempts": fetch_attempts,
                "transient_retry_codes": transient_retry_codes,
            },
        )

    run.final_url = fetched.url
    run.http_status = fetched.status_code
    run.response_bytes = fetched.byte_count
    run.evidence = {
        **run.evidence,
        **_fetch_evidence(fetched),
        "fetch_attempts": fetch_attempts,
        "transient_retry_codes": transient_retry_codes,
    }
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
        latest_candidate = await session.scalar(
            select(RateCandidate).where(RateCandidate.source_revision_id == latest.id)
        )
        if latest_candidate is None:
            conditional_evidence = _fetch_evidence(fetched)
            run.evidence = {
                **run.evidence,
                "conditional_recovery": {
                    "reason": "latest_revision_has_no_parsed_candidate",
                    "conditional_response": conditional_evidence,
                },
            }
            try:
                fetched = await fetch_with_validators(None, None)
            except SourceFetchError as exc:
                evidence = {
                    "failure_phase": "conditional_recovery_fetch",
                    **exc.evidence,
                }
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
                    extra_evidence={
                        "failure_phase": "conditional_recovery_fetch",
                        "fetch_attempts": fetch_attempts,
                        "transient_retry_codes": transient_retry_codes,
                    },
                )
            run.final_url = fetched.url
            run.http_status = fetched.status_code
            run.response_bytes = fetched.byte_count
            run.evidence = {
                **run.evidence,
                **_fetch_evidence(fetched),
                "fetch_attempts": fetch_attempts,
                "transient_retry_codes": transient_retry_codes,
            }
            if fetched.status_code == 304:
                return _complete_run(
                    session,
                    run=run,
                    source=source,
                    state="failed",
                    event_code="RATE_SYNC_FAILED",
                    error_code="NOT_MODIFIED_WITHOUT_PARSED_SNAPSHOT",
                    revision_id=latest.id,
                    completed_at=_utc_now(),
                    actor_user_id=actor_user_id,
                    extra_evidence={"failure_phase": "conditional_recovery_response"},
                )
        else:
            if fetched.etag is not None:
                source.current_etag = fetched.etag
            if fetched.last_modified is not None:
                source.current_last_modified = fetched.last_modified
            return _complete_run(
                session,
                run=run,
                source=source,
                state="unchanged",
                event_code="RATE_SOURCE_NOT_MODIFIED",
                revision_id=latest.id,
                candidate_id=latest_candidate.id,
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
        source.current_etag = fetched.etag
        source.current_last_modified = fetched.last_modified
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
        parsed = await asyncio.to_thread(
            parse_sce_tou_public_page,
            fetched.body,
            fetched.media_type,
        )
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
        home_id=home_id,
    )
    source.current_etag = fetched.etag
    source.current_last_modified = fetched.last_modified
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
    async with _rate_source_lease(session, source.id) as locked_source:
        try:
            async with asyncio.timeout(settings.rate_source_operation_timeout_seconds):
                return await _sync_official_rate_source_locked(
                    session,
                    settings,
                    locked_source,
                    home_id=home_id,
                    actor_user_id=actor_user_id,
                    correlation_id=correlation_id,
                    fetcher=fetcher,
                    now=now,
                )
        except TimeoutError:
            run = await session.scalar(
                select(RateSyncRun)
                .where(
                    RateSyncRun.source_id == locked_source.id,
                    RateSyncRun.home_id == home_id,
                    RateSyncRun.correlation_id == correlation_id[:80],
                    RateSyncRun.state == "running",
                )
                .order_by(RateSyncRun.started_at.desc(), RateSyncRun.id.desc())
                .limit(1)
            )
            if run is None:
                run = RateSyncRun(
                    source_id=locked_source.id,
                    home_id=home_id,
                    state="running",
                    event_code="RATE_SYNC_STARTED",
                    started_at=aware_utc(now or _utc_now()),
                    correlation_id=correlation_id[:80],
                    requested_url=locked_source.https_url or SCE_TOU_URL,
                    evidence={
                        "initiator": "user" if actor_user_id else "scheduled_worker",
                    },
                )
                session.add(run)
                await session.flush()
            return _complete_run(
                session,
                run=run,
                source=locked_source,
                state="failed",
                event_code="RATE_SYNC_FAILED",
                error_code="OPERATION_TIMEOUT",
                completed_at=_utc_now(),
                actor_user_id=actor_user_id,
                extra_evidence={
                    "failure_phase": "operation_deadline",
                    "operation_timeout_seconds": settings.rate_source_operation_timeout_seconds,
                    "retry_attempt_limit": settings.rate_source_retry_attempts,
                },
            )


async def _copy_scheduled_run_to_home(
    session: AsyncSession,
    *,
    source_run: RateSyncRun,
    home_id: str,
) -> None:
    copied = RateSyncRun(
        source_id=source_run.source_id,
        home_id=home_id,
        state=source_run.state,
        event_code=source_run.event_code,
        started_at=source_run.started_at,
        completed_at=source_run.completed_at,
        revision_id=source_run.revision_id,
        correlation_id=f"rate-sync-{uuid.uuid4()}",
        requested_url=source_run.requested_url,
        final_url=source_run.final_url,
        http_status=source_run.http_status,
        response_bytes=source_run.response_bytes,
        error_code=source_run.error_code,
        evidence={
            **source_run.evidence,
            "initiator": "scheduled_worker",
            "shared_source_run_id": source_run.id,
        },
    )
    session.add(copied)
    await session.flush()
    session.add(
        AuditEvent(
            actor_user_id=None,
            event_code=source_run.event_code,
            target_type="rate_sync_run",
            target_id=copied.id,
            correlation_id=copied.correlation_id,
            details={
                "source_id": copied.source_id,
                "home_id": home_id,
                "state": copied.state,
                "revision_id": copied.revision_id,
                "candidate_id": copied.evidence.get("candidate_id"),
                "error_code": copied.error_code,
                "shared_source_run_id": source_run.id,
            },
        )
    )


async def sync_due_rate_sources(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
    fetcher: FetchCallable | None = None,
) -> dict[str, int]:
    checked_at = aware_utc(now or _utc_now())
    await ensure_default_sce_source(session, str(settings.sce_rate_source_url))
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
    home_ids = tuple((await session.scalars(select(Home.id).order_by(Home.id))).all())
    stats = {"checked": 0, "failed": 0, "review_required": 0, "unchanged": 0}
    for source in due:
        try:
            result = await sync_official_rate_source(
                session,
                settings,
                source,
                home_id=home_ids[0] if home_ids else None,
                actor_user_id=None,
                correlation_id=f"rate-sync-{uuid.uuid4()}",
                fetcher=fetcher,
                now=checked_at,
            )
        except RateSyncBusy:
            stats["checked"] += 1
            stats["failed"] += 1
            continue
        if home_ids:
            source_run = await session.get(RateSyncRun, result.run_id)
            if source_run is None:
                raise RuntimeError("completed scheduled rate run disappeared")
            for home_id in home_ids[1:]:
                await _copy_scheduled_run_to_home(
                    session,
                    source_run=source_run,
                    home_id=home_id,
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
