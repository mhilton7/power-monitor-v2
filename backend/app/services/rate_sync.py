from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, cast
from urllib.parse import urlparse

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
    SceCatalogEntry,
    aware_utc,
)
from .rate_sources import SourceFetch, SourceFetchError, fetch_official_source
from .sce_catalog import (
    CATALOG_CRAWLER_VERSION,
    OFFICIAL_SCE_PUBLIC_PATH_PREFIXES,
    DiscoveredSceCatalogLink,
    DiscoveredScePlan,
    discover_sce_catalog,
    discovered_plan_from_link,
    inspect_sce_catalog_links,
)
from .sce_rate_parser import (
    PARSER_VERSION,
    ParsedRateCandidate,
    SourceParseError,
    side_by_side_diff,
)
from .sce_rate_parser import (
    parse_sce_tou_public_page as parse_sce_tou_public_page,
)

SCE_TOU_URL = DEFAULT_SCE_RATE_SOURCE_URL
SCE_SOURCE_NAME = "SCE residential rate-plan public page"
SCE_CATALOG_URL = "https://www.sce.com/save-money/rates-financing/residential-rate-plans"
SCE_CATALOG_SOURCE_NAME = "SCE residential rate catalog inventory"

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


@dataclass(frozen=True)
class CatalogCrawlResult:
    entries: tuple[DiscoveredScePlan, ...]
    manifest: dict[str, Any]
    manifest_body: bytes
    complete: bool


async def _record_catalog_discovery(
    session: AsyncSession,
    *,
    revision: RateSourceRevision,
    source_url: str,
    body: bytes,
    media_type: str,
    parsed: ParsedRateCandidate | None,
) -> dict[str, int | None]:
    discovered = discover_sce_catalog(
        body,
        media_type,
        source_url=source_url,
        parsed=parsed,
    )
    return await _record_catalog_entries(session, revision=revision, discovered=discovered)


async def _record_catalog_entries(
    session: AsyncSession,
    *,
    revision: RateSourceRevision,
    discovered: Iterable[DiscoveredScePlan],
) -> dict[str, int | None]:
    items = tuple(discovered)
    counts = {"parsed": 0, "requires_parser": 0, "excluded": 0}
    for item in items:
        existing = await session.scalar(
            select(SceCatalogEntry).where(
                SceCatalogEntry.source_revision_id == revision.id,
                SceCatalogEntry.canonical_name == item.canonical_name,
            )
        )
        if existing is None:
            existing = SceCatalogEntry(
                source_revision_id=revision.id,
                source_url=item.source_url,
                public_plan_name=item.public_plan_name,
                canonical_name=item.canonical_name,
                discovery_state=item.discovery_state,
            )
            session.add(existing)
        existing.source_url = item.source_url
        existing.source_level = item.source_level
        existing.public_plan_name = item.public_plan_name
        existing.official_schedule_code = item.official_schedule_code
        existing.plan_type = item.plan_type
        existing.enrollment_status = item.enrollment_status
        existing.eligibility = list(item.eligibility)
        existing.discovery_state = item.discovery_state
        existing.exclusion_reason = item.exclusion_reason
        existing.normalized_plan = item.normalized_plan
        existing.updated_at = _utc_now()
        counts[item.discovery_state] += 1
    await session.flush()
    return {"discovered": len(items), **counts, "silently_omitted": None}


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


async def ensure_default_sce_catalog_source(session: AsyncSession) -> RateSource:
    source = await session.scalar(
        select(RateSource)
        .where(
            RateSource.name == SCE_CATALOG_SOURCE_NAME,
            RateSource.source_type == "official_https",
        )
        .with_for_update()
        .limit(1)
    )
    if source is not None:
        if source.https_url != SCE_CATALOG_URL:
            collision = await session.scalar(
                select(RateSource.id).where(
                    RateSource.https_url == SCE_CATALOG_URL,
                    RateSource.id != source.id,
                )
            )
            if collision is not None:
                raise ValueError("official SCE catalog source URL is already owned")
        source.https_url = SCE_CATALOG_URL
        source.enabled = True
        source.check_interval_hours = 168
        await session.flush()
        return source
    source = await session.scalar(
        select(RateSource).where(RateSource.https_url == SCE_CATALOG_URL).with_for_update()
    )
    if source is not None:
        source.name = SCE_CATALOG_SOURCE_NAME
        source.source_type = "official_https"
        source.enabled = True
        source.check_interval_hours = 168
        await session.flush()
        return cast(RateSource, source)
    candidate = RateSource(
        name=SCE_CATALOG_SOURCE_NAME,
        source_type="official_https",
        https_url=SCE_CATALOG_URL,
        enabled=True,
        check_interval_hours=168,
    )
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(RateSource).where(RateSource.https_url == SCE_CATALOG_URL)
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


async def _latest_catalog_root_revision(
    session: AsyncSession,
    source_id: str,
) -> RateSourceRevision | None:
    runs = (
        await session.scalars(
            select(RateSyncRun)
            .where(
                RateSyncRun.source_id == source_id,
                RateSyncRun.revision_id.is_not(None),
                RateSyncRun.event_code == "SCE_CATALOG_CRAWL_COMPLETE",
            )
            .order_by(RateSyncRun.started_at.desc(), RateSyncRun.id.desc())
            .limit(50)
        )
    ).all()
    for run in runs:
        manifest = run.evidence.get("catalog_crawl_manifest")
        if isinstance(manifest, dict):
            closure = manifest.get("closure")
            if (
                not isinstance(closure, dict)
                or closure.get("proved") is not True
                or closure.get("plans_silently_omitted") != 0
            ):
                continue
            root_revision_id = manifest.get("root_revision_id")
            if isinstance(root_revision_id, str):
                revision = await session.get(RateSourceRevision, root_revision_id)
                if revision is not None and revision.source_id == source_id:
                    return revision
    return None


def _read_immutable_artifact(
    directory: Path,
    artifact: RateSourceArtifact,
    revision: RateSourceRevision,
) -> bytes:
    root = directory.resolve()
    stored = Path(artifact.storage_path)
    if stored.is_symlink():
        raise OSError("rate-source artifact path cannot be a symlink")
    target = stored.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise OSError("rate-source artifact path is outside the artifact directory")
    body = target.read_bytes()
    if (
        len(body) != artifact.byte_count
        or hashlib.sha256(body).hexdigest() != revision.artifact_sha256
    ):
        raise OSError("rate-source artifact failed integrity verification")
    return body


async def _captured_revision_fetch(
    session: AsyncSession,
    settings: Settings,
    revision: RateSourceRevision,
    *,
    source_url: str,
) -> SourceFetch:
    artifact = await session.scalar(
        select(RateSourceArtifact).where(RateSourceArtifact.revision_id == revision.id)
    )
    if artifact is None:
        raise OSError("rate-source revision has no captured artifact")
    body = await asyncio.to_thread(
        _read_immutable_artifact,
        settings.rate_artifact_dir,
        artifact,
        revision,
    )
    return SourceFetch(
        requested_url=source_url,
        url=source_url,
        status_code=200,
        body=body,
        sha256=revision.artifact_sha256,
        etag=revision.etag,
        last_modified=revision.last_modified,
        media_type=artifact.media_type,
        hops=(),
    )


async def _get_or_create_revision(
    session: AsyncSession,
    *,
    source: RateSource,
    fetched: SourceFetch,
    target: Path,
    parser_version: str = PARSER_VERSION,
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
            parser_version=parser_version,
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


def _allowed_catalog_url(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    normalized_hosts = {host.lower().rstrip(".") for host in allowed_hosts}
    return (
        parsed.scheme.lower() == "https"
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and not parsed.fragment
        and (parsed.hostname or "").lower().rstrip(".") in normalized_hosts
        and parsed.path.lower().startswith(OFFICIAL_SCE_PUBLIC_PATH_PREFIXES)
    )


def _known_non_candidate_catalog_layout(link: DiscoveredSceCatalogLink) -> bool:
    path = urlparse(link.target_url).path.lower().rstrip("/")
    return path.endswith("/rate-plan-comparison")


def _merge_catalog_entry(
    entries: dict[str, DiscoveredScePlan],
    item: DiscoveredScePlan,
) -> None:
    priority = {"requires_parser": 0, "excluded": 1, "parsed": 2}
    existing = entries.get(item.canonical_name)
    if existing is None or priority[item.discovery_state] > priority[existing.discovery_state]:
        entries[item.canonical_name] = item
        return
    if (
        priority[item.discovery_state] == priority[existing.discovery_state]
        and item.source_level < existing.source_level
    ):
        entries[item.canonical_name] = item


def _parsed_catalog_names(parsed: ParsedRateCandidate | None) -> frozenset[str]:
    if parsed is None:
        return frozenset()
    plans = parsed.normalized_rates.get("plans")
    if not isinstance(plans, list):
        return frozenset()
    names: set[str] = set()
    for item in discover_sce_catalog(
        b"<html></html>",
        "text/html",
        source_url=SCE_CATALOG_URL,
        parsed=parsed,
    ):
        if item.discovery_state == "parsed":
            names.add(item.canonical_name)
    return frozenset(names)


async def _parse_catalog_document(
    body: bytes,
    media_type: str,
    *,
    timeout_seconds: float,
) -> tuple[ParsedRateCandidate | None, str | None]:
    if media_type not in {"text/html", "application/xhtml+xml"}:
        return None, "MEDIA_TYPE_REQUIRES_DEDICATED_PARSER"
    try:
        async with asyncio.timeout(timeout_seconds):
            parsed = await asyncio.to_thread(parse_sce_tou_public_page, body, media_type)
        return parsed, None
    except TimeoutError:
        return None, "PARSER_TIMEOUT"
    except SourceParseError as exc:
        return None, exc.error_code
    except Exception:
        return None, "PARSER_INTERNAL_ERROR"


def _catalog_link_record(
    link: DiscoveredSceCatalogLink,
    *,
    depth: int,
) -> dict[str, Any]:
    return {
        "source_url": link.source_url,
        "url": link.target_url,
        "label": link.label,
        "canonical_name": link.canonical_name,
        "official_schedule_code": link.official_schedule_code,
        "source_level": link.source_level,
        "kind": link.kind,
        "depth": depth,
        "resolution": "pending",
        "reason": link.exclusion_reason,
        "artifact_sha256": None,
        "artifact_revision_id": None,
    }


async def _crawl_sce_catalog(
    session: AsyncSession,
    settings: Settings,
    source: RateSource,
    *,
    root_fetch: SourceFetch,
    root_revision: RateSourceRevision,
    fetch_document: Callable[[str, int], Awaitable[SourceFetch]],
) -> CatalogCrawlResult:
    """Capture and close a sequential, resource-bounded official SCE crawl."""

    if root_fetch.body is None or root_fetch.media_type is None or root_fetch.sha256 is None:
        raise ValueError("catalog crawl root must be a complete captured response")
    root_url = root_fetch.url
    root_inspection = inspect_sce_catalog_links(
        root_fetch.body,
        root_fetch.media_type,
        source_url=root_url,
    )
    root_links = root_inspection.links
    root_parsed: ParsedRateCandidate | None = None

    entries: dict[str, DiscoveredScePlan] = {}
    for item in discover_sce_catalog(
        root_fetch.body,
        root_fetch.media_type,
        source_url=root_url,
        parsed=root_parsed,
    ):
        _merge_catalog_entry(entries, item)

    documents: list[dict[str, Any]] = [
        {
            "url": root_url,
            "depth": 0,
            "media_type": root_fetch.media_type,
            "artifact_sha256": root_fetch.sha256,
            "artifact_revision_id": root_revision.id,
            "classification": (
                "catalog_index"
                if root_links and root_inspection.error_code is None
                else "unresolved_empty_catalog"
            ),
            "parser_error_code": (
                root_inspection.error_code
                or ("CATALOG_INDEX_NO_LINKS_EXTRACTED" if not root_links else None)
            ),
        }
    ]
    total_bytes = root_fetch.byte_count
    link_records: dict[str, dict[str, Any]] = {}
    queue: deque[tuple[DiscoveredSceCatalogLink, int]] = deque()
    seen_documents = {root_url}
    document_attempts = 1

    def add_links(links: Iterable[DiscoveredSceCatalogLink], depth: int) -> None:
        for link in links:
            if link.target_url in link_records:
                continue
            record = _catalog_link_record(link, depth=depth)
            link_records[link.target_url] = record
            if link.kind == "excluded":
                record["resolution"] = "explicitly_excluded"
                record["reason"] = link.exclusion_reason
                _merge_catalog_entry(
                    entries,
                    discovered_plan_from_link(
                        link,
                        discovery_state="excluded",
                        exclusion_reason=link.exclusion_reason,
                    ),
                )
                continue
            if link.kind == "plan":
                _merge_catalog_entry(
                    entries,
                    discovered_plan_from_link(link, discovery_state="requires_parser"),
                )
            if link.target_url == root_url:
                if link.kind == "traversal":
                    record["resolution"] = "explicitly_excluded"
                    record["reason"] = "CATALOG_INDEX_TRAVERSAL_ACCOUNTED_FOR"
                elif link.canonical_name in _parsed_catalog_names(root_parsed):
                    record["resolution"] = "parsed"
                else:
                    record["resolution"] = "requires_parser"
                    record["reason"] = "NO_MATCHING_NORMALIZED_PLAN"
                record["artifact_sha256"] = root_fetch.sha256
                record["artifact_revision_id"] = root_revision.id
                continue
            queue.append((link, depth))

    add_links(root_links, 1)
    limit_reasons: set[str] = set()
    if root_inspection.error_code is not None:
        limit_reasons.add("root_catalog_index_parse_failed")
    if not root_links:
        limit_reasons.add("root_catalog_index_no_links")
    while queue:
        link, depth = queue.popleft()
        record = link_records[link.target_url]
        if link.target_url in seen_documents:
            continue
        if depth > settings.sce_catalog_crawl_max_depth:
            record["resolution"] = "depth_limit_exceeded"
            record["reason"] = "CATALOG_CRAWL_DEPTH_LIMIT"
            limit_reasons.add("depth_limit_reached")
            continue
        if document_attempts >= settings.sce_catalog_crawl_max_documents:
            record["resolution"] = "document_limit_exceeded"
            record["reason"] = "CATALOG_CRAWL_DOCUMENT_LIMIT"
            limit_reasons.add("document_limit_reached")
            continue
        remaining_bytes = settings.sce_catalog_crawl_max_total_bytes - total_bytes
        if remaining_bytes <= 0:
            record["resolution"] = "total_byte_limit_exceeded"
            record["reason"] = "CATALOG_CRAWL_TOTAL_BYTE_LIMIT"
            limit_reasons.add("total_byte_limit_reached")
            continue
        seen_documents.add(link.target_url)
        document_attempts += 1
        try:
            if settings.sce_catalog_crawl_request_delay_seconds:
                await asyncio.sleep(settings.sce_catalog_crawl_request_delay_seconds)
            fetched = await fetch_document(
                link.target_url,
                min(settings.rate_source_max_bytes, remaining_bytes),
            )
        except SourceFetchError as exc:
            record["resolution"] = "fetch_failed"
            record["reason"] = exc.error_code
            continue
        except Exception:
            record["resolution"] = "fetch_failed"
            record["reason"] = "FETCH_INTERNAL_ERROR"
            continue
        if (
            fetched.status_code != 200
            or fetched.body is None
            or fetched.sha256 is None
            or fetched.media_type is None
            or not _allowed_catalog_url(fetched.url, settings.allowed_sce_hosts)
            or hashlib.sha256(fetched.body).hexdigest() != fetched.sha256
        ):
            record["resolution"] = "fetch_evidence_invalid"
            record["reason"] = "FETCH_EVIDENCE_INCOMPLETE"
            continue
        if fetched.byte_count > remaining_bytes:
            record["resolution"] = "total_byte_limit_exceeded"
            record["reason"] = "CATALOG_CRAWL_TOTAL_BYTE_LIMIT"
            limit_reasons.add("total_byte_limit_reached")
            continue
        total_bytes += fetched.byte_count
        try:
            target = await asyncio.to_thread(
                _write_immutable_artifact,
                settings.rate_artifact_dir,
                fetched.sha256,
                fetched.body,
            )
            child_revision, _created = await _get_or_create_revision(
                session,
                source=source,
                fetched=fetched,
                target=target,
            )
        except OSError:
            record["resolution"] = "artifact_storage_failed"
            record["reason"] = "ARTIFACT_STORAGE_FAILED"
            continue
        record["artifact_sha256"] = fetched.sha256
        record["artifact_revision_id"] = child_revision.id
        document_record: dict[str, Any] = {
            "url": fetched.url,
            "depth": depth,
            "media_type": fetched.media_type,
            "artifact_sha256": fetched.sha256,
            "artifact_revision_id": child_revision.id,
            "classification": "pending",
            "parser_error_code": None,
        }
        documents.append(document_record)
        if fetched.media_type not in {"text/html", "application/xhtml+xml"}:
            if link.kind == "traversal" and not _known_non_candidate_catalog_layout(link):
                record["resolution"] = "requires_parser"
                record["reason"] = "CATALOG_INDEX_MEDIA_TYPE"
                document_record["classification"] = "unresolved_catalog_index"
                document_record["parser_error_code"] = "CATALOG_INDEX_MEDIA_TYPE"
            else:
                record["resolution"] = "explicitly_excluded"
                record["reason"] = "OFFICIAL_DOCUMENT_MEDIA_TYPE_UNSUPPORTED"
                document_record["classification"] = "explicitly_excluded"
                document_record["parser_error_code"] = "MEDIA_TYPE_REQUIRES_DEDICATED_PARSER"
            if link.kind == "plan":
                _merge_catalog_entry(
                    entries,
                    discovered_plan_from_link(
                        link,
                        discovery_state="excluded",
                        exclusion_reason="OFFICIAL_DOCUMENT_MEDIA_TYPE_UNSUPPORTED",
                    ),
                )
            continue

        nested_inspection = inspect_sce_catalog_links(
            fetched.body,
            fetched.media_type,
            source_url=fetched.url,
        )
        nested_links = nested_inspection.links
        add_links(nested_links, depth + 1)
        if link.kind == "traversal":
            if _known_non_candidate_catalog_layout(link):
                record["resolution"] = "explicitly_excluded"
                record["reason"] = "LAYOUT_ONLY_NON_CANDIDATE"
                document_record["classification"] = "layout_only_non_candidate"
                document_record["parser_error_code"] = None
            elif nested_inspection.error_code is not None:
                record["resolution"] = "requires_parser"
                record["reason"] = nested_inspection.error_code
                document_record["classification"] = "unresolved_catalog_index"
                document_record["parser_error_code"] = nested_inspection.error_code
            elif not nested_links:
                record["resolution"] = "requires_parser"
                record["reason"] = "CATALOG_INDEX_NO_LINKS_EXTRACTED"
                document_record["classification"] = "unresolved_catalog_index"
                document_record["parser_error_code"] = "CATALOG_INDEX_NO_LINKS_EXTRACTED"
            else:
                record["resolution"] = "explicitly_excluded"
                record["reason"] = "CATALOG_INDEX_TRAVERSAL_ACCOUNTED_FOR"
                document_record["classification"] = "catalog_index"
                document_record["parser_error_code"] = None
            continue

        parsed, parser_error = await _parse_catalog_document(
            fetched.body,
            fetched.media_type,
            timeout_seconds=settings.sce_catalog_parse_timeout_seconds,
        )
        parsed_names = _parsed_catalog_names(parsed)
        if not parsed_names:
            parsed = None
        for item in discover_sce_catalog(
            fetched.body,
            fetched.media_type,
            source_url=fetched.url,
            parsed=parsed,
        ):
            _merge_catalog_entry(entries, item)
        if link.canonical_name in parsed_names:
            record["resolution"] = "parsed"
            record["reason"] = None
            document_record["classification"] = "parsed_plan"
        else:
            record["resolution"] = "requires_parser"
            record["reason"] = parser_error or "NO_MATCHING_NORMALIZED_PLAN"
            document_record["classification"] = "requires_parser"
            document_record["parser_error_code"] = record["reason"]

    ordered_entries = tuple(entries[name] for name in sorted(entries))
    accepted_resolutions = {
        "parsed",
        "explicitly_excluded",
    }
    unresolved_links = sorted(
        record["url"]
        for record in link_records.values()
        if record["resolution"] not in accepted_resolutions
    )
    parser_attention = sorted(
        item.canonical_name for item in ordered_entries if item.discovery_state == "requires_parser"
    )
    failure_reasons = set(limit_reasons)
    if total_bytes > settings.sce_catalog_crawl_max_total_bytes:
        failure_reasons.add("total_byte_limit_reached")
    if unresolved_links:
        failure_reasons.add("unresolved_discovered_links")
    if parser_attention:
        failure_reasons.add("plans_require_parser_updates")
    if not ordered_entries:
        failure_reasons.add("no_residential_plans_discovered")
    complete = not failure_reasons
    for record in link_records.values():
        resolution = record["resolution"]
        record["discovery_status"] = (
            "accounted_for" if resolution in accepted_resolutions else "incomplete"
        )
        record["exact_tariff_status"] = (
            "public_plan_normalized_tariff_confirmation_required"
            if resolution == "parsed"
            else "dedicated_parser_required"
            if resolution == "explicitly_excluded"
            and record["reason"] != "LAYOUT_ONLY_NON_CANDIDATE"
            else "not_applicable"
            if resolution == "explicitly_excluded"
            else "unresolved"
        )
    plan_summaries = [
        {
            "canonical_name": item.canonical_name,
            "source_url": item.source_url,
            "discovery_state": item.discovery_state,
            "exclusion_reason": item.exclusion_reason,
            "exact_tariff_status": (
                "public_plan_normalized_tariff_confirmation_required"
                if item.discovery_state == "parsed"
                else "dedicated_parser_required"
                if item.discovery_state == "excluded"
                else "unresolved"
            ),
            "normalized_plan_sha256": (
                hashlib.sha256(
                    json.dumps(
                        item.normalized_plan,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if item.normalized_plan
                else None
            ),
        }
        for item in ordered_entries
    ]
    resolved_count = sum(
        record["resolution"] in accepted_resolutions for record in link_records.values()
    )
    manifest: dict[str, Any] = {
        "schema_version": "sce-catalog-crawl/1.0.0",
        "crawler_version": CATALOG_CRAWLER_VERSION,
        "source_policy": "official_public_sce_only",
        "network_policy": "sequential_allowlisted_https_with_pinned_dns_and_tls",
        "root_url": root_url,
        "root_artifact_sha256": root_fetch.sha256,
        "root_revision_id": root_revision.id,
        "limits": {
            "max_documents": settings.sce_catalog_crawl_max_documents,
            "max_depth": settings.sce_catalog_crawl_max_depth,
            "max_total_bytes": settings.sce_catalog_crawl_max_total_bytes,
            "max_document_bytes": settings.rate_source_max_bytes,
            "parse_timeout_seconds": settings.sce_catalog_parse_timeout_seconds,
            "request_delay_seconds": settings.sce_catalog_crawl_request_delay_seconds,
            "parallel_requests": 1,
        },
        "documents": documents,
        "links": [link_records[url] for url in sorted(link_records)],
        "plans": plan_summaries,
        "counts": {
            "documents_captured": len(documents),
            "documents_attempted": document_attempts,
            "bytes_captured": total_bytes,
            "links_discovered": len(link_records),
            "links_resolved": resolved_count,
            "plans_discovered": len(ordered_entries),
            "plans_parsed": sum(item.discovery_state == "parsed" for item in ordered_entries),
            "plans_requiring_parser_updates": len(parser_attention),
            "plans_explicitly_excluded": sum(
                item.discovery_state == "excluded" for item in ordered_entries
            ),
        },
        "closure": {
            "proved": complete,
            "scope": "residential_catalog_discovery_not_rate_publication",
            "all_discovered_links_accounted_for": not unresolved_links,
            "plans_silently_omitted": 0 if complete else None,
            "exact_tariff_parsing_complete": False,
            "exact_tariff_note": (
                "Catalog closure proves discovery accounting only; publication still requires "
                "exact approved tariff parsing and review."
            ),
            "reason": (
                "all_discovered_links_accounted_for" if complete else sorted(failure_reasons)[0]
            ),
            "failure_reasons": sorted(failure_reasons),
            "unresolved_links": unresolved_links,
            "plans_requiring_parser_updates": parser_attention,
        },
    }
    manifest_body = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return CatalogCrawlResult(
        entries=ordered_entries,
        manifest=manifest,
        manifest_body=manifest_body,
        complete=complete,
    )


async def _persist_catalog_manifest(
    session: AsyncSession,
    settings: Settings,
    source: RateSource,
    *,
    root_fetch: SourceFetch,
    crawl: CatalogCrawlResult,
) -> RateSourceRevision:
    digest = hashlib.sha256(crawl.manifest_body).hexdigest()
    target = await asyncio.to_thread(
        _write_immutable_artifact,
        settings.rate_artifact_dir,
        digest,
        crawl.manifest_body,
    )
    manifest_fetch = SourceFetch(
        requested_url=root_fetch.requested_url,
        url=root_fetch.url,
        status_code=200,
        body=crawl.manifest_body,
        sha256=digest,
        etag=None,
        last_modified=None,
        media_type="application/json",
        hops=(),
    )
    revision, _created = await _get_or_create_revision(
        session,
        source=source,
        fetched=manifest_fetch,
        target=target,
        parser_version=CATALOG_CRAWLER_VERSION,
    )
    return revision


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
    catalog_inventory_only = source.name == SCE_CATALOG_SOURCE_NAME
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

    async def fetch_with_validators(
        request_url: str,
        etag: str | None,
        last_modified: str | None,
        *,
        max_bytes: int | None = None,
    ) -> SourceFetch:
        nonlocal fetch_attempts
        for attempt in range(settings.rate_source_retry_attempts):
            fetch_attempts += 1
            try:
                return await fetch(
                    request_url,
                    allowed_hosts=settings.allowed_sce_hosts,
                    etag=etag,
                    last_modified=last_modified,
                    max_bytes=max_bytes or settings.rate_source_max_bytes,
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
        fetched = await fetch_with_validators(
            source_url,
            source.current_etag,
            source.current_last_modified,
            max_bytes=(
                min(
                    settings.rate_source_max_bytes,
                    settings.sce_catalog_crawl_max_total_bytes,
                )
                if catalog_inventory_only
                else None
            ),
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
            extra_evidence={
                "failure_phase": "fetch",
                "fetch_attempts": fetch_attempts,
                "transient_retry_codes": transient_retry_codes,
            },
        )

    run.final_url = fetched.url
    run.http_status = fetched.status_code
    run.response_bytes = fetched.byte_count
    root_response_etag = fetched.etag
    root_response_last_modified = fetched.last_modified
    run.evidence = {
        **run.evidence,
        **_fetch_evidence(fetched),
        "fetch_attempts": fetch_attempts,
        "transient_retry_codes": transient_retry_codes,
    }
    latest = (
        await _latest_catalog_root_revision(session, source.id)
        if catalog_inventory_only
        else await _latest_revision(session, source.id)
    )
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
        if catalog_inventory_only:
            try:
                captured_root = await _captured_revision_fetch(
                    session,
                    settings,
                    latest,
                    source_url=source_url,
                )
            except OSError:
                return _complete_run(
                    session,
                    run=run,
                    source=source,
                    state="failed",
                    event_code="SCE_CATALOG_CRAWL_FAILED",
                    error_code="CACHED_ROOT_ARTIFACT_INVALID",
                    revision_id=latest.id,
                    completed_at=_utc_now(),
                    actor_user_id=actor_user_id,
                    extra_evidence={"failure_phase": "cached_root_recovery"},
                )
            run.evidence = {
                **run.evidence,
                "conditional_root_not_modified": True,
                "cached_root_revision_id": latest.id,
            }
            fetched = captured_root
        latest_candidate = await session.scalar(
            select(RateCandidate).where(RateCandidate.source_revision_id == latest.id)
        )
        if catalog_inventory_only:
            pass
        elif latest_candidate is None:
            conditional_evidence = _fetch_evidence(fetched)
            run.evidence = {
                **run.evidence,
                "conditional_recovery": {
                    "reason": "latest_revision_has_no_parsed_candidate",
                    "conditional_response": conditional_evidence,
                },
            }
            try:
                fetched = await fetch_with_validators(source_url, None, None)
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
    if catalog_inventory_only:

        async def fetch_catalog_document(url: str, max_bytes: int) -> SourceFetch:
            return await fetch_with_validators(
                url,
                None,
                None,
                max_bytes=max_bytes,
            )

        crawl = await _crawl_sce_catalog(
            session,
            settings,
            source,
            root_fetch=fetched,
            root_revision=revision,
            fetch_document=fetch_catalog_document,
        )
        try:
            manifest_revision = await _persist_catalog_manifest(
                session,
                settings,
                source,
                root_fetch=fetched,
                crawl=crawl,
            )
        except OSError:
            return _complete_run(
                session,
                run=run,
                source=source,
                state="failed",
                event_code="SCE_CATALOG_CRAWL_FAILED",
                error_code="CATALOG_MANIFEST_STORAGE_FAILED",
                revision_id=revision.id,
                completed_at=_utc_now(),
                actor_user_id=actor_user_id,
                extra_evidence={
                    "failure_phase": "catalog_manifest_storage",
                    "catalog_crawl_manifest": crawl.manifest,
                },
            )
        catalog_counts = await _record_catalog_entries(
            session,
            revision=manifest_revision,
            discovered=crawl.entries,
        )
        catalog_counts["silently_omitted"] = 0 if crawl.complete else None
        if crawl.complete:
            source.current_etag = root_response_etag
            source.current_last_modified = root_response_last_modified
        return _complete_run(
            session,
            run=run,
            source=source,
            state="unchanged" if crawl.complete else "failed",
            event_code=(
                "SCE_CATALOG_CRAWL_COMPLETE" if crawl.complete else "SCE_CATALOG_CRAWL_INCOMPLETE"
            ),
            error_code=None if crawl.complete else "CATALOG_CLOSURE_UNPROVED",
            revision_id=manifest_revision.id,
            completed_at=_utc_now(),
            actor_user_id=actor_user_id,
            extra_evidence={
                "catalog_discovery": catalog_counts,
                "catalog_crawl_manifest": crawl.manifest,
                "inventory_completeness": (
                    "closure_proved" if crawl.complete else "crawl_incomplete"
                ),
                "candidate_generation_attempted": False,
            },
        )
    existing_candidate = await session.scalar(
        select(RateCandidate).where(RateCandidate.source_revision_id == revision.id)
    )
    if not revision_created and (existing_candidate is not None or catalog_inventory_only):
        catalog_counts = await _record_catalog_discovery(
            session,
            revision=revision,
            source_url=fetched.url,
            body=fetched.body,
            media_type=fetched.media_type,
            parsed=(
                ParsedRateCandidate(
                    normalized_rates=existing_candidate.normalized_rates,
                    validation_evidence=existing_candidate.validation_evidence,
                )
                if existing_candidate is not None
                else None
            ),
        )
        source.current_etag = fetched.etag
        source.current_last_modified = fetched.last_modified
        return _complete_run(
            session,
            run=run,
            source=source,
            state="unchanged",
            event_code=(
                "SCE_CATALOG_CONTENT_UNCHANGED"
                if catalog_inventory_only
                else "RATE_SOURCE_CONTENT_UNCHANGED"
            ),
            revision_id=revision.id,
            candidate_id=existing_candidate.id if existing_candidate is not None else None,
            completed_at=_utc_now(),
            actor_user_id=actor_user_id,
            extra_evidence={
                "duplicate_http_200": True,
                "catalog_discovery": catalog_counts,
            },
        )
    catalog_counts = await _record_catalog_discovery(
        session,
        revision=revision,
        source_url=fetched.url,
        body=fetched.body,
        media_type=fetched.media_type,
        parsed=None,
    )
    if catalog_inventory_only:
        source.current_etag = fetched.etag
        source.current_last_modified = fetched.last_modified
        return _complete_run(
            session,
            run=run,
            source=source,
            state="unchanged",
            event_code="SCE_CATALOG_INVENTORY_CAPTURED",
            revision_id=revision.id,
            completed_at=_utc_now(),
            actor_user_id=actor_user_id,
            extra_evidence={
                "catalog_discovery": catalog_counts,
                "inventory_completeness": "single_source_inventory_incomplete",
                "candidate_generation_attempted": False,
            },
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
                "catalog_discovery": catalog_counts,
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
                "catalog_discovery": catalog_counts,
            },
        )
    catalog_counts = await _record_catalog_discovery(
        session,
        revision=revision,
        source_url=fetched.url,
        body=fetched.body,
        media_type=fetched.media_type,
        parsed=parsed,
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
            "catalog_discovery": catalog_counts,
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
    await ensure_default_sce_catalog_source(session)
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
    "ensure_default_sce_catalog_source",
    "ensure_default_sce_source",
    "sync_due_rate_sources",
    "sync_official_rate_source",
]
