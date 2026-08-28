from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from backend.app.config import Settings
from backend.app.main import session_factory
from backend.app.models import (
    AuditEvent,
    Home,
    RateSource,
    RateSourceRevision,
    RateSyncRun,
    SceCatalogEntry,
)
from backend.app.services.rate_sources import SourceFetch, SourceHop, fetch_official_source
from backend.app.services.rate_sync import (
    SCE_CATALOG_SOURCE_NAME,
    SCE_CATALOG_URL,
    ensure_default_sce_catalog_source,
    sync_official_rate_source,
)
from backend.app.services.sce_catalog import (
    SUPPORTED_PLAN_NAMES,
    discover_sce_catalog,
    discover_sce_catalog_links,
    inspect_sce_catalog_links,
)
from backend.app.services.sce_rate_parser import ParsedRateCandidate, parse_sce_tou_public_page
from httpx import AsyncClient
from sqlalchemy import select

CATALOG_FIXTURES = Path(__file__).parent / "fixtures" / "sce_catalog"
TIERED_URL = (
    "https://www.sce.com/save-money/rates-financing/residential-rate-plans/tiered-rate-plan"
)
TOU_4_URL = "https://www.sce.com/save-money/rates-financing/residential-rate-plans/tou-d-4-9"
TOU_5_URL = "https://www.sce.com/save-money/rates-financing/residential-rate-plans/tou-d-5-8"
TOU_PRIME_URL = "https://www.sce.com/save-money/rates-financing/residential-rate-plans/tou-d-prime"
DETAIL_FIXTURES = {
    TIERED_URL: "tiered-rate-plan.html",
    TOU_4_URL: "tou-d-4-9.html",
    TOU_5_URL: "tou-d-5-8.html",
    TOU_PRIME_URL: "tou-d-prime.html",
}


def _catalog_body(name: str) -> bytes:
    return (CATALOG_FIXTURES / name).read_bytes()


def _catalog_fetch(
    url: str,
    body: bytes,
    media_type: str = "text/html",
    *,
    last_modified: str = "Thu, 20 Aug 2026 18:11:43 GMT",
) -> SourceFetch:
    digest = hashlib.sha256(body).hexdigest()
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
                hostname="www.sce.com",
                resolved_ips=("93.184.216.34",),
                connected_ip="93.184.216.34",
                status_code=200,
            ),
        ),
    )


@pytest.fixture
def catalog_artifact_dir() -> Path:
    path = Path(".test-runtime") / f"sce-catalog-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path


@pytest.mark.live_sce
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("PM_RUN_LIVE_SCE_SMOKE") != "1",
    reason="set PM_RUN_LIVE_SCE_SMOKE=1 to access the public official SCE catalog",
)
async def test_live_public_sce_catalog_root_is_fetchable_and_main_scoped() -> None:
    """Opt-in only; ordinary CI uses the sanitized captured HTML below."""

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
    parsed = parse_sce_tou_public_page(fetched.body, fetched.media_type)
    plans = discover_sce_catalog(
        fetched.body,
        fetched.media_type,
        source_url=fetched.url,
        parsed=parsed,
    )
    assert {plan.canonical_name for plan in plans} == set(SUPPORTED_PLAN_NAMES)
    assert all(
        plan.discovery_state == "parsed" for plan in plans if plan.canonical_name != "DOMESTIC"
    )
    assert next(plan for plan in plans if plan.canonical_name == "DOMESTIC").discovery_state == (
        "requires_parser"
    )
    assert parsed.normalized_rates["holiday_treatment"] == "unresolved"
    assert parsed.normalized_rates["effective_date_confirmation_required"] is True
    assert all("/my-account/" not in link.target_url.lower() for link in links)
    assert fetched.hops

    tiered_link = next(link for link in links if link.canonical_name == "DOMESTIC")
    await asyncio.sleep(0.25)
    tiered_fetch = await fetch_official_source(
        tiered_link.target_url,
        max_bytes=2_000_000,
        max_redirects=3,
        connect_timeout_seconds=5,
        read_timeout_seconds=15,
        total_timeout_seconds=30,
    )
    assert tiered_fetch.status_code == 200
    assert tiered_fetch.body is not None
    assert tiered_fetch.media_type in {"text/html", "application/xhtml+xml"}
    tiered_parsed = parse_sce_tou_public_page(tiered_fetch.body, tiered_fetch.media_type)
    tiered_plans = discover_sce_catalog(
        tiered_fetch.body,
        tiered_fetch.media_type,
        source_url=tiered_fetch.url,
        parsed=tiered_parsed,
        enrichment_for="DOMESTIC",
    )
    assert [(plan.canonical_name, plan.discovery_state) for plan in tiered_plans] == [
        ("DOMESTIC", "parsed")
    ]
    assert tiered_fetch.hops


def test_current_live_structure_discovers_embedded_tou_and_exact_tiered_link_only() -> None:
    body = _catalog_body("current-live-time-of-use-plans.html")
    parsed = parse_sce_tou_public_page(body, "text/html")
    inspection = inspect_sce_catalog_links(body, "text/html", source_url=SCE_CATALOG_URL)
    plans = discover_sce_catalog(
        body,
        "text/html",
        source_url=SCE_CATALOG_URL,
        parsed=parsed,
    )

    assert inspection.primary_plans == (
        ("TOU-D-4-9PM", "TOU-D 4 PM to 9 PM"),
        ("TOU-D-5-8PM", "TOU-D 5 PM to 8 PM"),
        ("TOU-D-PRIME", "TOU-D-PRIME"),
    )
    assert [(link.canonical_name, link.target_url) for link in inspection.links] == [
        ("DOMESTIC", TIERED_URL)
    ]
    assert {plan.canonical_name for plan in plans} == set(SUPPORTED_PLAN_NAMES)
    assert all(
        plan.discovery_state == "parsed" for plan in plans if plan.canonical_name != "DOMESTIC"
    )
    assert next(plan for plan in plans if plan.canonical_name == "DOMESTIC").discovery_state == (
        "requires_parser"
    )
    serialized = json.dumps([plan.__dict__ for plan in plans], sort_keys=True)
    for false_candidate in (
        "Rate Plans",
        "Solar Billing Plan",
        "Understanding Updates to Your Electricity Bill",
        "TOU-D-A",
        "TOU-D-B",
        "TOU-D-T",
    ):
        assert false_candidate not in serialized


def test_child_enrichment_cannot_introduce_another_primary_plan() -> None:
    parsed = parse_sce_tou_public_page(
        _catalog_body("current-live-time-of-use-plans.html"), "text/html"
    )
    entries = discover_sce_catalog(
        _catalog_body("current-live-time-of-use-plans.html"),
        "text/html",
        source_url=TOU_4_URL,
        parsed=parsed,
        enrichment_for="TOU-D-4-9PM",
    )

    assert [entry.canonical_name for entry in entries] == ["TOU-D-4-9PM"]


def test_new_primary_tou_heading_is_retained_as_parser_update_not_invented() -> None:
    body = b"""
      <html><body><main>
        <div class="accordion-container-bg-layout">
          <h2 class="accordion-container-header-button-headline">TOU-D-FUTURE</h2>
          <p>The official page layout changed and has no reusable rate schedule yet.</p>
        </div>
        <section class="faq"><h2>TOU-D-FAKE FAQ</h2></section>
      </main></body></html>
    """
    plans = discover_sce_catalog(
        body,
        "text/html",
        source_url=SCE_CATALOG_URL,
        parsed=None,
    )

    assert [(plan.canonical_name, plan.discovery_state) for plan in plans] == [
        ("TOU-D-FUTURE", "requires_parser")
    ]
    assert plans[0].normalized_plan["discovery_evidence"]["kind"] == ("primary_plan_heading")


def test_discovery_is_exact_main_scoped_and_rejects_false_candidates() -> None:
    assert SCE_CATALOG_URL == (
        "https://www.sce.com/save-money/rates-financing/residential-rate-plans/time-of-use-plans"
    )
    links = discover_sce_catalog_links(
        _catalog_body("time-of-use-plans-v2.html"),
        "text/html",
        source_url=SCE_CATALOG_URL,
    )

    assert {link.canonical_name for link in links} == set(SUPPORTED_PLAN_NAMES)
    assert {link.target_url for link in links} == {
        TIERED_URL,
        TOU_4_URL,
        TOU_5_URL,
        TOU_PRIME_URL,
    }
    assert next(link for link in links if link.canonical_name == "DOMESTIC").label == (
        "Tiered Rate Plan"
    )
    serialized = json.dumps([link.__dict__ for link in links], sort_keys=True)
    for false_candidate in (
        "Rate Plans",
        "Solar Billing Plan",
        "Understanding Updates to Your Electricity Bill",
        "TOU-D-A",
        "TOU-D-B",
        "TOU-D-T",
    ):
        assert false_candidate not in serialized


def test_layout_only_change_is_deterministic_and_does_not_duplicate_plans() -> None:
    baseline = discover_sce_catalog_links(
        _catalog_body("time-of-use-plans-v2.html"),
        "text/html",
        source_url=SCE_CATALOG_URL,
    )
    layout_only = discover_sce_catalog_links(
        _catalog_body("layout-only.html"),
        "text/html",
        source_url=SCE_CATALOG_URL,
    )

    baseline_signature = [(item.canonical_name, item.target_url, item.kind) for item in baseline]
    layout_signature = [(item.canonical_name, item.target_url, item.kind) for item in layout_only]
    assert baseline_signature == layout_signature
    assert len({item.canonical_name for item in layout_only}) == 4


def test_void_elements_cannot_leak_footer_or_navigation_into_main_scope() -> None:
    body = b"""
      <html><head><meta charset="utf-8"></head><body>
        <main><img src="presentation.png"><br>
          <h2>TOU-D 4 PM to 9 PM</h2>
          <a href="/save-money/rates-financing/residential-rate-plans/tou-d-4-9">
            TOU-D 4 PM to 9 PM details
          </a>
        </main>
        <footer><img src="footer.png"><br>
          <h2>TOU-D-PRIME</h2>
          <a href="/save-money/rates-financing/residential-rate-plans/tou-d-prime">
            TOU-D-PRIME details
          </a>
        </footer>
      </body></html>
    """
    links = discover_sce_catalog_links(body, "text/html", source_url=SCE_CATALOG_URL)

    assert [link.canonical_name for link in links] == ["TOU-D-4-9PM"]


def test_enrichment_is_family_bound_and_cannot_discover_another_plan() -> None:
    detail = b"""
      <html><body><main>
        <h1>TOU-D 4 PM to 9 PM</h1>
        <a href="/regulatory/tariff-books/tou-d-4-9.pdf">TOU-D 4 PM to 9 PM tariff</a>
        <h2>Related Links</h2>
        <a href="/save-money/rates-financing/solar-billing-plan">Solar Billing Plan</a>
        <a href="/regulatory/tariff-books/schedule-d.pdf">Schedule D tariff</a>
      </main></body></html>
    """
    inspected = inspect_sce_catalog_links(
        detail,
        "text/html",
        source_url=TOU_4_URL,
        enrichment_for="TOU-D-4-9PM",
    )

    assert [(link.canonical_name, link.target_url) for link in inspected.links] == [
        (
            "TOU-D-4-9PM",
            "https://www.sce.com/regulatory/tariff-books/tou-d-4-9.pdf",
        )
    ]


def test_normalized_enrichment_cannot_add_an_unauthorized_catalog_entry() -> None:
    valid = parse_sce_tou_public_page(_catalog_body("tou-d-4-9.html"), "text/html")
    valid_plan = valid.normalized_rates["plans"][0]
    fake_plan = {
        **valid_plan,
        "rate_plan_name": "Solar Billing Plan",
    }
    parsed = ParsedRateCandidate(
        normalized_rates={**valid.normalized_rates, "plans": [valid_plan, fake_plan]},
        validation_evidence=valid.validation_evidence,
    )
    entries = discover_sce_catalog(
        _catalog_body("tou-d-4-9.html"),
        "text/html",
        source_url=TOU_4_URL,
        parsed=parsed,
    )

    assert [entry.canonical_name for entry in entries] == ["TOU-D-4-9PM"]
    assert entries[0].public_plan_name == "TOU-D 4 PM to 9 PM"


def test_zero_period_candidate_is_not_complete() -> None:
    parsed = ParsedRateCandidate(
        normalized_rates={
            "effective_start": "2026-06-01",
            "plans": [
                {
                    "rate_plan_name": "TOU-D-4-9PM",
                    "pricing_model": "time_of_use",
                    "daily_fixed_charge": "0.79000000",
                    "periods": [],
                }
            ],
        },
        validation_evidence={},
    )
    entries = discover_sce_catalog(
        _catalog_body("time-of-use-plans-v2.html"),
        "text/html",
        source_url=SCE_CATALOG_URL,
        parsed=parsed,
    )

    tou = next(entry for entry in entries if entry.canonical_name == "TOU-D-4-9PM")
    assert tou.discovery_state == "requires_parser"
    assert tou.normalized_plan == {}


def test_power_monitor_predecessor_fixture_characterizes_schedule_semantics() -> None:
    """Freeze the proven predecessor output before adapting its parser behavior."""

    parsed = parse_sce_tou_public_page(
        _catalog_body("power-monitor-public-tou-page.html"),
        "text/html",
    )
    plan = parsed.normalized_rates["plans"][0]

    assert plan["rate_plan_name"] == "TOU-D-4-9PM"
    assert plan["daily_fixed_charge"] == "0.79000000"
    assert plan["baseline_credit_per_kwh"] == "0.10000000"
    assert parsed.normalized_rates["effective_start"] is None
    assert parsed.normalized_rates["effective_date_confirmation_required"] is True
    assert parsed.normalized_rates["holiday_treatment"] == "unresolved"
    assert [
        (
            period["season"],
            period["day_type"],
            period["name"],
            period["start_minute"],
            period["end_minute"],
            period["price_per_kwh"],
        )
        for period in plan["periods"]
    ] == [
        ("summer", "weekday", "off_peak", 0, 960, "0.34000000"),
        ("summer", "weekday", "on_peak", 960, 1260, "0.58000000"),
        ("summer", "weekday", "off_peak", 1260, 1440, "0.34000000"),
        ("summer", "weekend", "off_peak", 0, 960, "0.34000000"),
        ("summer", "weekend", "mid_peak", 960, 1260, "0.46000000"),
        ("summer", "weekend", "off_peak", 1260, 1440, "0.34000000"),
        ("winter", "all_days", "off_peak", 0, 480, "0.37000000"),
        ("winter", "all_days", "super_off_peak", 480, 960, "0.33000000"),
        ("winter", "all_days", "mid_peak", 960, 1260, "0.51000000"),
        ("winter", "all_days", "off_peak", 1260, 1440, "0.37000000"),
    ]


@pytest.mark.parametrize(
    ("fixture_name", "canonical_name", "period_count"),
    (
        ("tiered-rate-plan.html", "DOMESTIC", 2),
        ("tou-d-4-9.html", "TOU-D-4-9PM", 10),
        ("tou-d-5-8.html", "TOU-D-5-8PM", 10),
        ("tou-d-prime.html", "TOU-D-PRIME", 10),
    ),
)
def test_each_supported_plan_fixture_has_a_complete_semantic_signature(
    fixture_name: str,
    canonical_name: str,
    period_count: int,
) -> None:
    parsed = parse_sce_tou_public_page(_catalog_body(fixture_name), "text/html")
    plan = parsed.normalized_rates["plans"][0]

    assert plan["rate_plan_name"] == canonical_name
    assert len(plan["periods"]) == period_count
    assert parsed.normalized_rates["effective_start"] == "2026-06-01"
    assert parsed.normalized_rates["effective_date_confirmation_required"] is False
    assert plan["daily_fixed_charge"] == "0.79000000"
    if canonical_name == "DOMESTIC":
        assert {period["name"] for period in plan["periods"]} == {"tier_1", "tier_2"}
        assert plan["tier_threshold_basis"] == ("home_baseline_allocation_review_required")
    else:
        assert {period["season"] for period in plan["periods"]} == {"summer", "winter"}
        assert parsed.normalized_rates["holiday_treatment"] == "weekend_schedule"


@pytest.mark.asyncio
async def test_catalog_source_adopts_exact_official_root_without_losing_identity(
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
        assert adopted.https_url == SCE_CATALOG_URL
        assert adopted.enabled is True
        assert adopted.check_interval_hours == 168
        await session.commit()


@pytest.mark.asyncio
async def test_catalog_source_collision_is_reconciled_without_deleting_evidence(
    owner_client: AsyncClient,
) -> None:
    del owner_client
    old_root = "https://www.sce.com/save-money/rates-financing/residential-rate-plans"
    async with session_factory() as session:
        legacy = RateSource(
            name=SCE_CATALOG_SOURCE_NAME,
            source_type="official_https",
            https_url=old_root,
            enabled=True,
        )
        exact = RateSource(
            name="SCE residential TOU public page",
            source_type="official_https",
            https_url=SCE_CATALOG_URL,
            enabled=True,
        )
        session.add_all((legacy, exact))
        await session.flush()
        session.add_all(
            (
                RateSourceRevision(
                    source_id=legacy.id,
                    artifact_sha256="a" * 64,
                    parser_version="legacy-catalog-parser",
                ),
                RateSourceRevision(
                    source_id=exact.id,
                    artifact_sha256="b" * 64,
                    parser_version="exact-root-parser",
                ),
            )
        )
        await session.commit()
        legacy_id = legacy.id
        exact_id = exact.id

    async with session_factory() as session:
        canonical = await ensure_default_sce_catalog_source(session)
        await session.commit()
        assert canonical.id == exact_id
        assert canonical.name == SCE_CATALOG_SOURCE_NAME
        assert canonical.enabled is True

    async with session_factory() as session:
        preserved_legacy = await session.get(RateSource, legacy_id)
        assert preserved_legacy is not None
        assert preserved_legacy.name == "SCE residential rate catalog inventory (legacy root)"
        assert preserved_legacy.enabled is False
        revisions = (
            await session.scalars(
                select(RateSourceRevision).where(
                    RateSourceRevision.source_id.in_((legacy_id, exact_id))
                )
            )
        ).all()
        assert {(revision.source_id, revision.artifact_sha256) for revision in revisions} == {
            (legacy_id, "a" * 64),
            (exact_id, "b" * 64),
        }
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_code == "RATE_SOURCE_IDENTITY_REPAIRED",
                AuditEvent.target_id == exact_id,
            )
        )
        assert audit is not None
        assert audit.details["evidence_preserved"] is True
        assert audit.details["rows_deleted"] == 0


@pytest.mark.asyncio
async def test_bounded_catalog_crawl_parses_root_plans_and_fetches_only_tiered_detail(
    owner_client: AsyncClient,
    catalog_artifact_dir: Path,
) -> None:
    requests: list[str] = []

    async def fetcher(url: str, **kwargs: object) -> SourceFetch:
        requests.append(url)
        if url == SCE_CATALOG_URL:
            body = _catalog_body("current-live-time-of-use-plans.html")
        else:
            body = _catalog_body(DETAIL_FIXTURES[url])
        max_bytes = kwargs["max_bytes"]
        assert isinstance(max_bytes, int) and len(body) <= max_bytes
        return _catalog_fetch(url, body)

    settings = Settings(
        env="test",
        rate_artifact_dir=catalog_artifact_dir,
        sce_catalog_crawl_request_delay_seconds=0,
    )
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
        result = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=home_id,
            actor_user_id=None,
            correlation_id="catalog-exact-boundary",
            fetcher=fetcher,
        )
        await session.commit()

        assert result.state == "unchanged"
        assert result.event_code == "SCE_CATALOG_CRAWL_COMPLETE"
        run = await session.get(RateSyncRun, result.run_id)
        assert run is not None
        manifest = run.evidence["catalog_crawl_manifest"]
        assert manifest["root_url"] == SCE_CATALOG_URL
        assert manifest["counts"] == {
            **manifest["counts"],
            "plans_discovered": 4,
            "plans_parsed": 4,
            "plans_requiring_parser_updates": 0,
            "plans_explicitly_excluded": 0,
        }
        assert manifest["closure"]["proved"] is True
        assert manifest["closure"]["plans_silently_omitted"] == 0
        assert {item["canonical_name"] for item in manifest["plans"]} == set(SUPPORTED_PLAN_NAMES)
        entries = (
            await session.scalars(
                select(SceCatalogEntry).where(
                    SceCatalogEntry.source_revision_id == result.revision_id
                )
            )
        ).all()
        assert {entry.public_plan_name for entry in entries} == set(SUPPORTED_PLAN_NAMES.values())

    assert requests == [SCE_CATALOG_URL, TIERED_URL]
    response = await owner_client.get(f"/api/v1/rate-sources/catalog?home_id={home_id}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["catalog_ready"] is True
    assert payload["summary"]["plans_discovered"] == 4
    assert {plan["canonical_name"] for plan in payload["plans"]} == set(SUPPORTED_PLAN_NAMES)
    assert all(plan["rate_precision"] == "consumer_display_rounded" for plan in payload["plans"])
    assert all(plan["exact_rates_verified"] is False for plan in payload["plans"])


@pytest.mark.asyncio
async def test_one_failed_plan_preserves_all_last_known_good_entries(
    owner_client: AsyncClient,
    catalog_artifact_dir: Path,
) -> None:
    fail_tou_4 = False

    async def fetcher(url: str, **kwargs: object) -> SourceFetch:
        if url == SCE_CATALOG_URL:
            body = _catalog_body("time-of-use-plans-v2.html")
        elif fail_tou_4 and url == TOU_4_URL:
            body = b"<html><body><main><h1>Official layout changed</h1></main></body></html>"
        else:
            body = _catalog_body(DETAIL_FIXTURES[url])
        return _catalog_fetch(url, body)

    settings = Settings(
        env="test",
        rate_artifact_dir=catalog_artifact_dir,
        sce_catalog_crawl_request_delay_seconds=0,
    )
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        assert home_id is not None
        source = RateSource(
            name=SCE_CATALOG_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_CATALOG_URL,
            enabled=True,
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

        fail_tou_4 = True
        incomplete = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=home_id,
            actor_user_id=None,
            correlation_id="catalog-lkg-one-plan-failed",
            fetcher=fetcher,
        )
        await session.commit()

        assert incomplete.state == "failed"
        assert incomplete.event_code == "SCE_CATALOG_CRAWL_INCOMPLETE"
        failed_run = await session.get(RateSyncRun, incomplete.run_id)
        assert failed_run is not None
        closure = failed_run.evidence["catalog_crawl_manifest"]["closure"]
        assert closure["plans_requiring_parser_updates"] == ["TOU-D-4-9PM"]

    response = await owner_client.get(f"/api/v1/rate-sources/catalog?home_id={home_id}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["catalog_ready"] is False
    assert {plan["canonical_name"] for plan in payload["plans"]} == set(SUPPORTED_PLAN_NAMES)
    failed = next(plan for plan in payload["plans"] if plan["canonical_name"] == "TOU-D-4-9PM")
    assert failed["verification_state"] == "parsed"
    assert failed["latest_discovery_state"] == "requires_parser"
    assert failed["last_known_good_retained"] is True
    retained = [plan for plan in payload["plans"] if plan["canonical_name"] != "TOU-D-4-9PM"]
    assert all(plan["verification_state"] == "parsed" for plan in retained)


@pytest.mark.asyncio
async def test_catalog_document_limit_fails_closed_without_fetching_beyond_limit(
    catalog_artifact_dir: Path,
) -> None:
    requests: list[str] = []

    async def fetcher(url: str, **kwargs: object) -> SourceFetch:
        requests.append(url)
        body = (
            _catalog_body("time-of-use-plans-v2.html")
            if url == SCE_CATALOG_URL
            else _catalog_body(DETAIL_FIXTURES[url])
        )
        return _catalog_fetch(url, body)

    settings = Settings(
        env="test",
        rate_artifact_dir=catalog_artifact_dir,
        sce_catalog_crawl_max_documents=2,
        sce_catalog_crawl_request_delay_seconds=0,
    )
    async with session_factory() as session:
        source = RateSource(
            name=SCE_CATALOG_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_CATALOG_URL,
            enabled=True,
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
        assert len(requests) == 2
        run = await session.get(RateSyncRun, result.run_id)
        assert run is not None
        closure = run.evidence["catalog_crawl_manifest"]["closure"]
        assert "document_limit_reached" in closure["failure_reasons"]
        assert closure["plans_silently_omitted"] is None


@pytest.mark.asyncio
async def test_catalog_api_uses_latest_complete_source_after_later_failed_check(
    owner_client: AsyncClient,
) -> None:
    """The API's LKG selection remains independent of live source access."""

    parsed = parse_sce_tou_public_page(_catalog_body("tou-d-4-9.html"), "text/html")
    plan = discover_sce_catalog(
        _catalog_body("tou-d-4-9.html"),
        "text/html",
        source_url=TOU_4_URL,
        parsed=parsed,
    )[0]
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        assert home_id is not None
        source = RateSource(
            name=SCE_CATALOG_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_CATALOG_URL,
            enabled=True,
        )
        session.add(source)
        await session.flush()
        from backend.app.models import RateSourceRevision

        revision = RateSourceRevision(
            source_id=source.id,
            artifact_sha256="c" * 64,
            parser_version="sce-power-monitor-compatible-crawl-v3",
            retrieved_at=datetime(2026, 8, 20, tzinfo=UTC),
        )
        session.add(revision)
        await session.flush()
        session.add(
            SceCatalogEntry(
                source_revision_id=revision.id,
                source_url=plan.source_url,
                source_level=plan.source_level,
                official_schedule_code=plan.official_schedule_code,
                public_plan_name=plan.public_plan_name,
                canonical_name=plan.canonical_name,
                plan_type=plan.plan_type,
                enrollment_status=plan.enrollment_status,
                eligibility=list(plan.eligibility),
                discovery_state="parsed",
                normalized_plan=plan.normalized_plan,
            )
        )
        session.add(
            SceCatalogEntry(
                source_revision_id=revision.id,
                source_url="https://www.sce.com/save-money/solar-billing-plan",
                source_level=2,
                official_schedule_code=None,
                public_plan_name="Solar Billing Plan Maximize savings with solar energy credits",
                canonical_name="SOLAR-BILLING-PLAN",
                plan_type="unknown",
                enrollment_status="unknown",
                eligibility=[],
                discovery_state="parsed",
                normalized_plan={},
            )
        )
        session.add(
            RateSyncRun(
                source_id=source.id,
                home_id=home_id,
                state="unchanged",
                event_code="SCE_CATALOG_CRAWL_COMPLETE",
                completed_at=datetime(2026, 8, 20, tzinfo=UTC),
                revision_id=revision.id,
                requested_url=SCE_CATALOG_URL,
                evidence={
                    "catalog_crawl_manifest": {
                        "schema_version": "sce-catalog-crawl/1.0.0",
                        "documents": [{"artifact_sha256": "c" * 64}],
                        "links": [],
                        "plans": [{"canonical_name": "TOU-D-4-9PM"}],
                        "counts": {
                            "documents_captured": 1,
                            "links_discovered": 0,
                            "links_resolved": 0,
                            "plans_discovered": 1,
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
                },
            )
        )
        await session.commit()

    response = await owner_client.get(f"/api/v1/rate-sources/catalog?home_id={home_id}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["live_source_access_performed"] is False
    assert [plan["canonical_name"] for plan in payload["plans"]] == ["TOU-D-4-9PM"]
    async with session_factory() as session:
        preserved_false_evidence = await session.scalar(
            select(SceCatalogEntry).where(SceCatalogEntry.canonical_name == "SOLAR-BILLING-PLAN")
        )
        assert preserved_false_evidence is not None
