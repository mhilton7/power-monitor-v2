from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from backend.app.config import Settings
from backend.app.main import session_factory
from backend.app.models import (
    Home,
    RateCandidate,
    RateSource,
    RateSourceArtifact,
    RateSourceRevision,
    RateSyncRun,
    SceCatalogEntry,
)
from backend.app.routes.billing import _catalog_manifest_proves_discovery_closure
from backend.app.services.rate_sources import SourceFetch, SourceHop, fetch_official_source
from backend.app.services.rate_sync import (
    SCE_CATALOG_SOURCE_NAME,
    SCE_CATALOG_URL,
    ensure_default_sce_catalog_source,
    sync_official_rate_source,
)
from backend.app.services.sce_catalog import discover_sce_catalog, discover_sce_catalog_links
from backend.app.services.sce_rate_parser import ParsedRateCandidate, parse_sce_tou_public_page
from httpx import AsyncClient
from sqlalchemy import func, select

CATALOG_FIXTURES = Path(__file__).parent / "fixtures" / "sce_catalog"
TOU_INDEX_URL = (
    "https://www.sce.com/save-money/rates-financing/residential-rate-plans/time-of-use-plans"
)
COMPARISON_URL = "https://www.sce.com/save-money/rates-financing/rate-plan-comparison"
TARIFF_PDF_URL = "https://www.sce.com/regulatory/tariff-books/schedule-d-residential.pdf"
TOU_4_URL = "https://www.sce.com/save-money/rates-financing/residential-rate-plans/tou-d-4-9"
TOU_5_URL = "https://www.sce.com/save-money/rates-financing/residential-rate-plans/tou-d-5-8"


@pytest.mark.live_sce
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("PM_RUN_LIVE_SCE_SMOKE") != "1",
    reason="set PM_RUN_LIVE_SCE_SMOKE=1 to access the public official SCE catalog",
)
async def test_live_public_sce_catalog_root_is_fetchable_and_discoverable() -> None:
    """Opt-in network smoke; the normal deterministic suite uses captured fixtures."""

    fetched = await fetch_official_source(
        SCE_CATALOG_URL,
        max_bytes=2_000_000,
        max_redirects=3,
        connect_timeout_seconds=5,
        read_timeout_seconds=15,
        total_timeout_seconds=30,
    )
    assert fetched.status_code == 200
    assert fetched.body is not None
    assert fetched.media_type in {"text/html", "application/xhtml+xml"}
    links = discover_sce_catalog_links(
        fetched.body,
        fetched.media_type,
        source_url=fetched.url,
    )
    assert any(link.kind in {"plan", "traversal"} for link in links)
    assert all("/my-account/" not in link.target_url.lower() for link in links)


def _catalog_fetch(
    url: str,
    body: bytes,
    media_type: str = "text/html",
    *,
    last_modified: str = "Thu, 20 Aug 2026 18:11:43 GMT",
) -> SourceFetch:
    digest = hashlib.sha256(body).hexdigest()
    hostname = "www.sce.com"
    return SourceFetch(
        requested_url=url,
        url=url,
        status_code=200,
        body=body,
        sha256=digest,
        etag=f'"{digest[:16]}"',
        last_modified=last_modified,
        media_type=media_type,
        hops=(
            SourceHop(
                url=url,
                hostname=hostname,
                resolved_ips=("93.184.216.34",),
                connected_ip="93.184.216.34",
                status_code=200,
            ),
        ),
    )


def _catalog_body(name: str) -> bytes:
    return (CATALOG_FIXTURES / name).read_bytes()


@pytest.fixture
def catalog_artifact_dir() -> Path:
    path = Path(".test-runtime") / f"sce-catalog-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path


def _parsed_candidate() -> ParsedRateCandidate:
    return ParsedRateCandidate(
        normalized_rates={
            "utility_name": "Southern California Edison",
            "timezone": "America/Los_Angeles",
            "currency": "USD",
            "effective_start": "2026-06-01",
            "effective_end": None,
            "holiday_treatment": "weekend_schedule",
            "season_definitions": {
                "summer": {"start_month": 6, "end_month": 9},
                "winter": {"start_month": 10, "end_month": 5},
            },
            "plans": [
                {
                    "rate_plan_name": "TOU-D-4-9PM",
                    "pricing_model": "time_of_use_plus_baseline_credit",
                    "daily_fixed_charge": "0.79000000",
                    "monthly_fixed_charge": "0.00000000",
                    "baseline_credit_per_kwh": "0.10000000",
                    "rate_components": "sce_delivery_and_generation_combined",
                    "periods": [
                        {
                            "season": "summer",
                            "day_type": "weekday",
                            "name": "on_peak",
                            "start_minute": 960,
                            "end_minute": 1260,
                            "price_per_kwh": "0.58000000",
                            "currency": "USD",
                            "unit": "kWh",
                            "tier_min_kwh": None,
                            "tier_max_kwh": None,
                        }
                    ],
                }
            ],
        },
        validation_evidence={"coverage": "complete"},
    )


def test_dynamic_discovery_never_silently_drops_unknown_or_pinned_tariff_links() -> None:
    assert SCE_CATALOG_URL == (
        "https://www.sce.com/save-money/rates-financing/residential-rate-plans"
    )
    body = b"""
      <html><body>
        <a href="/save-money/rates-financing/residential-rate-plans/tou-d-4-9">
          TOU-D 4 PM to 9 PM
        </a>
        <a href="/save-money/rates-financing/residential-rate-plans/tou-d-4-9?duplicate=1">
          TOU-D 4 PM to 9 PM
        </a>
        <a href="/save-money/rates-financing/residential-rate-plans/care-medical-pilot">
          CARE Medical Baseline Residential Rate Plan Pilot
        </a>
        <a href="/save-money/rates-financing/residential-rate-plans/ev-electrification">
          Electric Vehicle and Electrification Rate Plan
        </a>
        <a href="/save-money/rates-financing/residential-rate-plans/fera-existing">
          FERA Existing Customers Only Rate Plan
        </a>
        <a href="/save-money/rates-financing/residential-rate-plans/multifamily-rv">
          Multifamily and Recreational Vehicle Residential Rate Plan
        </a>
        <a href="/save-money/rates-financing/residential-rate-plans/solar-nem">
          Solar NEM Residential Rate Plan
        </a>
        <a href="https://edisonintl.sharepoint.com/:f:/t/Public/TM2/example">
          Residential Rates
        </a>
        <a href="https://example.invalid/fake-rate-plan">Fake Residential Rate Plan</a>
        <a href="https://www.sce.com/my-account/rates">Residential Rate Plan</a>
        <a href="https://www.sce.com/help/contact-us">Contact SCE</a>
      </body></html>
    """
    entries = discover_sce_catalog(
        body,
        "text/html",
        source_url="https://www.sce.com/regulatory/regulatory-information/tariff-books/",
        parsed=_parsed_candidate(),
    )
    by_name = {entry.canonical_name: entry for entry in entries}
    assert by_name["TOU-D-4-9PM"].discovery_state == "parsed"
    unknown = next(entry for entry in entries if entry.public_plan_name.startswith("CARE Medical"))
    assert unknown.discovery_state == "requires_parser"
    assert {"care", "medical_baseline"}.issubset(unknown.eligibility)
    ev = next(entry for entry in entries if entry.public_plan_name.startswith("Electric Vehicle"))
    assert {"electric_vehicle", "heat_pump_or_electrification"}.issubset(ev.eligibility)
    fera = next(entry for entry in entries if entry.public_plan_name.startswith("FERA"))
    assert fera.enrollment_status == "existing_customers_only"
    assert "fera" in fera.eligibility
    multifamily = next(
        entry for entry in entries if entry.public_plan_name.startswith("Multifamily")
    )
    assert multifamily.discovery_state == "requires_parser"
    solar = next(entry for entry in entries if entry.public_plan_name.startswith("Solar"))
    assert "solar_or_nem" in solar.eligibility
    tariff = next(entry for entry in entries if "edisonintl.sharepoint.com" in entry.source_url)
    assert tariff.discovery_state == "excluded"
    assert tariff.source_level == 1
    assert tariff.exclusion_reason == "OFFICIAL_HOST_OUTSIDE_FETCH_ALLOWLIST"
    assert all("example.invalid" not in entry.source_url for entry in entries)
    assert all("my-account" not in entry.source_url for entry in entries)
    assert all("contact-us" not in entry.source_url for entry in entries)
    assert len([entry for entry in entries if entry.canonical_name == "TOU-D-4-9PM"]) == 1


def test_single_plan_official_detail_fixture_retains_complete_schedule_validation() -> None:
    parsed = parse_sce_tou_public_page(_catalog_body("tou-d-4-9.html"), "text/html")
    assert [plan["rate_plan_name"] for plan in parsed.normalized_rates["plans"]] == ["TOU-D-4-9PM"]
    assert parsed.validation_evidence["coverage"] == "complete"
    assert parsed.validation_evidence["plan_count"] == 1
    assert parsed.validation_evidence["period_count"] == 10


@pytest.mark.asyncio
async def test_catalog_source_adopts_a_preexisting_official_root_without_losing_identity(
    owner_client: AsyncClient,
) -> None:
    del owner_client
    async with session_factory() as session:
        legacy = RateSource(
            name="Previously configured residential root",
            source_type="official_https",
            https_url=SCE_CATALOG_URL,
            enabled=False,
            check_interval_hours=24,
        )
        session.add(legacy)
        await session.commit()
        legacy_id = legacy.id

    async with session_factory() as session:
        adopted = await ensure_default_sce_catalog_source(session)
        assert adopted.id == legacy_id
        assert adopted.name == SCE_CATALOG_SOURCE_NAME
        assert adopted.source_type == "official_https"
        assert adopted.enabled is True
        assert adopted.check_interval_hours == 168
        await session.commit()


@pytest.mark.asyncio
async def test_bounded_catalog_crawl_proves_closure_and_detects_a_new_official_plan(
    owner_client: AsyncClient,
    catalog_artifact_dir: Path,
) -> None:
    crawl_version = 1
    requests: list[str] = []

    async def fetcher(url: str, **kwargs: object) -> SourceFetch:
        requests.append(url)
        if url == SCE_CATALOG_URL and crawl_version == 2:
            root_digest = hashlib.sha256(_catalog_body("catalog-root.html")).hexdigest()
            assert kwargs["etag"] == f'"{root_digest[:16]}"'
            return SourceFetch(
                requested_url=url,
                url=url,
                status_code=304,
                body=None,
                sha256=None,
                etag=f'"{root_digest[:16]}"',
                last_modified="Thu, 20 Aug 2026 18:11:43 GMT",
                media_type=None,
                hops=(
                    SourceHop(
                        url=url,
                        hostname="www.sce.com",
                        resolved_ips=("93.184.216.34",),
                        connected_ip="93.184.216.34",
                        status_code=304,
                    ),
                ),
            )
        bodies: dict[str, tuple[bytes, str]] = {
            SCE_CATALOG_URL: (_catalog_body("catalog-root.html"), "text/html"),
            TOU_INDEX_URL: (
                _catalog_body(
                    "time-of-use-plans-v1.html"
                    if crawl_version == 1
                    else "time-of-use-plans-v2.html"
                ),
                "text/html",
            ),
            COMPARISON_URL: (
                _catalog_body("layout-only.html")
                + (b"\n<!-- presentation-only revision -->" if crawl_version == 2 else b""),
                "text/html",
            ),
            TARIFF_PDF_URL: (_catalog_body("schedule-d-residential.pdf"), "application/pdf"),
            TOU_4_URL: (_catalog_body("tou-d-4-9.html"), "text/html"),
            TOU_5_URL: (_catalog_body("tou-d-5-8.html"), "text/html"),
        }
        assert url in bodies
        body, media_type = bodies[url]
        max_bytes = kwargs["max_bytes"]
        assert isinstance(max_bytes, int)
        assert len(body) <= max_bytes
        return _catalog_fetch(url, body, media_type)

    settings = Settings(env="test", rate_artifact_dir=catalog_artifact_dir)
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        assert home_id is not None
        source = RateSource(
            name=SCE_CATALOG_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_CATALOG_URL,
            enabled=True,
            check_interval_hours=168,
        )
        session.add(source)
        await session.flush()
        first = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=home_id,
            actor_user_id=None,
            correlation_id="catalog-closure-v1",
            fetcher=fetcher,
        )
        assert first.state == "unchanged"
        first_run = await session.get(RateSyncRun, first.run_id)
        assert first_run is not None
        first_manifest = first_run.evidence["catalog_crawl_manifest"]
        assert first_manifest["closure"] == {
            **first_manifest["closure"],
            "proved": True,
            "plans_silently_omitted": 0,
            "unresolved_links": [],
        }
        assert first_manifest["counts"]["plans_parsed"] == 1
        assert first_manifest["counts"]["plans_explicitly_excluded"] == 1

        crawl_version = 2
        second = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=home_id,
            actor_user_id=None,
            correlation_id="catalog-closure-v2",
            fetcher=fetcher,
        )
        await session.commit()

        assert second.state == "unchanged"
        assert second.event_code == "SCE_CATALOG_CRAWL_COMPLETE"
        second_run = await session.get(RateSyncRun, second.run_id)
        assert second_run is not None
        manifest = second_run.evidence["catalog_crawl_manifest"]
        assert manifest["closure"]["proved"] is True
        assert manifest["closure"]["plans_silently_omitted"] == 0
        assert manifest["closure"]["all_discovered_links_accounted_for"] is True
        assert manifest["counts"] == {
            **manifest["counts"],
            "plans_discovered": 3,
            "plans_parsed": 2,
            "plans_requiring_parser_updates": 0,
            "plans_explicitly_excluded": 1,
        }
        links = {item["url"]: item for item in manifest["links"]}
        assert links[TOU_5_URL]["resolution"] == "parsed"
        assert links[TARIFF_PDF_URL]["resolution"] == "explicitly_excluded"
        assert links[TARIFF_PDF_URL]["reason"] == ("OFFICIAL_DOCUMENT_MEDIA_TYPE_UNSUPPORTED")
        assert links[COMPARISON_URL]["resolution"] == "explicitly_excluded"
        assert links[COMPARISON_URL]["reason"] == "LAYOUT_ONLY_NON_CANDIDATE"
        assert await session.scalar(select(func.count()).select_from(RateCandidate)) == 0
        artifact_count = await session.scalar(select(func.count()).select_from(RateSourceArtifact))
        assert artifact_count is not None and artifact_count >= 7
        latest_entries = (
            await session.scalars(
                select(SceCatalogEntry).where(
                    SceCatalogEntry.source_revision_id == second.revision_id
                )
            )
        ).all()
        assert {entry.canonical_name for entry in latest_entries} == {
            "DOMESTIC",
            "TOU-D-4-9PM",
            "TOU-D-5-8PM",
        }
        assert requests.count(SCE_CATALOG_URL) == 2

    response = await owner_client.get(f"/api/v1/rate-sources/catalog?home_id={home_id}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["catalog_ready"] is True
    assert payload["catalog_completeness"] == "closure_proved"
    assert payload["inventory_scope"] == "bounded_official_multi_document_crawl"
    assert payload["completeness_reason"] == "all_discovered_links_accounted_for"
    assert payload["summary"]["plans_silently_omitted"] == 0
    assert payload["summary"]["plans_discovered"] == 3


def test_catalog_manifest_closure_validation_rejects_unresolved_or_unaccounted_links() -> None:
    manifest: dict[str, object] = {
        "schema_version": "sce-catalog-crawl/1.0.0",
        "source_policy": "official_public_sce_only",
        "documents": [{"artifact_sha256": "a" * 64}],
        "links": [
            {
                "url": TOU_4_URL,
                "resolution": "parsed",
                "discovery_status": "accounted_for",
            },
            {
                "url": TARIFF_PDF_URL,
                "resolution": "explicitly_excluded",
                "discovery_status": "accounted_for",
            },
        ],
        "plans": [
            {"canonical_name": "TOU-D-4-9PM", "discovery_state": "parsed"},
            {"canonical_name": "DOMESTIC", "discovery_state": "excluded"},
        ],
        "counts": {
            "documents_captured": 1,
            "links_discovered": 2,
            "links_resolved": 2,
            "plans_discovered": 2,
            "plans_requiring_parser_updates": 0,
        },
        "closure": {
            "proved": True,
            "all_discovered_links_accounted_for": True,
            "plans_silently_omitted": 0,
            "failure_reasons": [],
            "unresolved_links": [],
            "plans_requiring_parser_updates": [],
        },
    }
    assert _catalog_manifest_proves_discovery_closure(manifest) is True

    links = manifest["links"]
    assert isinstance(links, list)
    unresolved = links[0]
    assert isinstance(unresolved, dict)
    unresolved["resolution"] = "requires_parser"
    assert _catalog_manifest_proves_discovery_closure(manifest) is False


@pytest.mark.asyncio
async def test_catalog_parser_failure_preserves_last_known_good_and_reports_incomplete(
    owner_client: AsyncClient,
    catalog_artifact_dir: Path,
) -> None:
    fail_detail = False

    async def fetcher(url: str, **kwargs: object) -> SourceFetch:
        bodies: dict[str, tuple[bytes, str]] = {
            SCE_CATALOG_URL: (_catalog_body("catalog-root.html"), "text/html"),
            TOU_INDEX_URL: (
                _catalog_body(
                    "time-of-use-plans-v1.html" if fail_detail else "time-of-use-plans-v2.html"
                ),
                "text/html",
            ),
            COMPARISON_URL: (_catalog_body("layout-only.html"), "text/html"),
            TARIFF_PDF_URL: (_catalog_body("schedule-d-residential.pdf"), "application/pdf"),
            TOU_4_URL: (
                b"<html><body><h1>Official page layout changed</h1></body></html>"
                if fail_detail
                else _catalog_body("tou-d-4-9.html"),
                "text/html",
            ),
            TOU_5_URL: (_catalog_body("tou-d-5-8.html"), "text/html"),
        }
        assert url in bodies
        body, media_type = bodies[url]
        max_bytes = kwargs["max_bytes"]
        assert isinstance(max_bytes, int)
        assert len(body) <= max_bytes
        return _catalog_fetch(url, body, media_type)

    settings = Settings(env="test", rate_artifact_dir=catalog_artifact_dir)
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        assert home_id is not None
        source = RateSource(
            name=SCE_CATALOG_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_CATALOG_URL,
            enabled=True,
            check_interval_hours=168,
        )
        session.add(source)
        await session.flush()
        complete = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=home_id,
            actor_user_id=None,
            correlation_id="catalog-lkg-complete",
            fetcher=fetcher,
        )
        assert complete.event_code == "SCE_CATALOG_CRAWL_COMPLETE"

        fail_detail = True
        incomplete = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=home_id,
            actor_user_id=None,
            correlation_id="catalog-lkg-parser-failure",
            fetcher=fetcher,
        )
        await session.commit()

        assert incomplete.state == "failed"
        assert incomplete.event_code == "SCE_CATALOG_CRAWL_INCOMPLETE"
        assert incomplete.error_code == "CATALOG_CLOSURE_UNPROVED"
        failed_run = await session.get(RateSyncRun, incomplete.run_id)
        assert failed_run is not None
        closure = failed_run.evidence["catalog_crawl_manifest"]["closure"]
        assert closure["proved"] is False
        assert closure["plans_silently_omitted"] is None
        assert closure["plans_requiring_parser_updates"] == ["TOU-D-4-9PM"]

    response = await owner_client.get(f"/api/v1/rate-sources/catalog?home_id={home_id}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["catalog_ready"] is False
    assert payload["catalog_completeness"] == "crawl_incomplete"
    assert payload["summary"]["plans_silently_omitted"] is None
    assert payload["summary"]["plans_requiring_parser_updates"] == 1
    retained = next(plan for plan in payload["plans"] if plan["canonical_name"] == "TOU-D-4-9PM")
    assert retained["verification_state"] == "parsed"
    assert retained["latest_discovery_state"] == "requires_parser"
    assert retained["last_known_good_retained"] is True
    retained_missing = next(
        plan for plan in payload["plans"] if plan["canonical_name"] == "TOU-D-5-8PM"
    )
    assert retained_missing["verification_state"] == "parsed"
    assert retained_missing["latest_discovery_state"] == "parsed"
    assert retained_missing["last_known_good_retained"] is True
    assert (
        retained_missing["source"]["revision_id"]
        == retained_missing["latest_discovery_revision_id"]
    )


@pytest.mark.asyncio
async def test_catalog_crawl_document_limit_fails_closed_and_bounds_network_access(
    catalog_artifact_dir: Path,
) -> None:
    requests: list[str] = []

    async def fetcher(url: str, **kwargs: object) -> SourceFetch:
        requests.append(url)
        bodies: dict[str, tuple[bytes, str]] = {
            SCE_CATALOG_URL: (_catalog_body("catalog-root.html"), "text/html"),
            TOU_INDEX_URL: (_catalog_body("time-of-use-plans-v1.html"), "text/html"),
            COMPARISON_URL: (_catalog_body("layout-only.html"), "text/html"),
            TARIFF_PDF_URL: (_catalog_body("schedule-d-residential.pdf"), "application/pdf"),
        }
        body, media_type = bodies[url]
        max_bytes = kwargs["max_bytes"]
        assert isinstance(max_bytes, int)
        assert len(body) <= max_bytes
        return _catalog_fetch(url, body, media_type)

    settings = Settings(
        env="test",
        rate_artifact_dir=catalog_artifact_dir,
        sce_catalog_crawl_max_documents=2,
    )
    async with session_factory() as session:
        source = RateSource(
            name=SCE_CATALOG_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_CATALOG_URL,
            enabled=True,
            check_interval_hours=168,
        )
        session.add(source)
        await session.flush()
        result = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="catalog-document-bound",
            fetcher=fetcher,
        )
        await session.commit()

        assert result.state == "failed"
        assert requests[0] == SCE_CATALOG_URL
        assert len(requests) == 2
        run = await session.get(RateSyncRun, result.run_id)
        assert run is not None
        manifest = run.evidence["catalog_crawl_manifest"]
        assert manifest["limits"]["max_documents"] == 2
        assert manifest["counts"]["documents_attempted"] == 2
        assert "document_limit_reached" in manifest["closure"]["failure_reasons"]
        assert manifest["closure"]["plans_silently_omitted"] is None


@pytest.mark.asyncio
async def test_incomplete_catalog_does_not_promote_validators_and_304_uses_complete_root(
    catalog_artifact_dir: Path,
) -> None:
    phase = "complete"
    root_body = _catalog_body("catalog-root.html")
    changed_root_body = root_body + b"\n<!-- incomplete captured root revision -->"
    complete_root_etag = _catalog_fetch(SCE_CATALOG_URL, root_body).etag
    incomplete_root_etag = _catalog_fetch(SCE_CATALOG_URL, changed_root_body).etag
    complete_last_modified = "Thu, 20 Aug 2026 18:11:43 GMT"
    incomplete_last_modified = "Fri, 21 Aug 2026 18:11:43 GMT"
    root_requests: list[tuple[object, object]] = []

    async def fetcher(url: str, **kwargs: object) -> SourceFetch:
        nonlocal phase
        if url == SCE_CATALOG_URL:
            root_requests.append((kwargs.get("etag"), kwargs.get("last_modified")))
            if phase == "recovery":
                assert kwargs["etag"] == complete_root_etag
                return SourceFetch(
                    requested_url=url,
                    url=url,
                    status_code=304,
                    body=None,
                    sha256=None,
                    etag=complete_root_etag,
                    last_modified="Thu, 20 Aug 2026 18:11:43 GMT",
                    media_type=None,
                    hops=(
                        SourceHop(
                            url=url,
                            hostname="www.sce.com",
                            resolved_ips=("93.184.216.34",),
                            connected_ip="93.184.216.34",
                            status_code=304,
                        ),
                    ),
                )
            return _catalog_fetch(
                url,
                changed_root_body if phase == "incomplete" else root_body,
                last_modified=(
                    incomplete_last_modified if phase == "incomplete" else complete_last_modified
                ),
            )
        bodies: dict[str, tuple[bytes, str]] = {
            TOU_INDEX_URL: (
                _catalog_body("time-of-use-plans-malformed.html")
                if phase == "incomplete"
                else _catalog_body("time-of-use-plans-v1.html"),
                "text/html",
            ),
            COMPARISON_URL: (_catalog_body("layout-only.html"), "text/html"),
            TARIFF_PDF_URL: (_catalog_body("schedule-d-residential.pdf"), "application/pdf"),
            TOU_4_URL: (_catalog_body("tou-d-4-9.html"), "text/html"),
        }
        body, media_type = bodies[url]
        return _catalog_fetch(url, body, media_type)

    settings = Settings(env="test", rate_artifact_dir=catalog_artifact_dir)
    async with session_factory() as session:
        source = RateSource(
            name=SCE_CATALOG_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_CATALOG_URL,
            enabled=True,
            check_interval_hours=168,
        )
        session.add(source)
        await session.flush()

        complete = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="catalog-validator-complete",
            fetcher=fetcher,
        )
        assert complete.event_code == "SCE_CATALOG_CRAWL_COMPLETE"
        complete_run = await session.get(RateSyncRun, complete.run_id)
        assert complete_run is not None
        complete_root_revision_id = complete_run.evidence["catalog_crawl_manifest"][
            "root_revision_id"
        ]
        assert source.current_etag == complete_root_etag
        assert source.current_last_modified == complete_last_modified

        phase = "incomplete"
        incomplete = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="catalog-validator-incomplete",
            fetcher=fetcher,
        )
        assert incomplete.event_code == "SCE_CATALOG_CRAWL_INCOMPLETE"
        incomplete_run = await session.get(RateSyncRun, incomplete.run_id)
        assert incomplete_run is not None
        incomplete_manifest = incomplete_run.evidence["catalog_crawl_manifest"]
        index_link = next(
            link for link in incomplete_manifest["links"] if link["url"] == TOU_INDEX_URL
        )
        assert index_link["resolution"] == "requires_parser"
        assert index_link["reason"] == "CATALOG_INDEX_NO_LINKS_EXTRACTED"
        assert incomplete_manifest["closure"]["proved"] is False
        assert incomplete_manifest["closure"]["plans_silently_omitted"] is None
        assert source.current_etag == complete_root_etag
        assert source.current_etag != incomplete_root_etag
        assert source.current_last_modified == complete_last_modified
        assert source.current_last_modified != incomplete_last_modified

        phase = "recovery"
        recovered = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="catalog-validator-recovery-304",
            fetcher=fetcher,
        )
        await session.commit()

        assert recovered.event_code == "SCE_CATALOG_CRAWL_COMPLETE"
        recovered_run = await session.get(RateSyncRun, recovered.run_id)
        assert recovered_run is not None
        assert recovered_run.evidence["conditional_root_not_modified"] is True
        assert recovered_run.evidence["cached_root_revision_id"] == complete_root_revision_id
        assert recovered_run.evidence["catalog_crawl_manifest"]["closure"]["proved"] is True
        assert source.current_etag == complete_root_etag
        assert source.current_last_modified == complete_last_modified
        assert [request[0] for request in root_requests] == [
            None,
            complete_root_etag,
            complete_root_etag,
        ]


@pytest.mark.asyncio
async def test_catalog_api_returns_normalized_schedule_and_dynamic_health(
    owner_client: AsyncClient,
) -> None:
    parsed = _parsed_candidate()
    plan = parsed.normalized_rates["plans"][0]
    assert isinstance(plan, dict)
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        assert home_id is not None
        source = RateSource(
            name=SCE_CATALOG_SOURCE_NAME,
            source_type="official_https",
            https_url=(
                "https://www.sce.com/regulatory/regulatory-information/"
                "tariff-books/rates-pricing-choices"
            ),
            enabled=True,
        )
        session.add(source)
        await session.flush()
        revision = RateSourceRevision(
            source_id=source.id,
            artifact_sha256="c" * 64,
            parser_version="sce-residential-public-v2",
            retrieved_at=datetime(2026, 8, 20, tzinfo=UTC),
        )
        session.add(revision)
        await session.flush()
        session.add(
            RateSyncRun(
                source_id=source.id,
                home_id=home_id,
                state="unchanged",
                event_code="SCE_CATALOG_CRAWL_COMPLETE",
                http_status=200,
                completed_at=datetime(2026, 8, 20, tzinfo=UTC),
                revision_id=revision.id,
                requested_url=source.https_url,
                evidence={
                    "catalog_crawl_manifest": {
                        "schema_version": "sce-catalog-crawl/1.0.0",
                        "source_policy": "official_public_sce_only",
                        "documents": [{"artifact_sha256": "c" * 64}],
                        "links": [
                            {
                                "url": source.https_url,
                                "resolution": "parsed",
                                "discovery_status": "accounted_for",
                            }
                        ],
                        "plans": [
                            {
                                "canonical_name": "TOU-D-4-9PM",
                                "discovery_state": "parsed",
                            }
                        ],
                        "counts": {
                            "documents_captured": 1,
                            "links_discovered": 1,
                            "links_resolved": 1,
                            "plans_discovered": 1,
                            "plans_parsed": 1,
                            "plans_requiring_parser_updates": 0,
                            "plans_explicitly_excluded": 0,
                        },
                        "closure": {
                            "proved": True,
                            "all_discovered_links_accounted_for": True,
                            "plans_silently_omitted": 0,
                            "reason": "all_discovered_links_accounted_for",
                            "failure_reasons": [],
                            "unresolved_links": [],
                            "plans_requiring_parser_updates": [],
                        },
                    }
                },
            )
        )
        session.add(
            SceCatalogEntry(
                source_revision_id=revision.id,
                source_url=source.https_url,
                source_level=1,
                official_schedule_code="TOU-D-4-9PM",
                public_plan_name="TOU-D 4 PM to 9 PM",
                canonical_name="TOU-D-4-9PM",
                plan_type="time_of_use_with_baseline_credit",
                enrollment_status="open_or_eligibility_required",
                eligibility=["electric_vehicle"],
                discovery_state="parsed",
                normalized_plan={
                    **plan,
                    "catalog_metadata": {
                        "effective_start": "2026-06-01",
                        "effective_end": None,
                        "timezone": "America/Los_Angeles",
                        "currency": "USD",
                        "holiday_treatment": "weekend_schedule",
                        "season_definitions": parsed.normalized_rates["season_definitions"],
                    },
                },
            )
        )
        newer_revision = RateSourceRevision(
            source_id=source.id,
            artifact_sha256="d" * 64,
            parser_version="sce-residential-public-v2",
            retrieved_at=datetime(2026, 8, 21, tzinfo=UTC),
        )
        session.add(newer_revision)
        await session.flush()
        session.add(
            RateSyncRun(
                source_id=source.id,
                home_id=home_id,
                state="failed",
                event_code="SCE_CATALOG_CRAWL_INCOMPLETE",
                completed_at=datetime(2026, 8, 21, tzinfo=UTC),
                revision_id=newer_revision.id,
                requested_url=source.https_url,
                evidence={
                    "catalog_crawl_manifest": {
                        "closure": {
                            "proved": False,
                            "plans_silently_omitted": None,
                            "reason": "plans_require_parser_updates",
                        }
                    }
                },
            )
        )
        session.add(
            SceCatalogEntry(
                source_revision_id=newer_revision.id,
                source_url=source.https_url,
                source_level=1,
                official_schedule_code="TOU-D-4-9PM",
                public_plan_name="TOU-D 4 PM to 9 PM",
                canonical_name="TOU-D-4-9PM",
                plan_type="time_of_use_with_baseline_credit",
                enrollment_status="open_or_eligibility_required",
                eligibility=["electric_vehicle"],
                discovery_state="requires_parser",
                normalized_plan={},
            )
        )
        await session.commit()

    response = await owner_client.get("/api/v1/rate-sources/catalog")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["summary"] == {
        **payload["summary"],
        "plans_discovered": 1,
        "plans_parsed": 0,
        "plans_requiring_parser_updates": 1,
        "plans_explicitly_excluded": 0,
        "plans_silently_omitted": None,
    }
    returned = payload["plans"][0]
    assert returned["plan_type"] == "time_of_use_with_baseline_credit"
    assert returned["verification_state"] == "parsed"
    assert returned["latest_discovery_state"] == "requires_parser"
    assert returned["latest_discovery_revision_id"] != returned["source"]["revision_id"]
    assert returned["last_known_good_retained"] is True
    assert returned["eligibility_requirements"] == [
        {
            "requirement": "electric_vehicle",
            "verification": "home_confirmation_required",
        }
    ]
    assert returned["holiday_treatment"] == "weekend_schedule"
    assert returned["periods"] == returned["schedule"]
    assert returned["schedule"][0] == {
        **returned["schedule"][0],
        "local_start_time": "16:00",
        "local_end_time": "21:00",
        "price_per_kwh": "0.58000000",
    }
    assert returned["schedule"][0]["rate_components"][0]["source_status"] == "combined_only"
    assert payload["live_source_access_performed"] is False
    assert payload["inventory_scope"] == "bounded_official_multi_document_crawl"
    assert payload["catalog_completeness"] == "crawl_incomplete"
    assert payload["catalog_ready"] is False
    assert payload["completeness_reason"] == "plans_require_parser_updates"
    assert payload["summary"]["last_successful_official_check"].startswith("2026-08-20")
