from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic

import pytest
from backend.app.config import RATE_SOURCE_OPERATION_TIMEOUT_MAX_SECONDS, Settings
from backend.app.errors import RateSyncBusy, RateWorkflowConflict
from backend.app.main import app, engine, session_factory
from backend.app.models import (
    AuditEvent,
    Home,
    RateAssignment,
    RateCandidate,
    RateCandidateReview,
    RateDatedPrice,
    RateHoliday,
    RatePeriod,
    RatePlan,
    RatePlanVersion,
    RateSource,
    RateSourceRevision,
    RateSyncRun,
    SceCatalogEntry,
    User,
    UtilityAccount,
    UtilityAccountTierThreshold,
)
from backend.app.schemas.api import ManualRateCandidateRequest
from backend.app.services import rate_sync as rate_sync_service
from backend.app.services.rate_sources import SourceFetchError
from backend.app.services.rate_sync import (
    SCE_CATALOG_URL,
    SCE_SOURCE_NAME,
    SCE_TOU_URL,
    ensure_default_sce_source,
    sync_due_rate_sources,
    sync_official_rate_source,
)
from backend.app.services.rate_workflow import (
    _canonical_dated_prices,
    activate_rate_candidate,
    create_manual_rate_candidate,
    locked_rate_plan_and_next_version,
    publish_rate_candidate,
    replace_rate_assignment,
    replace_utility_account_tier_threshold,
    resolve_utility_account_cycle_tier_threshold,
    resolve_utility_account_tier_threshold,
)
from backend.app.services.sce_rate_parser import ParsedRateCandidate
from backend.tests.test_rate_source_sync import _fetched, valid_sce_page
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def rate_artifact_dir() -> Path:
    path = Path(".test-runtime") / f"rate-workflow-{uuid.uuid4()}"
    path.mkdir(parents=True)
    return path


def _manual_payload(*, price: str = "0.12345678") -> dict[str, object]:
    return {
        "source_title": "SCE Schedule D official tariff",
        "tariff_identifier": "Schedule D 2026-08-01",
        "source_url": "https://www.sce.com/regulatory/tariff-books/rates-pricing-choices",
        "administrator_attests_official_source": True,
        "rate_plan_name": "MANUAL-TOU-D",
        "rate_class": "residential",
        "effective_start": "2026-08-01T00:00:00-07:00",
        "daily_fixed_charge": "0.79000000",
        "monthly_fixed_charge": "0.00000000",
        "baseline_credit_per_kwh": "0.10000000",
        "periods": [
            {
                "season": "all",
                "day_type": "all",
                "period_name": "all_day",
                "start_minute": 0,
                "end_minute": 1440,
                "price_per_kwh": price,
            }
        ],
    }


def _install_authoritative_holiday_calendar_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_parser = rate_sync_service.parse_sce_tou_public_page

    def parse_with_holidays(body: bytes, media_type: str) -> ParsedRateCandidate:
        parsed = original_parser(body, media_type)
        normalized = copy.deepcopy(parsed.normalized_rates)
        plans = normalized.get("plans")
        if isinstance(plans, list):
            for plan in plans:
                if isinstance(plan, dict):
                    plan["rate_precision"] = "approved_tariff_exact"
        normalized["holiday_calendar"] = {
            "status": "resolved",
            "authority": "Southern California Edison",
            "source_url": "https://www.sce.com/residential/rates/holidays",
            "coverage_start": "2026-01-01",
            "coverage_end": "2026-12-31",
            "holidays": [
                {"local_date": "2026-09-07", "name": "Labor Day"},
                {"local_date": "2026-11-26", "name": "Thanksgiving Day"},
                {"local_date": "2026-12-25", "name": "Christmas Day"},
            ],
        }
        return ParsedRateCandidate(
            normalized_rates=normalized,
            validation_evidence={
                **parsed.validation_evidence,
                "holiday_calendar": "authoritative_bounded_fixture",
                "warnings": [
                    warning
                    for warning in parsed.validation_evidence.get("warnings", [])
                    if warning != "PUBLIC_SOURCE_PRICES_ARE_DISPLAY_ROUNDED"
                ],
                "price_precision": "approved_tariff_exact_fixture",
            },
        )

    monkeypatch.setattr(rate_sync_service, "parse_sce_tou_public_page", parse_with_holidays)


@pytest.mark.parametrize(
    ("second_start", "second_end"),
    (
        ("2026-11-01T09:30:00+00:00", "2026-11-01T10:00:00+00:00"),
        ("2026-11-01T08:30:00+00:00", "2026-11-01T09:30:00+00:00"),
    ),
)
def test_dynamic_hourly_publication_rejects_gaps_and_overlaps(
    second_start: str, second_end: str
) -> None:
    plan = {
        "dated_prices": [
            {
                "start_utc": "2026-11-01T08:00:00+00:00",
                "end_utc": "2026-11-01T09:00:00+00:00",
                "price_per_kwh": "0.10000000",
            },
            {
                "start_utc": second_start,
                "end_utc": second_end,
                "price_per_kwh": "0.30000000",
            },
        ]
    }
    with pytest.raises(RateWorkflowConflict, match="gap or overlap"):
        _canonical_dated_prices(
            plan,
            effective_start=datetime(2026, 11, 1, 8, tzinfo=UTC),
            effective_end=datetime(2026, 11, 1, 10, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_manual_candidate_review_publish_activate_preserves_exact_provenance(
    owner_client: AsyncClient,
) -> None:
    scope = (await owner_client.get("/api/v1/home-scopes")).json()["home_scopes"][0]
    home_id = scope["id"]
    created = await owner_client.post(
        "/api/v1/rate-sources/manual-candidates",
        params={"home_id": home_id},
        json=_manual_payload(),
    )
    assert created.status_code == 201, created.text
    created_body = created.json()
    assert created_body["created"] is True
    assert created_body["network_fetch_performed"] is False
    candidate_id = created_body["candidate_id"]

    duplicate = await owner_client.post(
        "/api/v1/rate-sources/manual-candidates",
        params={"home_id": home_id},
        json=_manual_payload(),
    )
    assert duplicate.status_code == 201, duplicate.text
    assert duplicate.json()["created"] is False
    assert duplicate.json()["candidate_id"] == candidate_id

    reviewed = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{candidate_id}/review",
        params={"home_id": home_id},
        json={
            "selected_plan_name": "MANUAL-TOU-D",
            "effective_start": "2026-08-01T00:00:00-07:00",
            "administrator_confirmed_effective_date": True,
            "administrator_confirmed_provenance": True,
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["workflow"]["state"] == "reviewed"

    published = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{candidate_id}/publish",
        params={"home_id": home_id},
    )
    assert published.status_code == 201, published.text
    version_id = published.json()["rate_plan_version"]["id"]
    assert published.json()["workflow"]["state"] == "published"
    assert (
        published.json()["rate_plan_version"]["source_artifact_sha256"]
        == created_body["canonical_input_sha256"]
    )

    async with session_factory() as session:
        account_id = await session.scalar(
            select(UtilityAccount.id).where(UtilityAccount.home_id == home_id)
        )
        version = await session.get(RatePlanVersion, version_id)
        periods = (
            await session.scalars(
                select(RatePeriod).where(RatePeriod.rate_plan_version_id == version_id)
            )
        ).all()
        assert account_id is not None
        assert version is not None
        assert version.daily_fixed_charge == Decimal("0.79000000")
        assert version.baseline_credit_per_kwh == Decimal("0.10000000")
        assert len(periods) == 1
        assert periods[0].price_per_kwh == Decimal("0.12345678")

    activated = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{candidate_id}/activate",
        params={"home_id": home_id},
        json={"utility_account_id": account_id},
    )
    assert activated.status_code == 201, activated.text
    assert activated.json()["workflow"]["state"] == "activated"

    status = await owner_client.get("/api/v1/rate-sources/status", params={"home_id": home_id})
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["active"]["state"] == "active"
    assert body["active"]["rate_plan_version_id"] == version_id
    assert body["active"]["provenance"]["origin"] == "manual_administrator_entry"
    assert body["last_known_good"]["candidate_id"] == candidate_id
    assert body["last_known_good"]["active_source_match"] is True
    # Official-source chronology is intentionally separate from manual entry.
    assert body["last_run"] is None
    assert body["last_success"] is None
    assert body["last_failure"] is None

    async with session_factory() as session:
        failed_run = RateSyncRun(
            source_id=created_body["source_id"],
            home_id=home_id,
            state="failed",
            event_code="RATE_SYNC_FAILED",
            correlation_id="exact-home-status-failure",
            requested_url="manual-entry:no-network-fetch",
            error_code="CONNECT_TIMEOUT",
            completed_at=datetime.now(UTC),
            evidence={"initiator": "user", "failure_phase": "fetch"},
        )
        session.add(failed_run)
        await session.commit()
    failed_status = await owner_client.get(
        "/api/v1/rate-sources/status", params={"home_id": home_id}
    )
    assert failed_status.status_code == 200, failed_status.text
    failed_body = failed_status.json()
    assert failed_body["last_failure"] is None
    assert failed_body["last_run"] is None
    assert failed_body["last_known_good"]["candidate_id"] == candidate_id
    assert failed_body["active"]["rate_plan_version_id"] == version_id


@pytest.mark.asyncio
async def test_semantic_tier_candidate_cannot_publish_without_account_baseline(
    owner_client: AsyncClient,
    rate_artifact_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_authoritative_holiday_calendar_parser(monkeypatch)
    home_id = (await owner_client.get("/api/v1/home-scopes")).json()["home_scopes"][0]["id"]
    async with session_factory() as session:
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
        )
        session.add(source)
        await session.flush()

        async def fetch(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
            return _fetched(
                b"""<html><body><h1>Tiered Rate Plan</h1><p>Schedule D DOMESTIC</p>
                <p>Current rates as of 6/1/26.</p>
                <h2>Tier 1</h2><p>30 cents per kWh</p><p>Up to baseline allocation</p>
                <h2>Tier 2</h2><p>40 cents per kWh</p><p>Over baseline allocation</p>
                <p>79 cents daily Base Services Charge</p>
                <p>Summer Daily Allocations (June - September)</p></body></html>"""
            )

        synced = await sync_official_rate_source(
            session,
            Settings(env="test", rate_artifact_dir=rate_artifact_dir),
            source,
            home_id=home_id,
            actor_user_id=None,
            correlation_id="semantic-tier-workflow",
            fetcher=fetch,
        )
        assert synced.candidate_id is not None
        await session.commit()
        candidate_id = synced.candidate_id

    reviewed = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{candidate_id}/review",
        params={"home_id": home_id},
        json={
            "selected_plan_name": "DOMESTIC",
            "effective_start": "2026-06-01T00:00:00-07:00",
            "administrator_confirmed_effective_date": True,
            "administrator_confirmed_provenance": True,
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    published = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{candidate_id}/publish",
        params={"home_id": home_id},
    )
    assert published.status_code == 409, published.text
    assert "account baseline evidence is required" in published.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unsupported_field", "unsupported_value", "message"),
    [
        ("minimum_charge", "1.00", "minimum_charge"),
        ("meter_charge", "1.00", "meter_charge"),
        ("other_fixed_charge", "1.00", "other_fixed_charge"),
        ("season_boundaries", True, "season boundaries"),
        ("day_type", "event", "day type"),
        ("day_type", "non_event", "day type"),
        ("pricing_model", "dynamic_hourly", "dynamic-hourly inputs"),
        ("missing_seasons", True, "season evidence"),
    ],
)
async def test_unsupported_rate_semantics_fail_closed_without_changing_published_lkg(
    owner_client: AsyncClient,
    unsupported_field: str,
    unsupported_value: object,
    message: str,
) -> None:
    del owner_client
    normalized: dict[str, object] = {
        "schema": "sce-rate-candidate/1.0.0",
        "utility_name": "Southern California Edison",
        "timezone": "America/Los_Angeles",
        "currency": "USD",
        "season_definitions": {
            "summer": {"start_month": 6, "end_month": 9},
            "winter": {"start_month": 10, "end_month": 5},
        },
        "plans": [
            {
                "rate_plan_name": "UNSUPPORTED-SEMANTICS",
                "rate_class": "residential",
                "pricing_model": "time_of_use",
                "daily_fixed_charge": "0",
                "monthly_fixed_charge": "0",
                "baseline_credit_per_kwh": "0",
                "periods": [
                    {
                        "season": "all",
                        "day_type": "all",
                        "name": "all_day",
                        "start_minute": 0,
                        "end_minute": 1440,
                        "price_per_kwh": "0.20",
                    }
                ],
            }
        ],
    }
    candidate_rates = copy.deepcopy(normalized)
    plan = candidate_rates["plans"][0]  # type: ignore[index]
    assert isinstance(plan, dict)
    if unsupported_field in {"minimum_charge", "meter_charge", "other_fixed_charge"}:
        plan[unsupported_field] = unsupported_value
    elif unsupported_field == "season_boundaries":
        candidate_rates["season_definitions"] = {
            "summer": {"start_month": 5, "end_month": 10},
            "winter": {"start_month": 9, "end_month": 4},
        }
    elif unsupported_field == "pricing_model":
        plan["pricing_model"] = unsupported_value
    elif unsupported_field == "missing_seasons":
        candidate_rates.pop("season_definitions")
        plan["pricing_model"] = "seasonal_time_of_use"
        periods = plan["periods"]
        assert isinstance(periods, list) and isinstance(periods[0], dict)
        periods[0]["season"] = "summer"
    else:
        periods = plan["periods"]
        assert isinstance(periods, list) and isinstance(periods[0], dict)
        if unsupported_field == "day_type":
            periods[0]["day_type"] = unsupported_value
        else:
            raise AssertionError(f"unexpected unsupported field: {unsupported_field}")

    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        actor_id = await session.scalar(select(User.id))
        assert home_id is not None and actor_id is not None
        published_before = tuple(
            await session.scalars(
                select(RatePlanVersion.id)
                .where(RatePlanVersion.state == "published")
                .order_by(RatePlanVersion.id)
            )
        )
        assignments_before = tuple(
            await session.scalars(select(RateAssignment.id).order_by(RateAssignment.id))
        )
        candidate = RateCandidate(
            source_revision_id=str(uuid.uuid4()),
            normalized_rates=candidate_rates,
            validation_evidence={"coverage": "complete"},
            home_id=home_id,
            canonical_input_sha256=uuid.uuid4().hex * 2,
        )
        review = RateCandidateReview(
            candidate_id=str(uuid.uuid4()),
            home_id=home_id,
            selected_plan_name="UNSUPPORTED-SEMANTICS",
            effective_start=datetime(2026, 8, 1, tzinfo=UTC),
            state="reviewed",
            reviewed_by_user_id=actor_id,
        )
        with pytest.raises(RateWorkflowConflict, match=message):
            await publish_rate_candidate(
                session,
                candidate=candidate,
                review=review,
                actor_user_id=actor_id,
                correlation_id="unsupported-rate-semantics",
            )
        assert (
            tuple(
                await session.scalars(
                    select(RatePlanVersion.id)
                    .where(RatePlanVersion.state == "published")
                    .order_by(RatePlanVersion.id)
                )
            )
            == published_before
        )
        assert (
            tuple(await session.scalars(select(RateAssignment.id).order_by(RateAssignment.id)))
            == assignments_before
        )


@pytest.mark.asyncio
async def test_publish_supports_custom_seasons_event_override_midnight_and_fixed_semantics(
    owner_client: AsyncClient,
) -> None:
    del owner_client
    normalized = {
        "schema": "sce-rate-candidate/1.0.0",
        "utility_name": "Southern California Edison",
        "timezone": "America/Los_Angeles",
        "currency": "USD",
        "holiday_treatment": "no_special_treatment",
        "season_definitions": [
            {
                "season_name": "warm",
                "start_month": 5,
                "start_day": 15,
                "end_month": 10,
                "end_day": 14,
                "source_label": "official warm season",
            },
            {
                "season_name": "cool",
                "start_month": 10,
                "start_day": 15,
                "end_month": 5,
                "end_day": 14,
                "source_label": "official cool season",
            },
        ],
        "event_calendar": {
            "status": "complete",
            "coverage_start": "2026-08-01",
            "coverage_end": "2026-08-31",
            "local_dates": ["2026-08-10"],
        },
        "plans": [
            {
                "rate_plan_name": "EVENT-MIDNIGHT",
                "rate_class": "residential",
                "pricing_model": "critical_peak_pricing",
                "daily_fixed_charge": "0",
                "monthly_fixed_charge": "0",
                "minimum_charge": "0",
                "meter_charge": "0",
                "other_fixed_charge": "1.25",
                "fixed_charges": [
                    {
                        "charge": "other_fixed_charge",
                        "amount": "1.25",
                        "applies": "per_account_per_cycle",
                    }
                ],
                "baseline_credit_per_kwh": "0",
                "periods": [
                    {
                        "season": "all",
                        "day_type": "all_days",
                        "name": "ordinary",
                        "start_minute": 0,
                        "end_minute": 1440,
                        "price_per_kwh": "0.20",
                    },
                    {
                        "season": "warm",
                        "day_type": "event_day",
                        "name": "overnight_event",
                        "start_minute": 1320,
                        "end_minute": 360,
                        "price_per_kwh": "0.80",
                    },
                ],
            }
        ],
    }
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        actor_id = await session.scalar(select(User.id))
        assert home_id is not None and actor_id is not None
        source = RateSource(
            name="Exact event publication fixture",
            source_type="official_https",
            https_url="https://www.sce.com/regulatory/tariff-books/rates-pricing-choices",
            enabled=False,
        )
        session.add(source)
        await session.flush()
        revision = RateSourceRevision(
            source_id=source.id,
            artifact_sha256="e" * 64,
            parser_version="event-publication-test-v1",
        )
        session.add(revision)
        await session.flush()
        candidate = RateCandidate(
            source_revision_id=revision.id,
            normalized_rates=normalized,
            validation_evidence={"coverage": "complete"},
            home_id=home_id,
            canonical_input_sha256="d" * 64,
        )
        session.add(candidate)
        await session.flush()
        review = RateCandidateReview(
            candidate_id=candidate.id,
            home_id=home_id,
            selected_plan_name="EVENT-MIDNIGHT",
            effective_start=datetime(2026, 8, 1, 7, tzinfo=UTC),
            effective_end=datetime(2026, 9, 1, 7, tzinfo=UTC),
            state="reviewed",
            reviewed_by_user_id=actor_id,
        )
        session.add(review)
        await session.flush()
        _plan, version = await publish_rate_candidate(
            session,
            candidate=candidate,
            review=review,
            actor_user_id=actor_id,
            correlation_id="event-publication",
        )
        periods = (
            await session.scalars(
                select(RatePeriod)
                .where(RatePeriod.rate_plan_version_id == version.id)
                .order_by(RatePeriod.start_minute, RatePeriod.end_minute)
            )
        ).all()
        assert version.algorithm_version == "cost-v2"
        assert version.other_fixed_charge == Decimal("1.25")
        assert (
            next(
                item["applies"]
                for item in version.fixed_charges
                if item["charge"] == "other_fixed_charge"
            )
            == "per_account_per_cycle"
        )
        assert version.season_definitions[0]["start_day"] == 15
        assert any(
            item.get("evidence_type") == "event_calendar"
            for item in version.eligibility_evidence
            if isinstance(item, dict)
        )
        event_periods = [period for period in periods if period.day_type == "event_day"]
        assert [(period.start_minute, period.end_minute) for period in event_periods] == [
            (0, 360),
            (1320, 1440),
        ]


@pytest.mark.asyncio
async def test_publish_preserves_executable_tier_bounds_and_exact_components(
    owner_client: AsyncClient,
) -> None:
    del owner_client
    normalized = {
        "schema": "sce-rate-candidate/1.0.0",
        "utility_name": "Southern California Edison",
        "timezone": "America/Los_Angeles",
        "currency": "USD",
        "holiday_treatment": "not_applicable",
        "plans": [
            {
                "rate_plan_name": "EXACT-TIERED",
                "rate_class": "residential",
                "pricing_model": "tiered",
                "periods": [
                    {
                        "season": "all",
                        "day_type": "all",
                        "name": "tier_1",
                        "start_minute": 0,
                        "end_minute": 1440,
                        "price_per_kwh": "0.30",
                        "tier_min_kwh": "0",
                        "tier_max_kwh": "100",
                        "boundary_inclusive": True,
                        "threshold_basis": "exact_cycle_kwh",
                        "threshold_value": "100",
                        "rate_components": [
                            {
                                "component": "delivery_rate",
                                "amount_per_kwh": "0.18",
                                "source_status": "exact",
                            },
                            {
                                "component": "generation_rate",
                                "amount_per_kwh": "0.12",
                                "source_status": "exact",
                            },
                        ],
                    },
                    {
                        "season": "all",
                        "day_type": "all",
                        "name": "tier_2",
                        "start_minute": 0,
                        "end_minute": 1440,
                        "price_per_kwh": "0.40",
                        "tier_start_kwh": "100",
                        "tier_end_kwh": None,
                        "boundary_inclusive": True,
                        "threshold_basis": "exact_cycle_kwh",
                        "threshold_value": "100",
                        "rate_components": [
                            {
                                "component": "delivery_rate",
                                "amount_per_kwh": "0.25",
                                "source_status": "exact",
                            },
                            {
                                "component": "generation_rate",
                                "amount_per_kwh": "0.15",
                                "source_status": "exact",
                            },
                        ],
                    },
                ],
            }
        ],
    }
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        actor_id = await session.scalar(select(User.id))
        assert home_id is not None and actor_id is not None
        source = RateSource(
            name="Exact tier publication fixture",
            source_type="official_https",
            https_url="https://www.sce.com/regulatory/tariff-books/tiered",
            enabled=False,
        )
        session.add(source)
        await session.flush()
        revision = RateSourceRevision(
            source_id=source.id,
            artifact_sha256="7" * 64,
            parser_version="exact-tier-test-v1",
        )
        session.add(revision)
        await session.flush()
        candidate = RateCandidate(
            source_revision_id=revision.id,
            normalized_rates=normalized,
            validation_evidence={"coverage": "complete"},
            home_id=home_id,
            canonical_input_sha256="8" * 64,
        )
        session.add(candidate)
        await session.flush()
        review = RateCandidateReview(
            candidate_id=candidate.id,
            home_id=home_id,
            selected_plan_name="EXACT-TIERED",
            effective_start=datetime(2026, 8, 1, tzinfo=UTC),
            state="reviewed",
            reviewed_by_user_id=actor_id,
        )
        session.add(review)
        await session.flush()
        _plan, version = await publish_rate_candidate(
            session,
            candidate=candidate,
            review=review,
            actor_user_id=actor_id,
            correlation_id="exact-tier-publication",
        )
        periods = (
            await session.scalars(
                select(RatePeriod)
                .where(RatePeriod.rate_plan_version_id == version.id)
                .order_by(RatePeriod.tier_start_kwh)
            )
        ).all()
        assert [(period.tier_start_kwh, period.tier_end_kwh) for period in periods] == [
            (Decimal("0"), Decimal("100")),
            (Decimal("100"), None),
        ]
        assert periods[0].delivery_per_kwh == Decimal("0.18")
        assert periods[0].generation_per_kwh == Decimal("0.12")
        assert periods[0].threshold_basis == "exact_cycle_kwh"
        assert periods[0].threshold_value == Decimal("100")
        assert periods[0].rate_components[0]["source_status"] == "exact"
        assert version.season_definitions[0]["season_name"] == "all_year"


@pytest.mark.asyncio
async def test_publish_resolves_reviewed_daily_tier_threshold_from_exact_evidence(
    owner_client: AsyncClient,
) -> None:
    del owner_client
    normalized = {
        "schema": "sce-rate-candidate/1.0.0",
        "utility_name": "Southern California Edison",
        "timezone": "America/Los_Angeles",
        "currency": "USD",
        "holiday_treatment": "not_applicable",
        "season_definitions": {
            "summer": {"start_month": 6, "end_month": 9},
            "winter": {"start_month": 10, "end_month": 5},
        },
        "plans": [
            {
                "rate_plan_name": "EXACT-DOMESTIC",
                "rate_class": "residential",
                "pricing_model": "seasonal_tiered",
                "periods": [
                    {
                        "season": "all",
                        "day_type": "all",
                        "name": "tier_1",
                        "start_minute": 0,
                        "end_minute": 1440,
                        "price_per_kwh": "0.30863",
                        "tier_min_kwh": "0",
                        "tier_max_kwh": None,
                    },
                    {
                        "season": "all",
                        "day_type": "all",
                        "name": "tier_2",
                        "start_minute": 0,
                        "end_minute": 1440,
                        "price_per_kwh": "0.40962",
                        "tier_min_kwh": None,
                        "tier_max_kwh": None,
                    },
                ],
            }
        ],
    }
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        actor_id = await session.scalar(select(User.id))
        assert home_id is not None and actor_id is not None
        source = RateSource(
            name="Exact DOMESTIC tariff fixture",
            source_type="official_https",
            https_url="https://www.sce.com/regulatory/tariff-books/domestic",
            enabled=False,
        )
        session.add(source)
        await session.flush()
        revision = RateSourceRevision(
            source_id=source.id,
            artifact_sha256="4" * 64,
            parser_version="exact-domestic-v1",
        )
        session.add(revision)
        await session.flush()
        candidate = RateCandidate(
            source_revision_id=revision.id,
            normalized_rates=normalized,
            validation_evidence={
                "coverage": "semantic_tier_coverage",
                "warnings": [],
            },
            home_id=home_id,
            canonical_input_sha256="5" * 64,
        )
        session.add(candidate)
        await session.flush()
        review = RateCandidateReview(
            candidate_id=candidate.id,
            home_id=home_id,
            selected_plan_name="EXACT-DOMESTIC",
            effective_start=datetime(2026, 6, 1, 7, tzinfo=UTC),
            tier_threshold_rule={
                "rule_type": "daily_allowance",
                "season": "summer",
                "kwh_per_day": "19.3",
                "source_allowance_kwh": "579.0",
                "source_billing_days": 30,
                "tier1_boundary_inclusive": True,
                "source_label": "approved Schedule D baseline table",
            },
            state="reviewed",
            reviewed_by_user_id=actor_id,
        )
        session.add(review)
        await session.flush()
        rounded_candidate = RateCandidate(
            source_revision_id=revision.id,
            normalized_rates=normalized,
            validation_evidence={
                "coverage": "semantic_tier_coverage",
                "warnings": ["PUBLIC_SOURCE_PRICES_ARE_DISPLAY_ROUNDED"],
            },
            home_id=home_id,
            canonical_input_sha256="9" * 64,
        )
        with pytest.raises(RateWorkflowConflict, match="exact tariff prices are required"):
            await publish_rate_candidate(
                session,
                candidate=rounded_candidate,
                review=review,
                actor_user_id=actor_id,
                correlation_id="rounded-domestic-review-only",
            )
        _plan, version = await publish_rate_candidate(
            session,
            candidate=candidate,
            review=review,
            actor_user_id=actor_id,
            correlation_id="exact-domestic-threshold",
        )
        periods = (
            await session.scalars(
                select(RatePeriod)
                .where(RatePeriod.rate_plan_version_id == version.id)
                .order_by(RatePeriod.tier_start_kwh)
            )
        ).all()
        assert version.tier_threshold_kwh_per_day is None
        assert version.tier_threshold_season is None
        assert version.tier_threshold_source_kwh is None
        assert version.tier_threshold_source_days is None
        assert [(item.tier_start_kwh, item.tier_end_kwh) for item in periods] == [
            (Decimal("0"), Decimal("1")),
            (Decimal("1"), None),
        ]
        assert {item.threshold_basis for item in periods} == {"account_daily_baseline"}
        account = await session.scalar(
            select(UtilityAccount).where(UtilityAccount.home_id == home_id)
        )
        assert account is not None
        await activate_rate_candidate(
            session,
            candidate=candidate,
            review=review,
            utility_account_id=account.id,
            actor_user_id=actor_id,
            correlation_id="activate-first-home-domestic",
        )
        first_rule = await resolve_utility_account_tier_threshold(
            session,
            utility_account_id=account.id,
            rate_plan_id=version.rate_plan_id,
            season="summer",
            instant=datetime(2026, 6, 15, 7, tzinfo=UTC),
        )
        assert first_rule is not None and first_rule.kwh_per_day == Decimal("19.3")

        second_home = Home(name="Second baseline home", timezone="America/Los_Angeles")
        session.add(second_home)
        await session.flush()
        second_account = UtilityAccount(
            home_id=second_home.id,
            timezone="America/Los_Angeles",
            billing_day=15,
            cost_scope="full_account",
        )
        session.add(second_account)
        await session.flush()
        await replace_rate_assignment(
            session,
            account=second_account,
            version=version,
            actor_user_id=actor_id,
        )
        for target_account, summer_daily, winter_daily, source_prefix in (
            (account, Decimal("19.3"), Decimal("10"), "first"),
            (second_account, Decimal("10"), Decimal("5"), "second"),
        ):
            if target_account.id != account.id:
                await replace_utility_account_tier_threshold(
                    session,
                    account=target_account,
                    rate_plan_id=version.rate_plan_id,
                    season="summer",
                    kwh_per_day=summer_daily,
                    source_allowance_kwh=summer_daily * 30,
                    source_billing_days=30,
                    tier1_boundary_inclusive=True,
                    source_label=f"{source_prefix} home summer evidence",
                    source_kind="candidate_review",
                    source_artifact_sha256=revision.artifact_sha256,
                    effective_start=datetime(2026, 6, 1, 7, tzinfo=UTC),
                    effective_end=None,
                    actor_user_id=actor_id,
                )
            await replace_utility_account_tier_threshold(
                session,
                account=target_account,
                rate_plan_id=version.rate_plan_id,
                season="winter",
                kwh_per_day=winter_daily,
                source_allowance_kwh=winter_daily * 30,
                source_billing_days=30,
                tier1_boundary_inclusive=True,
                source_label=f"{source_prefix} home winter evidence",
                source_kind="candidate_review",
                source_artifact_sha256=revision.artifact_sha256,
                effective_start=datetime(2026, 6, 1, 7, tzinfo=UTC),
                effective_end=None,
                actor_user_id=actor_id,
            )
        await replace_utility_account_tier_threshold(
            session,
            account=account,
            rate_plan_id=version.rate_plan_id,
            season="summer",
            kwh_per_day=Decimal("20"),
            source_allowance_kwh=Decimal("600"),
            source_billing_days=30,
            tier1_boundary_inclusive=True,
            source_label="first home effective July evidence",
            source_kind="candidate_review",
            source_artifact_sha256=revision.artifact_sha256,
            effective_start=datetime(2026, 7, 1, 7, tzinfo=UTC),
            effective_end=None,
            actor_user_id=actor_id,
        )
        first_mixed_effective = await resolve_utility_account_cycle_tier_threshold(
            session,
            utility_account_id=account.id,
            rate_plan_id=version.rate_plan_id,
            season_definitions=version.season_definitions,
            timezone=version.timezone,
            cycle_start=datetime(2026, 6, 15, 7, tzinfo=UTC),
            cycle_end=datetime(2026, 7, 15, 7, tzinfo=UTC),
        )
        first_mixed_season = await resolve_utility_account_cycle_tier_threshold(
            session,
            utility_account_id=account.id,
            rate_plan_id=version.rate_plan_id,
            season_definitions=version.season_definitions,
            timezone=version.timezone,
            cycle_start=datetime(2026, 9, 15, 7, tzinfo=UTC),
            cycle_end=datetime(2026, 10, 15, 7, tzinfo=UTC),
        )
        second_summer = await resolve_utility_account_cycle_tier_threshold(
            session,
            utility_account_id=second_account.id,
            rate_plan_id=version.rate_plan_id,
            season_definitions=version.season_definitions,
            timezone=version.timezone,
            cycle_start=datetime(2026, 7, 1, 7, tzinfo=UTC),
            cycle_end=datetime(2026, 7, 31, 7, tzinfo=UTC),
        )
        second_summer_28 = await resolve_utility_account_cycle_tier_threshold(
            session,
            utility_account_id=second_account.id,
            rate_plan_id=version.rate_plan_id,
            season_definitions=version.season_definitions,
            timezone=version.timezone,
            cycle_start=datetime(2026, 7, 1, 7, tzinfo=UTC),
            cycle_end=datetime(2026, 7, 29, 7, tzinfo=UTC),
        )
        second_summer_31 = await resolve_utility_account_cycle_tier_threshold(
            session,
            utility_account_id=second_account.id,
            rate_plan_id=version.rate_plan_id,
            season_definitions=version.season_definitions,
            timezone=version.timezone,
            cycle_start=datetime(2026, 7, 1, 7, tzinfo=UTC),
            cycle_end=datetime(2026, 8, 1, 7, tzinfo=UTC),
        )
        first_dst_cycle = await resolve_utility_account_cycle_tier_threshold(
            session,
            utility_account_id=account.id,
            rate_plan_id=version.rate_plan_id,
            season_definitions=version.season_definitions,
            timezone=version.timezone,
            cycle_start=datetime(2026, 11, 1, 7, tzinfo=UTC),
            cycle_end=datetime(2026, 12, 1, 8, tzinfo=UTC),
        )
        assert first_mixed_effective is not None
        assert first_mixed_effective.total_kwh == Decimal("588.8")
        assert first_mixed_season is not None
        assert first_mixed_season.total_kwh == Decimal("460")
        assert second_summer is not None and second_summer.total_kwh == Decimal("300")
        assert second_summer_28 is not None and second_summer_28.total_kwh == Decimal("280")
        assert second_summer_31 is not None and second_summer_31.total_kwh == Decimal("310")
        assert first_dst_cycle is not None and first_dst_cycle.total_kwh == Decimal("300")
        await session.refresh(version)
        unchanged_periods = (
            await session.scalars(
                select(RatePeriod)
                .where(RatePeriod.rate_plan_version_id == version.id)
                .order_by(RatePeriod.tier_start_kwh)
            )
        ).all()
        assert version.tier_threshold_kwh_per_day is None
        assert [(item.tier_start_kwh, item.tier_end_kwh) for item in unchanged_periods] == [
            (Decimal("0"), Decimal("1")),
            (Decimal("1"), None),
        ]
        assert (
            int(await session.scalar(select(func.count(UtilityAccountTierThreshold.id))) or 0) == 5
        )


@pytest.mark.asyncio
async def test_publish_dynamic_prices_binds_catalog_metadata_and_exact_utc_rows(
    owner_client: AsyncClient,
) -> None:
    del owner_client
    start = datetime(2026, 11, 1, 8, tzinfo=UTC)
    end = datetime(2026, 11, 1, 10, tzinfo=UTC)
    dynamic_plan = {
        "rate_plan_name": "DYNAMIC-TEST",
        "rate_class": "residential",
        "pricing_model": "dynamic_hourly",
        "description": "Official bounded hourly pilot schedule.",
        "dated_prices": [
            {
                "start_utc": "2026-11-01T08:00:00Z",
                "end_utc": "2026-11-01T09:00:00Z",
                "price_per_kwh": "0.10",
                "rate_components": [
                    {
                        "component": "delivery_rate",
                        "amount_per_kwh": "0.04",
                        "source_status": "exact",
                    },
                    {
                        "component": "generation_rate",
                        "amount_per_kwh": "0.06",
                        "source_status": "exact",
                    },
                ],
                "source_label": "first repeated 1 AM hour",
            },
            {
                "start_utc": "2026-11-01T09:00:00Z",
                "end_utc": "2026-11-01T10:00:00Z",
                "price_per_kwh": "0.30",
                "rate_components": [
                    {
                        "component": "delivery_rate",
                        "amount_per_kwh": "0.12",
                        "source_status": "exact",
                    },
                    {
                        "component": "generation_rate",
                        "amount_per_kwh": "0.18",
                        "source_status": "exact",
                    },
                ],
                "source_label": "second repeated 1 AM hour",
            },
        ],
    }
    normalized = {
        "schema": "sce-rate-candidate/1.0.0",
        "utility_name": "Southern California Edison",
        "timezone": "America/Los_Angeles",
        "currency": "USD",
        "holiday_treatment": "not_applicable",
        "plans": [dynamic_plan],
    }
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        actor_id = await session.scalar(select(User.id))
        assert home_id is not None and actor_id is not None
        source = RateSource(
            name="Official bounded dynamic pilot fixture",
            source_type="official_https",
            https_url="https://www.sce.com/regulatory/tariff-books/dynamic-pilot",
            enabled=False,
        )
        session.add(source)
        await session.flush()
        revision = RateSourceRevision(
            source_id=source.id,
            artifact_sha256="6" * 64,
            parser_version="dynamic-hourly-v1",
        )
        session.add(revision)
        await session.flush()
        session.add(
            SceCatalogEntry(
                source_revision_id=revision.id,
                source_url=source.https_url,
                source_level=1,
                official_schedule_code="DYNAMIC-PILOT",
                public_plan_name="SCE Dynamic Hourly Pilot",
                canonical_name="DYNAMIC-TEST",
                plan_type="dynamic_hourly",
                enrollment_status="pilot",
                eligibility=["approved_pilot_participant"],
                discovery_state="parsed",
                normalized_plan=dynamic_plan,
            )
        )
        candidate = RateCandidate(
            source_revision_id=revision.id,
            normalized_rates=normalized,
            validation_evidence={"coverage": "complete"},
            home_id=home_id,
            canonical_input_sha256="7" * 64,
        )
        session.add(candidate)
        await session.flush()
        review = RateCandidateReview(
            candidate_id=candidate.id,
            home_id=home_id,
            selected_plan_name="DYNAMIC-TEST",
            effective_start=start,
            effective_end=end,
            state="reviewed",
            reviewed_by_user_id=actor_id,
        )
        session.add(review)
        await session.flush()
        plan, version = await publish_rate_candidate(
            session,
            candidate=candidate,
            review=review,
            actor_user_id=actor_id,
            correlation_id="dynamic-hourly-publication",
        )
        rows = (
            await session.scalars(
                select(RateDatedPrice)
                .where(RateDatedPrice.rate_plan_version_id == version.id)
                .order_by(RateDatedPrice.start_utc)
            )
        ).all()
        assert plan.official_schedule_code == "DYNAMIC-PILOT"
        assert plan.public_plan_name == "SCE Dynamic Hourly Pilot"
        assert plan.canonical_name == "DYNAMIC-TEST"
        assert plan.enrollment_status == "pilot"
        assert plan.eligibility == ["approved_pilot_participant"]
        assert plan.description == "Official bounded hourly pilot schedule."
        assert [item.price_per_kwh for item in rows] == [Decimal("0.10"), Decimal("0.30")]
        metadata = next(
            item
            for item in version.eligibility_evidence
            if isinstance(item, dict) and item.get("evidence_type") == "official_plan_metadata"
        )
        assert metadata["catalog_entry_id"]
        assert metadata["official_schedule_code"] == "DYNAMIC-PILOT"


@pytest.mark.asyncio
async def test_publish_event_schedule_without_bounded_calendar_fails_closed(
    owner_client: AsyncClient,
) -> None:
    del owner_client
    normalized = {
        "schema": "sce-rate-candidate/1.0.0",
        "utility_name": "Southern California Edison",
        "timezone": "America/Los_Angeles",
        "currency": "USD",
        "holiday_treatment": "event_calendar_required",
        "season_definitions": {
            "summer": {"start_month": 6, "end_month": 9},
            "winter": {"start_month": 10, "end_month": 5},
        },
        "plans": [
            {
                "rate_plan_name": "UNRESOLVED-EVENT",
                "rate_class": "residential",
                "pricing_model": "critical_peak_pricing",
                "periods": [
                    {
                        "season": "all",
                        "day_type": "all",
                        "name": "ordinary",
                        "start_minute": 0,
                        "end_minute": 1440,
                        "price_per_kwh": "0.20",
                    },
                    {
                        "season": "summer",
                        "day_type": "event_day",
                        "name": "event",
                        "start_minute": 960,
                        "end_minute": 1260,
                        "price_per_kwh": "0.80",
                    },
                ],
            }
        ],
    }
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        actor_id = await session.scalar(select(User.id))
        assert home_id is not None and actor_id is not None
        candidate = RateCandidate(
            source_revision_id=str(uuid.uuid4()),
            normalized_rates=normalized,
            validation_evidence={"coverage": "complete"},
            home_id=home_id,
            canonical_input_sha256=uuid.uuid4().hex * 2,
        )
        review = RateCandidateReview(
            candidate_id=str(uuid.uuid4()),
            home_id=home_id,
            selected_plan_name="UNRESOLVED-EVENT",
            effective_start=datetime(2026, 8, 1, 7, tzinfo=UTC),
            effective_end=datetime(2026, 9, 1, 7, tzinfo=UTC),
            state="reviewed",
            reviewed_by_user_id=actor_id,
        )
        with pytest.raises(RateWorkflowConflict, match="event calendar is unresolved"):
            await publish_rate_candidate(
                session,
                candidate=candidate,
                review=review,
                actor_user_id=actor_id,
                correlation_id="unresolved-event",
            )


@pytest.mark.asyncio
async def test_holiday_sensitive_publication_requires_authoritative_bounded_calendar(
    owner_client: AsyncClient,
) -> None:
    del owner_client
    normalized: dict[str, object] = {
        "schema": "sce-rate-candidate/1.0.0",
        "utility_name": "Southern California Edison",
        "timezone": "America/Los_Angeles",
        "currency": "USD",
        "holiday_treatment": "same_as_weekend",
        "season_definitions": {"all_year": {"start_month": 1, "end_month": 12}},
        "plans": [
            {
                "rate_plan_name": "HOLIDAY-BOUNDED",
                "rate_class": "residential",
                "pricing_model": "time_of_use",
                "periods": [
                    {
                        "season": "all_year",
                        "day_type": "weekday",
                        "name": "weekday",
                        "start_minute": 0,
                        "end_minute": 1440,
                        "price_per_kwh": "0.20",
                    },
                    {
                        "season": "all_year",
                        "day_type": "weekend_holiday",
                        "name": "weekend_holiday",
                        "start_minute": 0,
                        "end_minute": 1440,
                        "price_per_kwh": "0.10",
                    },
                ],
            }
        ],
    }
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
        actor_id = await session.scalar(select(User.id))
        assert home_id is not None and actor_id is not None
        candidate = RateCandidate(
            source_revision_id=str(uuid.uuid4()),
            normalized_rates=normalized,
            validation_evidence={"coverage": "complete"},
            home_id=home_id,
            canonical_input_sha256=uuid.uuid4().hex * 2,
        )
        review = RateCandidateReview(
            candidate_id=str(uuid.uuid4()),
            home_id=home_id,
            selected_plan_name="HOLIDAY-BOUNDED",
            effective_start=datetime(2026, 8, 1, tzinfo=UTC),
            effective_end=datetime(2027, 1, 1, tzinfo=UTC),
            state="reviewed",
            reviewed_by_user_id=actor_id,
        )
        with pytest.raises(RateWorkflowConflict, match="holiday calendar evidence"):
            await publish_rate_candidate(
                session,
                candidate=candidate,
                review=review,
                actor_user_id=actor_id,
                correlation_id="missing-holiday-calendar",
            )
        candidate.normalized_rates = {
            **normalized,
            "holiday_calendar": {
                "status": "resolved",
                "authority": "Southern California Edison",
                "source_url": "https://www.sce.com/residential/rates/holidays",
                "coverage_start": "2026-01-01",
                "coverage_end": "2026-12-31",
                "holidays": [],
            },
        }
        review.effective_end = None
        with pytest.raises(RateWorkflowConflict, match="holiday calendar coverage"):
            await publish_rate_candidate(
                session,
                candidate=candidate,
                review=review,
                actor_user_id=actor_id,
                correlation_id="open-holiday-calendar",
            )


@pytest.mark.asyncio
async def test_official_status_is_not_masked_by_a_later_manual_candidate(
    owner_client: AsyncClient,
) -> None:
    home_id = (await owner_client.get("/api/v1/home-scopes")).json()["home_scopes"][0]["id"]
    completed_at = datetime(2026, 8, 15, 1, 0, tzinfo=UTC)
    async with session_factory() as session:
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
        )
        session.add(source)
        await session.flush()
        failed_run = RateSyncRun(
            source_id=source.id,
            home_id=home_id,
            state="failed",
            event_code="RATE_SYNC_FAILED",
            correlation_id="official-failed-before-manual",
            requested_url=SCE_TOU_URL,
            final_url=SCE_TOU_URL,
            error_code="READ_TIMEOUT",
            started_at=completed_at,
            completed_at=completed_at,
            evidence={"initiator": "scheduled_worker", "failure_phase": "fetch"},
        )
        session.add(failed_run)
        await session.commit()
        source_id = source.id
        failed_run_id = failed_run.id

    created = await owner_client.post(
        "/api/v1/rate-sources/manual-candidates",
        params={"home_id": home_id},
        json=_manual_payload(),
    )
    assert created.status_code == 201, created.text

    status = await owner_client.get("/api/v1/rate-sources/status", params={"home_id": home_id})
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["last_run"]["id"] == failed_run_id
    assert body["last_failure"]["id"] == failed_run_id
    assert body["last_failure"]["error_code"] == "READ_TIMEOUT"
    assert body["last_failure"]["source_id"] == source_id
    assert body["last_failure"]["source_name"] == SCE_SOURCE_NAME
    assert body["last_failure"]["source_type"] == "official_https"
    assert body["last_failure"]["source_url"] == SCE_TOU_URL
    assert body["last_success"] is None
    assert body["last_known_good"]["source_type"] == "manual_administrator_entry"


@pytest.mark.asyncio
async def test_reject_is_exact_home_terminal_and_audited(owner_client: AsyncClient) -> None:
    home_id = (await owner_client.get("/api/v1/home-scopes")).json()["home_scopes"][0]["id"]
    created = await owner_client.post(
        "/api/v1/rate-sources/manual-candidates",
        params={"home_id": home_id},
        json=_manual_payload(),
    )
    assert created.status_code == 201, created.text
    candidate_id = created.json()["candidate_id"]

    rejected = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{candidate_id}/reject",
        params={"home_id": home_id},
    )
    assert rejected.status_code == 200, rejected.text
    workflow = rejected.json()["workflow"]
    assert workflow["state"] == "rejected"
    assert workflow["selected_plan_name"] is None
    assert workflow["effective_start"] is None
    assert workflow["rate_plan_version_id"] is None
    assert workflow["utility_account_id"] is None

    repeated = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{candidate_id}/reject",
        params={"home_id": home_id},
    )
    assert repeated.status_code == 409
    reviewed = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{candidate_id}/review",
        params={"home_id": home_id},
        json={
            "selected_plan_name": "MANUAL-TOU-D",
            "effective_start": "2026-08-01T00:00:00-07:00",
            "administrator_confirmed_effective_date": True,
            "administrator_confirmed_provenance": True,
        },
    )
    assert reviewed.status_code == 409

    async with session_factory() as session:
        review = await session.scalar(
            select(RateCandidateReview).where(
                RateCandidateReview.candidate_id == candidate_id,
                RateCandidateReview.home_id == home_id,
            )
        )
        assert review is not None and review.state == "rejected"
        audits = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_code == "RATE_CANDIDATE_REJECTED",
                    AuditEvent.target_id == review.id,
                )
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].details == {"candidate_id": candidate_id, "home_id": home_id}


@pytest.mark.asyncio
async def test_official_candidate_has_authorized_publish_path_and_cost_compatible_periods(
    owner_client: AsyncClient,
    rate_artifact_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_authoritative_holiday_calendar_parser(monkeypatch)
    home_id = (await owner_client.get("/api/v1/home-scopes")).json()["home_scopes"][0]["id"]
    async with session_factory() as session:
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
        )
        session.add(source)
        await session.flush()

        async def fetch(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
            return _fetched(valid_sce_page())

        synced = await sync_official_rate_source(
            session,
            Settings(
                env="test",
                rate_artifact_dir=rate_artifact_dir,
                rate_source_retry_backoff_seconds=0,
            ),
            source,
            home_id=home_id,
            actor_user_id=None,
            correlation_id="official-workflow",
            fetcher=fetch,
        )
        assert synced.candidate_id is not None
        await session.commit()
        candidate_id = synced.candidate_id

    reviewed = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{candidate_id}/review",
        params={"home_id": home_id},
        json={
            "selected_plan_name": "TOU-D-4-9PM",
            "effective_start": "2026-08-01T00:00:00-07:00",
            "effective_end": "2027-01-01T00:00:00-08:00",
            "administrator_confirmed_effective_date": True,
            "administrator_confirmed_provenance": True,
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    published = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{candidate_id}/publish",
        params={"home_id": home_id},
    )
    assert published.status_code == 201, published.text
    version_id = published.json()["rate_plan_version"]["id"]
    async with session_factory() as session:
        periods = (
            await session.scalars(
                select(RatePeriod).where(RatePeriod.rate_plan_version_id == version_id)
            )
        ).all()
        assert len(periods) == 13
        assert {period.day_type for period in periods} == {
            "weekday",
            "weekend",
            "holiday",
            "all",
        }
        assert all(period.price_per_kwh > 0 for period in periods)
        holidays = (
            await session.scalars(
                select(RateHoliday)
                .where(RateHoliday.rate_plan_version_id == version_id)
                .order_by(RateHoliday.local_date)
            )
        ).all()
        assert [(holiday.local_date.isoformat(), holiday.name) for holiday in holidays] == [
            ("2026-09-07", "Labor Day"),
            ("2026-11-26", "Thanksgiving Day"),
            ("2026-12-25", "Christmas Day"),
        ]
        version = await session.get(RatePlanVersion, version_id)
        assert version is not None
        holiday_evidence = [
            item
            for item in version.eligibility_evidence
            if isinstance(item, dict) and item.get("evidence_type") == "holiday_calendar"
        ]
        assert len(holiday_evidence) == 1
        assert holiday_evidence[0]["coverage_end"] == "2026-12-31"


@pytest.mark.asyncio
async def test_manual_candidate_schema_rejects_guesses_and_nonofficial_provenance(
    owner_client: AsyncClient,
) -> None:
    invalid_url = _manual_payload()
    invalid_url["source_url"] = "https://example.com/rates"
    response = await owner_client.post("/api/v1/rate-sources/manual-candidates", json=invalid_url)
    assert response.status_code == 422

    query_url = _manual_payload()
    query_url["source_url"] = "https://www.sce.com/regulatory/tariff-books?account=prohibited"
    response = await owner_client.post("/api/v1/rate-sources/manual-candidates", json=query_url)
    assert response.status_code == 422

    excessive_precision = _manual_payload(price="0.123456789")
    response = await owner_client.post(
        "/api/v1/rate-sources/manual-candidates", json=excessive_precision
    )
    assert response.status_code == 422

    gap = _manual_payload()
    gap["periods"] = [
        {
            "season": "all",
            "day_type": "all",
            "period_name": "partial",
            "start_minute": 1,
            "end_minute": 1440,
            "price_per_kwh": "0.10",
        }
    ]
    response = await owner_client.post("/api/v1/rate-sources/manual-candidates", json=gap)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_candidates_and_status_exclude_unscoped_and_null_home_runs(
    owner_client: AsyncClient,
) -> None:
    home_id = (await owner_client.get("/api/v1/home-scopes")).json()["home_scopes"][0]["id"]
    async with session_factory() as session:
        hidden_home = Home(name="Hidden Home", timezone="America/Los_Angeles")
        source = RateSource(
            name="Null scoped source",
            source_type="manual_administrator_entry",
            enabled=False,
        )
        session.add_all((hidden_home, source))
        await session.flush()
        revision = RateSourceRevision(
            source_id=source.id,
            artifact_sha256="f" * 64,
            parser_version="manual-rate-entry-v1",
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
                home_id=None,
                state="review_required",
                event_code="RATE_SOURCE_CHANGED",
                correlation_id="null-home-run",
                requested_url="manual-entry:no-network-fetch",
                revision_id=revision.id,
                completed_at=datetime.now(UTC),
                evidence={"candidate_id": candidate.id, "initiator": "scheduled_worker"},
            )
        )
        await session.commit()
        candidate_id = candidate.id
        hidden_home_id = hidden_home.id

    listed = await owner_client.get("/api/v1/rate-sources/candidates", params={"home_id": home_id})
    assert listed.status_code == 200, listed.text
    assert candidate_id not in {item["id"] for item in listed.json()["candidates"]}
    runs = await owner_client.get("/api/v1/rate-sources/runs", params={"home_id": home_id})
    assert runs.status_code == 200, runs.text
    assert "null-home-run" not in {item["correlation_id"] for item in runs.json()["runs"]}
    hidden = await owner_client.get(
        "/api/v1/rate-sources/status", params={"home_id": hidden_home_id}
    )
    assert hidden.status_code == 404
    cross_candidate = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{candidate_id}/review",
        params={"home_id": home_id},
        json={
            "selected_plan_name": "hidden",
            "effective_start": "2026-08-01T00:00:00Z",
            "administrator_confirmed_effective_date": True,
            "administrator_confirmed_provenance": True,
        },
    )
    assert cross_candidate.status_code == 404


@pytest.mark.asyncio
async def test_transient_fetch_retries_are_bounded_and_preserve_lkg(
    rate_artifact_dir: Path,
) -> None:
    settings = Settings(
        env="test",
        rate_artifact_dir=rate_artifact_dir,
        rate_source_retry_attempts=3,
        rate_source_retry_backoff_seconds=0,
    )
    calls = 0

    async def eventually_succeeds(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls < 3:
            raise SourceFetchError("CONNECT_TIMEOUT", "transient timeout")
        return _fetched(valid_sce_page())

    async with session_factory() as session:
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
        )
        session.add(source)
        await session.flush()
        succeeded = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="retry-success",
            fetcher=eventually_succeeds,
        )
        assert succeeded.state == "review_required"
        succeeded_run = await session.get(RateSyncRun, succeeded.run_id)
        assert succeeded_run is not None
        assert succeeded_run.evidence["fetch_attempts"] == 3
        assert succeeded_run.evidence["transient_retry_codes"] == [
            "CONNECT_TIMEOUT",
            "CONNECT_TIMEOUT",
        ]
        lkg_etag = source.current_etag

        failed_calls = 0

        async def always_fails(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
            nonlocal failed_calls
            failed_calls += 1
            raise SourceFetchError("READ_TIMEOUT", "transient timeout")

        failed = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="retry-failure",
            fetcher=always_fails,
        )
        assert failed.state == "failed"
        assert failed.error_code == "READ_TIMEOUT"
        assert failed_calls == 3
        assert source.current_etag == lkg_etag
        assert await session.scalar(select(func.count()).select_from(RateCandidate)) == 1


@pytest.mark.asyncio
async def test_operation_deadline_returns_a_truthful_failure_before_proxy_budget(
    rate_artifact_dir: Path,
) -> None:
    settings = Settings(
        env="test",
        rate_artifact_dir=rate_artifact_dir,
        rate_source_retry_attempts=3,
        rate_source_retry_backoff_seconds=5,
        rate_source_total_timeout_seconds=120,
        rate_source_operation_timeout_seconds=0.05,
    )

    async def stalled_fetch(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        await asyncio.sleep(5)
        raise AssertionError("operation deadline did not cancel the stalled fetch")

    async with session_factory() as session:
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
        )
        session.add(source)
        await session.flush()
        started = monotonic()
        result = await sync_official_rate_source(
            session,
            settings,
            source,
            home_id=None,
            actor_user_id=None,
            correlation_id="operation-deadline",
            fetcher=stalled_fetch,
        )
        elapsed = monotonic() - started
        assert elapsed < 0.5
        assert result.state == "failed"
        assert result.error_code == "OPERATION_TIMEOUT"
        run = await session.get(RateSyncRun, result.run_id)
        assert run is not None
        assert run.state == "failed"
        assert run.completed_at is not None
        assert run.evidence["failure_phase"] == "operation_deadline"
        assert run.evidence["operation_timeout_seconds"] == 0.05

    caddy = (Path(__file__).parents[2] / "deploy" / "caddy" / "Caddyfile").read_text(
        encoding="utf-8"
    )
    matcher = caddy[caddy.index("@rateSync {") : caddy.index("@api path")]
    assert "path /api/v1/rate-sources/check-now" in matcher
    assert "response_header_timeout 40s" in matcher
    assert RATE_SOURCE_OPERATION_TIMEOUT_MAX_SECONDS < 40
    with pytest.raises(ValueError):
        Settings(
            env="test",
            rate_source_operation_timeout_seconds=(
                RATE_SOURCE_OPERATION_TIMEOUT_MAX_SECONDS + 0.01
            ),
        )


@pytest.mark.asyncio
async def test_configured_sce_url_is_strict_and_updates_the_managed_source_in_place() -> None:
    alternate_url = "https://www.sce.com/regulatory/tariff-books/rates-pricing-choices"
    settings = Settings.model_validate({"env": "test", "sce_rate_source_url": alternate_url})
    assert str(settings.sce_rate_source_url) == alternate_url
    for invalid_url in (
        "http://www.sce.com/regulatory/tariff-books",
        "https://evil.example/regulatory/tariff-books",
        "https://www.sce.com/unrelated/content",
        "https://www.sce.com/regulatory/tariff-books?account=secret",
        "https://user@www.sce.com/regulatory/tariff-books",
    ):
        with pytest.raises(ValueError):
            Settings.model_validate({"env": "test", "sce_rate_source_url": invalid_url})

    async with session_factory() as session:
        original = await ensure_default_sce_source(session, SCE_TOU_URL)
        await session.flush()
        source_id = original.id
        configured = await ensure_default_sce_source(session, alternate_url)
        assert configured.id == source_id
        assert configured.https_url == alternate_url
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RateSource)
                .where(
                    RateSource.name == SCE_SOURCE_NAME,
                    RateSource.source_type == "official_https",
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_new_diff_uses_last_published_candidate_not_newer_unapproved_candidate(
    owner_client: AsyncClient,
    rate_artifact_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_authoritative_holiday_calendar_parser(monkeypatch)
    home_id = (await owner_client.get("/api/v1/home-scopes")).json()["home_scopes"][0]["id"]
    settings = Settings(
        env="test",
        rate_artifact_dir=rate_artifact_dir,
        rate_source_retry_backoff_seconds=0,
    )

    async def synchronize(cents: int, correlation_id: str) -> str:
        async with session_factory() as session:
            source = await ensure_default_sce_source(session, SCE_TOU_URL)

            async def fetch(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
                return _fetched(
                    valid_sce_page(first_off_peak_cents=cents),
                    etag=f'"rate-{cents}"',
                )

            result = await sync_official_rate_source(
                session,
                settings,
                source,
                home_id=home_id,
                actor_user_id=None,
                correlation_id=correlation_id,
                fetcher=fetch,
            )
            await session.commit()
            assert result.candidate_id is not None
            return result.candidate_id

    approved_candidate_id = await synchronize(34, "approved-a")
    reviewed = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{approved_candidate_id}/review",
        params={"home_id": home_id},
        json={
            "selected_plan_name": "TOU-D-4-9PM",
            "effective_start": "2026-08-01T00:00:00-07:00",
            "effective_end": "2027-01-01T00:00:00-08:00",
            "administrator_confirmed_effective_date": True,
            "administrator_confirmed_provenance": True,
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    published = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{approved_candidate_id}/publish",
        params={"home_id": home_id},
    )
    assert published.status_code == 201, published.text

    unapproved_candidate_id = await synchronize(35, "unapproved-b")
    latest_candidate_id = await synchronize(36, "candidate-c")
    async with session_factory() as session:
        latest = await session.get(RateCandidate, latest_candidate_id)
        assert latest is not None
        assert unapproved_candidate_id != approved_candidate_id
        assert latest.diff["previous_candidate_id"] == approved_candidate_id
        assert latest.diff["previous_candidate_id"] != unapproved_candidate_id
        assert any(
            change["before"] == "0.34000000" and change["after"] == "0.36000000"
            for change in latest.diff["changes"]
        )


@pytest.mark.asyncio
async def test_rate_source_lease_rejects_overlap(rate_artifact_dir: Path) -> None:
    settings = Settings(
        env="test",
        rate_artifact_dir=rate_artifact_dir,
        rate_source_retry_backoff_seconds=0,
    )
    async with session_factory() as setup_session:
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
        )
        setup_session.add(source)
        await setup_session.commit()
        source_id = source.id

    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_fetch(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return _fetched(valid_sce_page())

    async with session_factory() as first_session, session_factory() as second_session:
        first_source = await first_session.get(RateSource, source_id)
        second_source = await second_session.get(RateSource, source_id)
        assert first_source is not None and second_source is not None
        first = asyncio.create_task(
            sync_official_rate_source(
                first_session,
                settings,
                first_source,
                home_id=None,
                actor_user_id=None,
                correlation_id="lease-first",
                fetcher=blocked_fetch,
            )
        )
        await entered.wait()
        with pytest.raises(RateSyncBusy):
            await sync_official_rate_source(
                second_session,
                settings,
                second_source,
                home_id=None,
                actor_user_id=None,
                correlation_id="lease-second",
                fetcher=blocked_fetch,
            )
        release.set()
        assert (await first).state == "review_required"


@pytest.mark.asyncio
async def test_postgres_advisory_lease_rejects_an_external_transaction_holder(
    rate_artifact_dir: Path,
) -> None:
    if engine.dialect.name != "postgresql":
        pytest.skip("cross-process advisory lease requires the CI PostgreSQL database")
    async with session_factory() as setup_session:
        source = RateSource(
            name=SCE_SOURCE_NAME,
            source_type="official_https",
            https_url=SCE_TOU_URL,
            enabled=True,
        )
        setup_session.add(source)
        await setup_session.commit()
        source_id = source.id
    async with session_factory() as holder, session_factory() as contender:
        await holder.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('powermeter-rate-source:' || :source_id, 0))"
            ),
            {"source_id": source_id},
        )
        contender_source = await contender.get(RateSource, source_id)
        assert contender_source is not None
        with pytest.raises(RateSyncBusy):
            await sync_official_rate_source(
                contender,
                Settings(
                    env="test",
                    rate_artifact_dir=rate_artifact_dir,
                    rate_source_retry_backoff_seconds=0,
                ),
                contender_source,
                home_id=None,
                actor_user_id=None,
                correlation_id="postgres-advisory-contender",
                fetcher=None,
            )


@pytest.mark.asyncio
async def test_assignment_replacement_closes_prior_clips_future_and_rejects_equal_start(
    owner_client: AsyncClient,
) -> None:
    home_id = (await owner_client.get("/api/v1/home-scopes")).json()["home_scopes"][0]["id"]
    starts = [
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 10, tzinfo=UTC),
        datetime(2026, 8, 20, tzinfo=UTC),
        datetime(2026, 8, 15, tzinfo=UTC),
        datetime(2026, 8, 10, tzinfo=UTC),
    ]
    async with session_factory() as session:
        account = await session.scalar(
            select(UtilityAccount).where(UtilityAccount.home_id == home_id)
        )
        actor_id = await session.scalar(select(User.id).where(User.email == "owner@example.com"))
        assert account is not None and actor_id is not None
        plan = RatePlan(
            name="Assignment replacement test",
            utility_name="Southern California Edison",
            rate_class="residential",
        )
        session.add(plan)
        await session.flush()
        versions = [
            RatePlanVersion(
                rate_plan_id=plan.id,
                version=index,
                effective_start=start,
                timezone="America/Los_Angeles",
                pricing_model="time_of_use",
                source_hash=f"{index:064x}",
                algorithm_version="cost-v1",
                state="published",
                published_by_user_id=actor_id,
                published_at=datetime.now(UTC),
            )
            for index, start in enumerate(starts, start=1)
        ]
        session.add_all(versions)
        await session.flush()

        first, first_created = await replace_rate_assignment(
            session, account=account, version=versions[0], actor_user_id=actor_id
        )
        second, second_created = await replace_rate_assignment(
            session, account=account, version=versions[1], actor_user_id=actor_id
        )
        future, future_created = await replace_rate_assignment(
            session, account=account, version=versions[2], actor_user_id=actor_id
        )
        middle, middle_created = await replace_rate_assignment(
            session, account=account, version=versions[3], actor_user_id=actor_id
        )
        repeated, repeated_created = await replace_rate_assignment(
            session, account=account, version=versions[1], actor_user_id=actor_id
        )
        assert all((first_created, second_created, future_created, middle_created))
        assert repeated.id == second.id and repeated_created is False
        assert first.effective_end == starts[1]
        assert second.effective_end == starts[3]
        assert middle.effective_end == starts[2]
        assert future.effective_end is None
        with pytest.raises(RateWorkflowConflict, match="exact instant"):
            await replace_rate_assignment(
                session,
                account=account,
                version=versions[4],
                actor_user_id=actor_id,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_manual_candidate_creation_is_database_idempotent_under_concurrency(
    owner_client: AsyncClient,
) -> None:
    if engine.dialect.name != "postgresql":
        pytest.skip("manual-candidate concurrency requires the CI PostgreSQL database")
    home_id = (await owner_client.get("/api/v1/home-scopes")).json()["home_scopes"][0]["id"]
    payload = ManualRateCandidateRequest.model_validate(_manual_payload())
    async with session_factory() as session:
        actor_id = await session.scalar(select(User.id).where(User.email == "owner@example.com"))
    assert actor_id is not None
    barrier = asyncio.Barrier(2)

    async def create(index: int) -> tuple[str, str, str, str, bool]:
        async with session_factory() as session:
            await barrier.wait()
            candidate, revision, source, run, created = await create_manual_rate_candidate(
                session,
                payload=payload,
                home_id=home_id,
                actor_user_id=actor_id,
                correlation_id=f"manual-race-{index}",
            )
            await session.commit()
            return candidate.id, revision.id, source.id, run.id, created

    results = await asyncio.gather(create(1), create(2))
    assert sum(result[4] for result in results) == 1
    assert len({result[0] for result in results}) == 1
    assert len({result[1] for result in results}) == 1
    assert len({result[2] for result in results}) == 1
    assert len({result[3] for result in results}) == 1
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RateSource)
                .where(RateSource.source_type == "manual_administrator_entry")
            )
            == 1
        )
        assert await session.scalar(select(func.count()).select_from(RateSourceRevision)) == 1
        assert await session.scalar(select(func.count()).select_from(RateCandidate)) == 1
        assert await session.scalar(select(func.count()).select_from(RateSyncRun)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_code == "RATE_MANUAL_CANDIDATE_CREATED")
            )
            == 1
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_plan_allocator_serializes_first_publication_and_versions(
    owner_client: AsyncClient,
) -> None:
    if engine.dialect.name != "postgresql":
        pytest.skip("rate-plan publication concurrency requires the CI PostgreSQL database")
    async with session_factory() as session:
        actor_id = await session.scalar(select(User.id).where(User.email == "owner@example.com"))
    assert actor_id is not None
    barrier = asyncio.Barrier(2)

    async def publish(index: int) -> tuple[str, int]:
        async with session_factory() as session:
            await barrier.wait()
            plan, version_number = await locked_rate_plan_and_next_version(
                session,
                name="Concurrent shared publication",
                utility_name="Southern California Edison",
                rate_class="residential",
            )
            version = RatePlanVersion(
                rate_plan_id=plan.id,
                version=version_number,
                effective_start=datetime(2026, 8, index, tzinfo=UTC),
                timezone="America/Los_Angeles",
                pricing_model="time_of_use",
                source_hash=f"{index:064x}",
                algorithm_version="cost-v1",
                state="published",
                published_by_user_id=actor_id,
                published_at=datetime.now(UTC),
            )
            session.add(version)
            await session.flush()
            # Hold the natural-key row lock long enough to force the competing
            # bill/SCE-shared allocator through the serialized path.
            await asyncio.sleep(0.05)
            await session.commit()
            return plan.id, version_number

    results = await asyncio.gather(publish(1), publish(2))
    assert len({result[0] for result in results}) == 1
    assert {result[1] for result in results} == {1, 2}
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(RatePlan)) == 1
        assert await session.scalar(select(func.count()).select_from(RatePlanVersion)) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_assignment_replacement_and_trigger_prevent_concurrent_overlap(
    owner_client: AsyncClient,
) -> None:
    if engine.dialect.name != "postgresql":
        pytest.skip("rate-assignment concurrency requires the CI PostgreSQL database")
    home_id = (await owner_client.get("/api/v1/home-scopes")).json()["home_scopes"][0]["id"]
    starts = (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 10, tzinfo=UTC))
    async with session_factory() as session:
        account = await session.scalar(
            select(UtilityAccount).where(UtilityAccount.home_id == home_id)
        )
        actor_id = await session.scalar(select(User.id).where(User.email == "owner@example.com"))
        assert account is not None and actor_id is not None
        plan = RatePlan(
            name="Concurrent assignment replacement",
            utility_name="Southern California Edison",
            rate_class="residential",
        )
        session.add(plan)
        await session.flush()
        versions = [
            RatePlanVersion(
                rate_plan_id=plan.id,
                version=index,
                effective_start=start,
                timezone="America/Los_Angeles",
                pricing_model="time_of_use",
                source_hash=f"{index + 10:064x}",
                algorithm_version="cost-v1",
                state="published",
                published_by_user_id=actor_id,
                published_at=datetime.now(UTC),
            )
            for index, start in enumerate(starts, start=1)
        ]
        session.add_all(versions)
        await session.commit()
        account_id = account.id
        version_ids = [version.id for version in versions]

    barrier = asyncio.Barrier(2)

    async def assign(version_id: str) -> None:
        async with session_factory() as session:
            account = await session.get(UtilityAccount, account_id)
            version = await session.get(RatePlanVersion, version_id)
            assert account is not None and version is not None
            await barrier.wait()
            await replace_rate_assignment(
                session,
                account=account,
                version=version,
                actor_user_id=actor_id,
            )
            await session.commit()

    await asyncio.gather(*(assign(version_id) for version_id in version_ids))
    async with session_factory() as session:
        assignments = (
            await session.scalars(select(RateAssignment).order_by(RateAssignment.effective_start))
        ).all()
        assert len(assignments) == 2
        assert assignments[0].effective_end == assignments[1].effective_start
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO rate_assignments "
                    "(id, utility_account_id, rate_plan_version_id, effective_start, "
                    "effective_end, assigned_by_user_id) VALUES "
                    "(:id, :account_id, :version_id, :start, :end, :actor_id)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "account_id": account_id,
                    "version_id": version_ids[0],
                    "start": datetime(2026, 8, 5, tzinfo=UTC),
                    "end": datetime(2026, 8, 12, tzinfo=UTC),
                    "actor_id": actor_id,
                },
            )
            await session.commit()
        await session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_direct_sql_cannot_rewrite_or_delete_candidate_review_provenance(
    owner_client: AsyncClient,
) -> None:
    if engine.dialect.name != "postgresql":
        pytest.skip("direct lifecycle guards require the CI PostgreSQL database")
    home_id = (await owner_client.get("/api/v1/home-scopes")).json()["home_scopes"][0]["id"]
    created = await owner_client.post(
        "/api/v1/rate-sources/manual-candidates",
        params={"home_id": home_id},
        json=_manual_payload(),
    )
    assert created.status_code == 201, created.text
    candidate_id = created.json()["candidate_id"]
    reviewed = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{candidate_id}/review",
        params={"home_id": home_id},
        json={
            "selected_plan_name": "MANUAL-TOU-D",
            "effective_start": "2026-08-01T00:00:00-07:00",
            "administrator_confirmed_effective_date": True,
            "administrator_confirmed_provenance": True,
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    published = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{candidate_id}/publish",
        params={"home_id": home_id},
    )
    assert published.status_code == 201, published.text
    review_id = published.json()["workflow"]["id"]

    statements = (
        (
            "UPDATE rate_candidate_reviews SET state = 'reviewed', "
            "rate_plan_version_id = NULL, published_at = NULL WHERE id = :id",
            review_id,
        ),
        (
            "UPDATE rate_candidate_reviews SET published_at = CURRENT_TIMESTAMP WHERE id = :id",
            review_id,
        ),
        ("DELETE FROM rate_candidate_reviews WHERE id = :id", review_id),
        ("DELETE FROM rate_candidates WHERE id = :id", candidate_id),
    )
    for statement, target_id in statements:
        async with session_factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(text(statement), {"id": target_id})
                await session.commit()
            await session.rollback()

    async with session_factory() as session:
        review = await session.get(RateCandidateReview, review_id)
        candidate = await session.get(RateCandidate, candidate_id)
        assert review is not None and review.state == "published"
        assert review.rate_plan_version_id == published.json()["rate_plan_version"]["id"]
        assert review.published_at is not None
        assert candidate is not None


def test_integrity_migration_write_locks_precede_preflight() -> None:
    migration = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260815_0011_rate_workflow_integrity.py"
    ).read_text(encoding="utf-8")
    upgrade = migration[migration.index("def upgrade()") :]
    lock = (
        '"LOCK TABLE rate_plans, rate_assignments, rate_candidate_reviews, "\n'
        '            "rate_candidates IN ACCESS EXCLUSIVE MODE"'
    )
    assert lock in upgrade
    assert upgrade.index(lock) < upgrade.index("    _preflight()")


@pytest.mark.asyncio
async def test_scheduled_sync_projects_one_network_result_to_each_home(
    rate_artifact_dir: Path,
) -> None:
    settings = Settings(
        env="test",
        rate_artifact_dir=rate_artifact_dir,
        rate_source_retry_backoff_seconds=0,
    )
    calls = 0
    catalog_detail_url = (
        "https://www.sce.com/save-money/rates-financing/residential-rate-plans/tou-d-4-9"
    )
    catalog_root = (
        "<html><body><main><h2>TOU-D 4 PM to 9 PM</h2>"
        f'<a href="{catalog_detail_url}">TOU-D 4 PM to 9 PM</a>'
        "</main></body></html>"
    ).encode()

    async def fetch_once(url: str, **_kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if url == SCE_CATALOG_URL:
            return _fetched(catalog_root, url=url)
        return _fetched(valid_sce_page(), url=url)

    async with session_factory() as session:
        session.add_all(
            (
                Home(name="Scheduled A", timezone="America/Los_Angeles"),
                Home(name="Scheduled B", timezone="America/Los_Angeles"),
            )
        )
        await session.flush()
        result = await sync_due_rate_sources(session, settings, fetcher=fetch_once)
        assert result == {"checked": 1, "failed": 0, "review_required": 0, "unchanged": 1}
        assert calls == 2
        runs = (await session.scalars(select(RateSyncRun))).all()
        assert len(runs) == 2
        assert {run.home_id for run in runs} == set((await session.scalars(select(Home.id))).all())
        assert all(run.evidence["initiator"] == "scheduled_worker" for run in runs)
        assert all(run.home_id is not None for run in runs)


@pytest.mark.asyncio
async def test_viewer_cannot_create_or_advance_rate_candidates(
    owner_client: AsyncClient,
) -> None:
    created_user = await owner_client.post(
        "/api/v1/users",
        json={
            "email": "rate-viewer@example.com",
            "display_name": "Rate Viewer",
            "password": "correct horse battery staple 2026!",
            "role_names": ["Viewer"],
        },
    )
    assert created_user.status_code == 201, created_user.text
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://powermeter.test"
    ) as viewer:
        logged_in = await viewer.post(
            "/api/v1/auth/login",
            json={
                "email": "rate-viewer@example.com",
                "password": "correct horse battery staple 2026!",
            },
        )
        assert logged_in.status_code == 200, logged_in.text
        viewer.headers["X-CSRF-Token"] = viewer.cookies["pm_csrf"]
        forbidden = await viewer.post(
            "/api/v1/rate-sources/manual-candidates", json=_manual_payload()
        )
        assert forbidden.status_code == 403

        owner_home_id = (await owner_client.get("/api/v1/home-scopes")).json()["home_scopes"][0][
            "id"
        ]
        candidate = await owner_client.post(
            "/api/v1/rate-sources/manual-candidates",
            params={"home_id": owner_home_id},
            json=_manual_payload(price="0.23456789"),
        )
        assert candidate.status_code == 201, candidate.text
        forbidden_reject = await viewer.post(
            f"/api/v1/rate-sources/candidates/{candidate.json()['candidate_id']}/reject",
            params={"home_id": owner_home_id},
        )
        assert forbidden_reject.status_code == 403


@pytest.mark.asyncio
async def test_disposable_rate_candidates_can_be_deleted_but_reviewed_provenance_cannot(
    owner_client: AsyncClient,
) -> None:
    payload = _manual_payload()
    created = await owner_client.post("/api/v1/rate-sources/manual-candidates", json=payload)
    assert created.status_code == 201, created.text
    home_id = created.json()["home_id"]
    candidate_id = created.json()["candidate_id"]

    removed = await owner_client.delete(
        f"/api/v1/rate-sources/candidates/{candidate_id}", params={"home_id": home_id}
    )
    assert removed.status_code == 204, removed.text
    async with session_factory() as session:
        assert await session.get(RateCandidate, candidate_id) is None
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_code == "RATE_CANDIDATE_DELETED",
                AuditEvent.target_id == candidate_id,
            )
        )
        assert audit is not None
        assert audit.details["published_rate_provenance_deleted"] is False

    reviewed_payload = {**_manual_payload(), "source_title": "Disposable review fixture"}
    reviewed = await owner_client.post(
        "/api/v1/rate-sources/manual-candidates", json=reviewed_payload
    )
    assert reviewed.status_code == 201, reviewed.text
    reviewed_id = reviewed.json()["candidate_id"]
    review = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{reviewed_id}/review",
        params={"home_id": home_id},
        json={
            "selected_plan_name": reviewed_payload["rate_plan_name"],
            "effective_start": "2026-08-17T07:00:00Z",
            "administrator_confirmed_effective_date": True,
            "administrator_confirmed_provenance": True,
        },
    )
    assert review.status_code == 200, review.text
    protected = await owner_client.delete(
        f"/api/v1/rate-sources/candidates/{reviewed_id}", params={"home_id": home_id}
    )
    assert protected.status_code == 409, protected.text

    rejected = await owner_client.post(
        f"/api/v1/rate-sources/candidates/{reviewed_id}/reject",
        params={"home_id": home_id},
    )
    assert rejected.status_code == 200, rejected.text
    deleted_rejected = await owner_client.delete(
        f"/api/v1/rate-sources/candidates/{reviewed_id}", params={"home_id": home_id}
    )
    assert deleted_rejected.status_code == 204, deleted_rejected.text
    async with session_factory() as session:
        assert await session.get(RateCandidate, reviewed_id) is None
        assert (
            await session.scalar(
                select(RateCandidateReview).where(RateCandidateReview.candidate_id == reviewed_id)
            )
            is None
        )
