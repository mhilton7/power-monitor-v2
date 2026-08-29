from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..bill_rate_import.isolated import (
    extract_rate_plan_isolated,
    extract_rate_plan_portable_for_tests,
)
from ..config import Settings, get_settings
from ..constants import MAX_PDF_BYTES
from ..db import get_session
from ..errors import BillRateImportError, InvalidRequest, NotFound, RateWorkflowConflict
from ..models import (
    Alert,
    AuditEvent,
    BillingCycleAdjustment,
    BillingEstimate,
    BillingEstimateSelection,
    Circuit,
    Device,
    DeviceHeartbeat,
    DeviceTelemetryState,
    IntervalCostSelection,
    NormalizedInterval,
    RateAssignment,
    RateCandidate,
    RateCandidateReview,
    RatePeriod,
    RatePlan,
    RatePlanVersion,
    RateSource,
    RateSourceRevision,
    RateSyncRun,
    RawReading,
    SceCatalogEntry,
    TelemetryEnergyEvent,
    UtilityAccount,
    UtilityBillRateCorrection,
    UtilityBillRateExtraction,
    UtilityBillRateUpload,
    aware_utc,
    user_home_scopes,
)
from ..schemas.api import (
    BillingCycleAdjustmentRequest,
    ManualRateCandidateRequest,
    RateCandidateActivationRequest,
    RateCandidateReviewRequest,
    RateCorrectionRequest,
    RatePublishRequest,
    RateSourceCheckRequest,
)
from ..schemas.billing import RatePlanDraft, TierThresholdRuleDraft
from ..security.auth import CurrentUser, require_permission
from ..services.cost_engine import season_from_storage
from ..services.rate_sync import (
    SCE_CATALOG_SOURCE_NAME,
    SCE_CATALOG_URL,
    ensure_default_sce_catalog_source,
    ensure_default_sce_source,
    sync_official_rate_source,
)
from ..services.rate_workflow import (
    activate_rate_candidate,
    create_manual_rate_candidate,
    exact_home_candidate,
    locked_rate_plan_and_next_version,
    publish_rate_candidate,
    reject_rate_candidate,
    replace_rate_assignment,
    replace_utility_account_tier_threshold,
    resolve_assigned_utility_account_cycle_tier_threshold,
    resolve_utility_account_tier_threshold,
    review_rate_candidate,
    safe_review,
)
from ..services.sce_catalog import CATALOG_CRAWLER_VERSION, SUPPORTED_PLAN_NAMES
from ..services.tiered_billing import (
    EnergyQuality,
    billing_calculation_state,
    billing_projection,
    estimate_confidence,
    tier_state_for_quality,
    tiered_cost,
)

router = APIRouter(prefix="/api/v1", tags=["billing"])
# Compatibility seam for existing API tests that replace the parser with a sanitized
# fixture. It is reached only under PM_ENV=test; production always calls the sandbox.
extract_rate_plan_from_pdf = extract_rate_plan_portable_for_tests


async def _estimate_short_gap_energy(
    session: AsyncSession,
    *,
    events: list[TelemetryEnergyEvent],
    cycle_start: datetime,
    scope_end: datetime,
    reading_coverage: Decimal,
    minimum_coverage: Decimal,
    maximum_gap_seconds: int,
    unresolved_counter_resets: int,
) -> dict[str, object]:
    """Estimate billing-only energy without creating or changing History rows."""

    estimated_mwh = Decimal("0")
    lower_mwh = Decimal("0")
    upper_mwh = Decimal("0")
    estimated_seconds = 0
    unknown_seconds = 0
    unknown_count = 0
    methods: set[str] = set()
    details: list[dict[str, object]] = []
    for event in events:
        gap_start = aware_utc(event.gap_start_utc) if event.gap_start_utc else None
        gap_end = aware_utc(event.gap_end_utc) if event.gap_end_utc else None
        duration_seconds = (
            max(0, int((gap_end - gap_start).total_seconds()))
            if gap_start is not None and gap_end is not None and gap_end > gap_start
            else 0
        )
        blocked_reason: str | None = None
        if gap_start is None or gap_end is None or duration_seconds <= 0:
            blocked_reason = "gap_bounds_unavailable"
        elif (
            gap_start < cycle_start
            or gap_end > scope_end
            or bool(event.evidence.get("crosses_billing_cycle") is True)
        ):
            blocked_reason = "gap_crosses_billing_cycle_boundary"
        elif unresolved_counter_resets:
            blocked_reason = "unresolved_counter_reset"
        elif reading_coverage < minimum_coverage:
            blocked_reason = "reading_coverage_below_minimum_threshold"
        elif duration_seconds > maximum_gap_seconds:
            blocked_reason = "gap_exceeds_maximum_estimatable_duration"

        before: NormalizedInterval | None = None
        after: NormalizedInterval | None = None
        if blocked_reason is None:
            assert gap_start is not None and gap_end is not None
            before = (
                await session.scalars(
                    select(NormalizedInterval)
                    .where(
                        NormalizedInterval.device_id == event.device_id,
                        NormalizedInterval.source_authenticated.is_(True),
                        NormalizedInterval.energy_mwh.is_not(None),
                        NormalizedInterval.end_utc <= gap_start,
                        NormalizedInterval.end_utc
                        >= gap_start - timedelta(seconds=maximum_gap_seconds),
                    )
                    .order_by(NormalizedInterval.end_utc.desc())
                    .limit(1)
                )
            ).first()
            after = (
                await session.scalars(
                    select(NormalizedInterval)
                    .where(
                        NormalizedInterval.device_id == event.device_id,
                        NormalizedInterval.source_authenticated.is_(True),
                        NormalizedInterval.energy_mwh.is_not(None),
                        NormalizedInterval.start_utc >= gap_end,
                        NormalizedInterval.start_utc
                        <= gap_end + timedelta(seconds=maximum_gap_seconds),
                    )
                    .order_by(NormalizedInterval.start_utc)
                    .limit(1)
                )
            ).first()
            if before is None or after is None:
                blocked_reason = "neighboring_intervals_unavailable"

        if blocked_reason is not None:
            unknown_count += 1
            unknown_seconds += duration_seconds
            details.append(
                {
                    "event_id": event.id,
                    "status": "unknown",
                    "duration_seconds": duration_seconds,
                    "reason": blocked_reason,
                }
            )
            continue

        assert before is not None and after is not None
        before_seconds = Decimal(str((before.end_utc - before.start_utc).total_seconds()))
        after_seconds = Decimal(str((after.end_utc - after.start_utc).total_seconds()))
        if before_seconds <= 0 or after_seconds <= 0:
            unknown_count += 1
            unknown_seconds += duration_seconds
            details.append(
                {
                    "event_id": event.id,
                    "status": "unknown",
                    "duration_seconds": duration_seconds,
                    "reason": "neighboring_interval_duration_invalid",
                }
            )
            continue
        before_rate = Decimal(before.energy_mwh or 0) / before_seconds
        after_rate = Decimal(after.energy_mwh or 0) / after_seconds
        gap_seconds = Decimal(duration_seconds)
        estimate = (((before_rate + after_rate) / Decimal(2)) * gap_seconds).quantize(
            Decimal("0.000001")
        )
        lower = (min(before_rate, after_rate) * gap_seconds).quantize(Decimal("0.000001"))
        upper = (max(before_rate, after_rate) * gap_seconds).quantize(Decimal("0.000001"))
        estimated_mwh += estimate
        lower_mwh += lower
        upper_mwh += upper
        estimated_seconds += duration_seconds
        methods.add("short_gap_neighbor_interpolation")
        details.append(
            {
                "event_id": event.id,
                "status": "estimated",
                "duration_seconds": duration_seconds,
                "method": "short_gap_neighbor_interpolation",
                "energy_kwh": estimate / Decimal(1_000_000),
                "lower_kwh": lower / Decimal(1_000_000),
                "upper_kwh": upper / Decimal(1_000_000),
            }
        )
    return {
        "estimated_mwh": estimated_mwh,
        "lower_mwh": lower_mwh,
        "upper_mwh": upper_mwh,
        "estimated_seconds": estimated_seconds,
        "unknown_seconds": unknown_seconds,
        "unknown_count": unknown_count,
        "methods": tuple(sorted(methods)),
        "details": details,
        "raw_history_modified": False,
    }


async def _user_homes(session: AsyncSession, user_id: str) -> tuple[str, ...]:
    return tuple(
        (
            await session.scalars(
                select(user_home_scopes.c.home_id)
                .where(user_home_scopes.c.user_id == user_id)
                .order_by(user_home_scopes.c.home_id)
            )
        ).all()
    )


async def _resolve_user_home(
    session: AsyncSession, user_id: str, requested_home_id: str | None
) -> str:
    homes = await _user_homes(session, user_id)
    if requested_home_id is not None:
        if requested_home_id not in homes:
            raise NotFound("home does not exist")
        return requested_home_id
    if not homes:
        raise NotFound("home does not exist")
    if len(homes) > 1:
        raise InvalidRequest("home_id is required when the actor can access multiple homes")
    return homes[0]


async def _scoped_extraction(
    session: AsyncSession,
    *,
    user_id: str,
    extraction_id: str,
    for_update: bool = False,
) -> tuple[UtilityBillRateExtraction, UtilityBillRateUpload]:
    actor_homes = select(user_home_scopes.c.home_id).where(user_home_scopes.c.user_id == user_id)
    statement = (
        select(UtilityBillRateExtraction, UtilityBillRateUpload)
        .join(
            UtilityBillRateUpload,
            UtilityBillRateUpload.id == UtilityBillRateExtraction.upload_id,
        )
        .where(
            UtilityBillRateExtraction.id == extraction_id,
            UtilityBillRateUpload.home_id.in_(actor_homes),
        )
    )
    if for_update:
        statement = statement.with_for_update()
    result = (await session.execute(statement)).first()
    if result is None:
        raise NotFound("rate extraction does not exist")
    return result[0], result[1]


def _safe_extraction(
    row: UtilityBillRateExtraction, upload: UtilityBillRateUpload
) -> dict[str, object]:
    return {
        "id": row.id,
        "home_id": upload.home_id,
        "artifact_sha256": upload.artifact_sha256,
        "utility_name": row.utility_name,
        "rate_plan_name": row.rate_plan_name,
        "rate_class": row.rate_class,
        "plan_classification": row.plan_classification,
        "holiday_treatment": row.holiday_treatment,
        "cca_or_direct_access_indicator": row.cca_or_direct_access_indicator,
        "season_definitions": row.season_definitions,
        "day_type_definitions": row.day_type_definitions,
        "tou_period_definitions": row.tou_period_definitions,
        "tier_threshold_definitions": row.tier_threshold_definitions,
        "tier_threshold_rule": row.tier_threshold_rule,
        "reusable_price_components": row.reusable_price_components,
        "billing_period_start": row.billing_period_start,
        "billing_period_end": row.billing_period_end,
        "billing_period_days": row.billing_period_days,
        "tier_threshold_basis": row.tier_threshold_basis,
        "candidate_complete": row.candidate_complete,
        "publication_scope": ("complete_schedule" if row.candidate_complete else "review_only"),
        "publishable_effective_start": None,
        "publishable_effective_end": None,
        "baseline_allocation_rule": row.baseline_allocation_rule,
        "baseline_credit_rate": row.baseline_credit_rate,
        "effective_start_candidate": row.effective_start_candidate,
        "effective_end_candidate": row.effective_end_candidate,
        "source_evidence": row.source_evidence,
        "parser_version": row.parser_version,
        "state": row.state,
        "resulting_rate_version_id": row.resulting_rate_version_id,
        "review_required": row.state == "review_required",
    }


def _catalog_manifest_proves_discovery_closure(
    manifest: dict[str, object] | None,
) -> bool:
    """Fail closed unless a captured catalog manifest proves link closure."""

    if manifest is None or manifest.get("schema_version") != "sce-catalog-crawl/1.0.0":
        return False
    if manifest.get("source_policy") != "official_public_sce_only":
        return False
    closure = manifest.get("closure")
    counts = manifest.get("counts")
    documents = manifest.get("documents")
    links = manifest.get("links")
    plans = manifest.get("plans")
    if not isinstance(closure, dict) or not isinstance(counts, dict):
        return False
    if not isinstance(documents, list) or not documents:
        return False
    if not isinstance(links, list) or not isinstance(plans, list) or not plans:
        return False

    link_resolutions_closed = all(
        isinstance(link, dict)
        and link.get("resolution") in {"parsed", "explicitly_excluded"}
        and link.get("discovery_status") == "accounted_for"
        for link in links
    )
    plan_states_closed = all(
        isinstance(plan, dict) and plan.get("discovery_state") in {"parsed", "excluded"}
        for plan in plans
    )
    return (
        closure.get("proved") is True
        and closure.get("all_discovered_links_accounted_for") is True
        and closure.get("plans_silently_omitted") == 0
        and closure.get("failure_reasons") == []
        and closure.get("unresolved_links") == []
        and closure.get("plans_requiring_parser_updates") == []
        and counts.get("links_discovered") == len(links)
        and counts.get("links_resolved") == len(links)
        and counts.get("plans_discovered") == len(plans)
        and counts.get("plans_requiring_parser_updates") == 0
        and counts.get("documents_captured") == len(documents)
        and link_resolutions_closed
        and plan_states_closed
    )


def _semantic_rate_values_from_draft(draft: RatePlanDraft) -> dict[str, object]:
    periods = [period.model_dump(mode="json") for period in draft.periods]
    return {
        "utility_name": draft.utility_name,
        "rate_plan_name": draft.rate_plan_name,
        "rate_class": draft.rate_class,
        "plan_classification": draft.plan_classification,
        "holiday_treatment": draft.holiday_treatment,
        "cca_or_direct_access_indicator": draft.cca_or_direct_access_indicator,
        "season_definitions": [
            {
                "name": "summer",
                "months": list(draft.summer_months),
                "source_verified": "summer" in draft.verified_seasons,
            },
            {
                "name": "winter",
                "months": list(draft.winter_months),
                "source_verified": "winter" in draft.verified_seasons,
            },
        ],
        "day_type_definitions": sorted({period.day_type for period in draft.periods}),
        "tou_period_definitions": periods,
        "tier_threshold_definitions": [
            {
                "start_kwh": str(period.tier_start_kwh),
                "end_kwh": str(period.tier_end_kwh) if period.tier_end_kwh else None,
            }
            for period in draft.periods
            if period.tier_start_kwh or period.tier_end_kwh
        ],
        "tier_threshold_rule": (
            draft.tier_threshold_rule.model_dump(mode="json")
            if draft.tier_threshold_rule is not None
            else None
        ),
        "reusable_price_components": [
            charge.model_dump(mode="json") for charge in draft.reusable_charges
        ],
        "tier_threshold_basis": draft.tier_threshold_basis,
        "candidate_complete": draft.candidate_complete,
        "baseline_allocation_rule": draft.baseline_allocation_rule,
        "baseline_credit_rate": (
            str(draft.baseline_credit_rate) if draft.baseline_credit_rate is not None else None
        ),
    }


def _semantic_rate_values_from_row(row: UtilityBillRateExtraction) -> dict[str, object]:
    return {
        "utility_name": row.utility_name,
        "rate_plan_name": row.rate_plan_name,
        "rate_class": row.rate_class,
        "plan_classification": row.plan_classification,
        "holiday_treatment": row.holiday_treatment,
        "cca_or_direct_access_indicator": row.cca_or_direct_access_indicator,
        "season_definitions": row.season_definitions,
        "day_type_definitions": row.day_type_definitions,
        "tou_period_definitions": row.tou_period_definitions,
        "tier_threshold_definitions": row.tier_threshold_definitions,
        "tier_threshold_rule": row.tier_threshold_rule,
        "reusable_price_components": row.reusable_price_components,
        "tier_threshold_basis": row.tier_threshold_basis,
        "candidate_complete": row.candidate_complete,
        "baseline_allocation_rule": row.baseline_allocation_rule,
        "baseline_credit_rate": (
            str(row.baseline_credit_rate) if row.baseline_credit_rate is not None else None
        ),
    }


def _semantic_rate_identity(values: dict[str, object]) -> str:
    canonical = json.dumps(values, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _existing_semantic_rate_candidate(
    session: AsyncSession,
    *,
    home_id: str,
    draft: RatePlanDraft,
) -> tuple[UtilityBillRateExtraction, UtilityBillRateUpload] | None:
    expected = _semantic_rate_identity(_semantic_rate_values_from_draft(draft))
    rows = (
        await session.execute(
            select(UtilityBillRateExtraction, UtilityBillRateUpload)
            .join(
                UtilityBillRateUpload,
                UtilityBillRateUpload.id == UtilityBillRateExtraction.upload_id,
            )
            .where(
                UtilityBillRateUpload.home_id == home_id,
                UtilityBillRateExtraction.state != "rejected",
            )
            .order_by(UtilityBillRateUpload.created_at.desc())
        )
    ).all()
    for extraction, upload in rows:
        if _semantic_rate_identity(_semantic_rate_values_from_row(extraction)) == expected:
            return extraction, upload
    return None


def _current_billing_cycle_bounds(
    account: UtilityAccount, now: datetime
) -> tuple[datetime, datetime]:
    zone = ZoneInfo(account.timezone)
    local_now = now.astimezone(zone)
    year = local_now.year
    month = local_now.month
    if local_now.day < account.billing_day:
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    local_start = datetime(year, month, account.billing_day, tzinfo=zone)
    end_year = year + (1 if month == 12 else 0)
    end_month = 1 if month == 12 else month + 1
    local_end = datetime(end_year, end_month, account.billing_day, tzinfo=zone)
    return local_start.astimezone(UTC), local_end.astimezone(UTC)


@router.post("/bill-rate-imports", status_code=201)
async def import_rates_from_bill(
    request: Request,
    document: UploadFile = File(...),
    home_id: str | None = Form(default=None, min_length=36, max_length=36),
    user: CurrentUser = Depends(require_permission("rates.bill_import")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    if document.content_type not in ("application/pdf", "application/x-pdf"):
        raise BillRateImportError("only PDF documents are accepted")
    data = await document.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise BillRateImportError("PDF exceeds the configured size limit")
    if settings.env == "test":
        # Test-only portability path. Development and production must establish the
        # real Linux kernel sandbox and fail closed when it is unavailable.
        draft, ignored_categories = extract_rate_plan_from_pdf(data)
    else:
        draft, ignored_categories = await extract_rate_plan_isolated(
            data, timeout_seconds=settings.bill_import_timeout_seconds
        )
    duplicate = await session.scalar(
        select(UtilityBillRateUpload.id).where(
            UtilityBillRateUpload.home_id == scoped_home_id,
            UtilityBillRateUpload.artifact_sha256 == draft.source_artifact_sha256,
        )
    )
    if duplicate is not None:
        raise BillRateImportError("this rate-source document was already imported")
    semantic_match = await _existing_semantic_rate_candidate(
        session,
        home_id=scoped_home_id,
        draft=draft,
    )
    if semantic_match is not None:
        data = b""
        extraction, existing_upload = semantic_match
        return {
            "extraction": _safe_extraction(extraction, existing_upload),
            "usage_source_notice": (
                "All usage and History come exclusively from authenticated PZEM sensors."
            ),
            "ignored_prohibited_categories": list(ignored_categories),
            "semantic_candidate_unchanged": True,
        }
    upload = UtilityBillRateUpload(
        home_id=scoped_home_id,
        artifact_sha256=draft.source_artifact_sha256,
        byte_count=len(data),
        page_count=max(field.source.page for field in draft.fields),
        media_type="application/pdf",
        state="parsed_rate_only",
        uploaded_by_user_id=user.id,
    )
    session.add(upload)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        # The uniqueness boundary is (home, artifact hash). A document already
        # owned by another home is deliberately indistinguishable from a new
        # document and is imported independently for this home.
        raise BillRateImportError("this rate-source document was already imported") from exc
    extraction = UtilityBillRateExtraction(
        upload_id=upload.id,
        utility_name=draft.utility_name,
        rate_plan_name=draft.rate_plan_name,
        rate_class=draft.rate_class,
        plan_classification=draft.plan_classification,
        holiday_treatment=draft.holiday_treatment,
        cca_or_direct_access_indicator=draft.cca_or_direct_access_indicator,
        season_definitions=[
            {
                "name": "summer",
                "months": list(draft.summer_months),
                "source_verified": "summer" in draft.verified_seasons,
            },
            {
                "name": "winter",
                "months": list(draft.winter_months),
                "source_verified": "winter" in draft.verified_seasons,
            },
        ],
        day_type_definitions=sorted({period.day_type for period in draft.periods}),
        tou_period_definitions=[period.model_dump(mode="json") for period in draft.periods],
        tier_threshold_definitions=[
            {
                "start_kwh": str(period.tier_start_kwh),
                "end_kwh": str(period.tier_end_kwh) if period.tier_end_kwh else None,
            }
            for period in draft.periods
            if period.tier_start_kwh or period.tier_end_kwh
        ],
        tier_threshold_rule=(
            draft.tier_threshold_rule.model_dump(mode="json")
            if draft.tier_threshold_rule is not None
            else None
        ),
        reusable_price_components=[
            charge.model_dump(mode="json") for charge in draft.reusable_charges
        ],
        billing_period_start=draft.billing_period_start,
        billing_period_end=draft.billing_period_end,
        billing_period_days=draft.billing_period_days,
        tier_threshold_basis=draft.tier_threshold_basis,
        candidate_complete=draft.candidate_complete,
        baseline_allocation_rule=draft.baseline_allocation_rule,
        baseline_credit_rate=draft.baseline_credit_rate,
        effective_start_candidate=draft.effective_start_candidate,
        effective_end_candidate=draft.effective_end_candidate,
        source_evidence=[field.model_dump(mode="json") for field in draft.fields],
        parser_version=draft.parser_version,
        state="review_required",
    )
    session.add(extraction)
    try:
        session.add(
            AuditEvent(
                actor_user_id=user.id,
                event_code="BILL_RATE_SOURCE_PARSED",
                target_type="utility_bill_rate_extraction",
                target_id=extraction.id,
                correlation_id=request.state.correlation_id,
                details={
                    "artifact_sha256": upload.artifact_sha256,
                    "ignored_categories": list(ignored_categories),
                },
            )
        )
        await session.commit()
    finally:
        data = b""  # release the source document; no OCR text enters application state
    return {
        "extraction": _safe_extraction(extraction, upload),
        "usage_source_notice": (
            "All usage and History come exclusively from authenticated PZEM sensors."
        ),
        "ignored_prohibited_categories": list(ignored_categories),
    }


@router.get("/bill-rate-imports")
async def list_bill_rate_imports(
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("rates.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    rows = (
        await session.execute(
            select(UtilityBillRateExtraction, UtilityBillRateUpload)
            .join(
                UtilityBillRateUpload,
                UtilityBillRateUpload.id == UtilityBillRateExtraction.upload_id,
            )
            .where(UtilityBillRateUpload.home_id == scoped_home_id)
            .order_by(UtilityBillRateUpload.created_at.desc())
        )
    ).all()
    return {"extractions": [_safe_extraction(extraction, upload) for extraction, upload in rows]}


@router.get("/bill-rate-imports/{extraction_id}")
async def get_bill_rate_import(
    extraction_id: str,
    user: CurrentUser = Depends(require_permission("rates.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    extraction, upload = await _scoped_extraction(
        session, user_id=user.id, extraction_id=extraction_id
    )
    return {"extraction": _safe_extraction(extraction, upload)}


@router.patch("/bill-rate-imports/{extraction_id}")
async def correct_bill_rate_import(
    extraction_id: str,
    payload: RateCorrectionRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("rates.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    extraction, upload = await _scoped_extraction(
        session,
        user_id=user.id,
        extraction_id=extraction_id,
        for_update=True,
    )
    if extraction.state != "review_required":
        raise BillRateImportError("only a review-required draft can be corrected")
    prior = getattr(extraction, payload.field)
    corrected: object = payload.corrected_value
    if payload.field == "baseline_credit_rate":
        try:
            corrected = Decimal(payload.corrected_value)
        except InvalidOperation as exc:
            raise BillRateImportError("baseline credit must be a decimal unit rate") from exc
        if corrected < 0:
            raise BillRateImportError("baseline credit must be nonnegative")
    if payload.field == "cca_or_direct_access_indicator" and corrected not in (
        "sce_generation",
        "cca",
        "direct_access",
        "unknown",
    ):
        raise BillRateImportError("invalid generation-service indicator")
    if payload.field == "billing_period_days":
        if not isinstance(extraction.tier_threshold_rule, dict):
            raise BillRateImportError("the bill has no reviewed baseline allowance to prorate")
        try:
            days = int(payload.corrected_value)
            allowance = Decimal(str(extraction.tier_threshold_rule["source_allowance_kwh"]))
            per_day = allowance / Decimal(days)
            rule = TierThresholdRuleDraft.model_validate(
                {
                    **extraction.tier_threshold_rule,
                    "source_billing_days": days,
                    "kwh_per_day": per_day,
                }
            )
        except (InvalidOperation, KeyError, ValueError) as exc:
            raise BillRateImportError(
                "billing days do not produce an exact supported daily allowance"
            ) from exc
        corrected = days
        extraction.billing_period_days = days
        extraction.tier_threshold_rule = rule.model_dump(mode="json")
        extraction.tier_threshold_basis = "bill_baseline_allowance"
        extraction.baseline_allocation_rule = "daily_allowance"
        extraction.candidate_complete = True
        updated_periods: list[dict[str, object]] = []
        for period in extraction.tou_period_definitions:
            updated = dict(period)
            if updated.get("name") == "tier_1":
                updated["tier_start_kwh"] = "0"
                updated["tier_end_kwh"] = str(allowance)
            elif updated.get("name") == "tier_2":
                updated["tier_start_kwh"] = str(allowance)
                updated["tier_end_kwh"] = None
            updated_periods.append(updated)
        extraction.tou_period_definitions = updated_periods
        extraction.tier_threshold_definitions = [
            {"start_kwh": "0", "end_kwh": str(allowance)},
            {"start_kwh": str(allowance), "end_kwh": None},
        ]
    else:
        setattr(extraction, payload.field, corrected)
    session.add(
        UtilityBillRateCorrection(
            extraction_id=extraction.id,
            allowed_field=payload.field,
            prior_value_hash=hashlib.sha256(str(prior).encode()).hexdigest(),
            corrected_value=str(corrected),
            corrected_by_user_id=user.id,
        )
    )
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="BILL_RATE_FIELD_CORRECTED",
            target_type="utility_bill_rate_extraction",
            target_id=extraction.id,
            correlation_id=request.state.correlation_id,
            details={"allowed_field": payload.field},
        )
    )
    await session.commit()
    return {"extraction": _safe_extraction(extraction, upload)}


@router.post("/bill-rate-imports/{extraction_id}/publish", status_code=201)
async def publish_bill_rate_import(
    extraction_id: str,
    payload: RatePublishRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("rates.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    extraction, upload = await _scoped_extraction(
        session,
        user_id=user.id,
        extraction_id=extraction_id,
        for_update=True,
    )
    if extraction.state != "review_required":
        raise BillRateImportError("rate extraction is not publishable")
    if not extraction.candidate_complete:
        raise BillRateImportError(
            "This bill provides exact rates but not a complete reusable tariff schedule. "
            "Retain the configured threshold and complete review through the manual or "
            "official-source workflow.",
            code="RATE_CANDIDATE_INCOMPLETE",
        )
    if payload.assign_to_utility_account_id:
        # Preserve home non-disclosure before reporting anything about the
        # draft's publication semantics. The row is locked later in the
        # established rate-assignment lock order.
        scoped_account_id = await session.scalar(
            select(UtilityAccount.id).where(
                UtilityAccount.id == payload.assign_to_utility_account_id,
                UtilityAccount.home_id == upload.home_id,
            )
        )
        if scoped_account_id is None:
            raise NotFound("utility account does not exist")
    day_sensitive = any(
        period.get("day_type") in {"weekday", "weekend", "holiday"}
        for period in extraction.tou_period_definitions
        if isinstance(period, dict)
    )
    if day_sensitive and extraction.holiday_treatment in {
        "weekend_schedule",
        "explicit_schedule",
        "unresolved",
    }:
        # Bill PDFs are rate-only inputs and this extraction model deliberately
        # has no place for an external holiday calendar. A day-sensitive draft
        # must be completed through the official-source workflow, which binds
        # authoritative bounded holiday evidence to the immutable version.
        raise BillRateImportError(
            "This schedule requires an authoritative holiday calendar before it can be "
            "published. Complete it through the official-source workflow.",
            code="RATE_HOLIDAY_CALENDAR_REQUIRED",
        )
    try:
        threshold_rule = (
            TierThresholdRuleDraft.model_validate(extraction.tier_threshold_rule)
            if extraction.plan_classification == "seasonal_tiered"
            else None
        )
    except ValueError as exc:
        raise BillRateImportError(
            "The structured tier threshold is incomplete or invalid.",
            code="RATE_CANDIDATE_INCOMPLETE",
        ) from exc
    if threshold_rule is not None and (
        threshold_rule.kwh_per_day is None or threshold_rule.source_billing_days is None
    ):
        raise BillRateImportError(
            "The structured tier threshold still requires billing days.",
            code="RATE_CANDIDATE_INCOMPLETE",
        )
    plan, version_number = await locked_rate_plan_and_next_version(
        session,
        name=extraction.rate_plan_name,
        utility_name=extraction.utility_name,
        rate_class=extraction.rate_class,
    )
    plan.utility_code = plan.utility_code or "SCE"
    plan.public_plan_name = plan.public_plan_name or extraction.rate_plan_name
    plan.canonical_name = plan.canonical_name or extraction.rate_plan_name
    plan.official_schedule_code = plan.official_schedule_code or (
        "D" if extraction.rate_plan_name.upper() == "DOMESTIC" else None
    )
    if plan.plan_type == "unknown":
        plan.plan_type = extraction.plan_classification
    plan.currency = "USD"
    plan.energy_unit = "kWh"
    daily = Decimal("0")
    monthly = Decimal("0")
    meter = Decimal("0")
    other_fixed = Decimal("0")
    fixed_charges: list[dict[str, object]] = []
    for component in extraction.reusable_price_components:
        kind = component.get("kind")
        if kind not in {
            "daily_fixed",
            "monthly_fixed",
            "meter_fixed",
            "other_fixed",
            "daily_fixed_charge",
            "monthly_fixed_charge",
            "meter_charge",
            "other_fixed_charge",
        }:
            continue
        amount = Decimal(str(component["amount"]))
        unit = component.get("unit")
        unit_text = unit if isinstance(unit, str) else None
        canonical_kind = {
            "daily_fixed": "daily_fixed_charge",
            "monthly_fixed": "monthly_fixed_charge",
            "meter_fixed": "meter_charge",
            "other_fixed": "other_fixed_charge",
        }.get(str(kind), str(kind))
        default_applies = {
            "daily_fixed_charge": "per_account_per_day",
            "monthly_fixed_charge": "per_account_per_month",
            "meter_charge": "per_meter_per_day"
            if unit_text == "USD/day"
            else "per_meter_per_month"
            if unit_text == "USD/month"
            else None,
            "other_fixed_charge": "per_account_per_day"
            if unit_text == "USD/day"
            else "per_account_per_month"
            if unit_text == "USD/month"
            else None,
        }[canonical_kind]
        applies = component.get("applies") or default_applies
        allowed_applies = {
            "daily_fixed_charge": {"per_account_per_day"},
            "monthly_fixed_charge": {"per_account_per_month"},
            "meter_charge": {
                "per_meter_per_day",
                "per_meter_per_month",
                "per_meter_per_cycle",
            },
            "other_fixed_charge": {
                "per_account_per_day",
                "per_account_per_month",
                "per_account_per_cycle",
            },
        }[canonical_kind]
        if applies not in allowed_applies:
            raise BillRateImportError(
                "This fixed charge does not include exact account/meter recurrence semantics.",
                code="RATE_FIXED_CHARGE_EVALUATOR_REQUIRED",
            )
        fixed_charges.append(
            {
                "charge": canonical_kind,
                "amount": format(amount, "f"),
                "currency": "USD",
                "applies": applies,
            }
        )
        if canonical_kind == "daily_fixed_charge":
            daily += amount
        elif canonical_kind == "monthly_fixed_charge":
            monthly += amount
        elif canonical_kind == "meter_charge":
            meter += amount
        else:
            other_fixed += amount
    version = RatePlanVersion(
        rate_plan_id=plan.id,
        version=version_number,
        effective_start=payload.effective_start.astimezone(UTC),
        effective_end=payload.effective_end.astimezone(UTC) if payload.effective_end else None,
        timezone="America/Los_Angeles",
        pricing_model=extraction.plan_classification,
        source_version=upload.artifact_sha256,
        holiday_treatment=extraction.holiday_treatment,
        eligibility_evidence=(
            [
                {
                    "evidence_type": "account_tier_threshold_requirement",
                    "rule_type": "daily_allowance",
                    "season": threshold_rule.season,
                    "tier1_boundary_inclusive": True,
                    "source_label": "reviewed SCE bill baseline allowance",
                    "account_scoped": True,
                }
            ]
            if threshold_rule is not None
            else []
        ),
        fixed_charges=fixed_charges,
        price_components=[dict(component) for component in extraction.reusable_price_components],
        daily_fixed_charge=daily,
        monthly_fixed_charge=monthly,
        meter_charge=meter,
        other_fixed_charge=other_fixed,
        baseline_credit_per_kwh=extraction.baseline_credit_rate or Decimal("0"),
        tier_threshold_kwh_per_day=None,
        tier_threshold_season=None,
        tier_threshold_source_kwh=None,
        tier_threshold_source_days=None,
        tier1_boundary_inclusive=(
            threshold_rule.tier1_boundary_inclusive if threshold_rule is not None else True
        ),
        source_hash=upload.artifact_sha256,
        algorithm_version="cost-v1",
        state="draft",
        published_by_user_id=user.id,
        published_at=datetime.now(UTC),
    )
    session.add(version)
    await session.flush()
    for period in extraction.tou_period_definitions:
        session.add(
            RatePeriod(
                rate_plan_version_id=version.id,
                season=period["season"],
                day_type=period["day_type"],
                period_name=period["name"],
                start_minute=int(period["start_minute"]),
                end_minute=int(period["end_minute"]),
                price_per_kwh=Decimal(str(period["price_per_kwh"])),
                delivery_per_kwh=Decimal(str(period.get("delivery_per_kwh", 0))),
                generation_per_kwh=Decimal(str(period.get("generation_per_kwh", 0))),
                rate_components=[
                    {
                        "component": "delivery_rate",
                        "amount_per_kwh": str(period.get("delivery_per_kwh", 0)),
                    },
                    {
                        "component": "generation_rate",
                        "amount_per_kwh": str(period.get("generation_per_kwh", 0)),
                    },
                ],
                baseline_credit_per_kwh=extraction.baseline_credit_rate or Decimal("0"),
                tier_start_kwh=(
                    Decimal("1")
                    if threshold_rule is not None
                    and Decimal(str(period.get("tier_start_kwh", 0))) > 0
                    else Decimal(str(period.get("tier_start_kwh", 0)))
                ),
                tier_end_kwh=(
                    Decimal("1")
                    if threshold_rule is not None and period.get("tier_end_kwh") is not None
                    else Decimal(str(period["tier_end_kwh"]))
                    if period.get("tier_end_kwh") is not None
                    else None
                ),
                boundary_inclusive=(
                    threshold_rule.tier1_boundary_inclusive if threshold_rule is not None else True
                ),
                threshold_basis=(
                    "account_daily_baseline"
                    if threshold_rule is not None
                    else extraction.tier_threshold_basis
                ),
                threshold_value=None,
                source_label=str(period["name"]),
            )
        )
    # Child rows are flushed while the version is still a draft.  The ORM and
    # PostgreSQL guards reject every later mutation of a published version or
    # any of its schedule/holiday rows.
    await session.flush()
    version.state = "published"
    account_threshold_id: str | None = None
    if payload.assign_to_utility_account_id:
        account = await session.scalar(
            select(UtilityAccount)
            .where(
                UtilityAccount.id == payload.assign_to_utility_account_id,
                UtilityAccount.home_id == upload.home_id,
            )
            .with_for_update()
        )
        if account is None:
            raise NotFound("utility account does not exist")
        assignment, _created = await replace_rate_assignment(
            session,
            account=account,
            version=version,
            actor_user_id=user.id,
        )
        if threshold_rule is not None:
            assert threshold_rule.kwh_per_day is not None
            assert threshold_rule.source_billing_days is not None
            account_threshold, _threshold_created = await replace_utility_account_tier_threshold(
                session,
                account=account,
                rate_plan_id=plan.id,
                season=threshold_rule.season,
                kwh_per_day=threshold_rule.kwh_per_day,
                source_allowance_kwh=threshold_rule.source_allowance_kwh,
                source_billing_days=threshold_rule.source_billing_days,
                tier1_boundary_inclusive=threshold_rule.tier1_boundary_inclusive,
                source_label="reviewed SCE bill baseline allowance",
                source_kind="bill_rate_import",
                source_artifact_sha256=upload.artifact_sha256,
                effective_start=assignment.effective_start,
                effective_end=assignment.effective_end,
                actor_user_id=user.id,
            )
            account_threshold_id = account_threshold.id
        # Selected-cost rows are mutable pointers into immutable cost evidence.
        # Invalidate only pointers in the new assignment's home/effective range;
        # the worker will create a new CostRun/IntervalCost and atomically select
        # it without deleting the prior calculation.
        affected_conditions = [
            Device.home_id == account.home_id,
            NormalizedInterval.start_utc >= version.effective_start,
        ]
        if assignment.effective_end is not None:
            affected_conditions.append(NormalizedInterval.end_utc <= assignment.effective_end)
        affected_intervals = (
            select(NormalizedInterval.id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(*affected_conditions)
        )
        await session.execute(
            delete(IntervalCostSelection).where(
                IntervalCostSelection.normalized_interval_id.in_(affected_intervals)
            )
        )
    extraction.state = "published"
    extraction.reviewer_user_id = user.id
    extraction.reviewed_at = datetime.now(UTC)
    extraction.resulting_rate_version_id = version.id
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="RATE_VERSION_PUBLISHED_FROM_BILL_RATE_SOURCE",
            target_type="rate_plan_version",
            target_id=version.id,
            correlation_id=request.state.correlation_id,
            details={
                "source_artifact_sha256": upload.artifact_sha256,
                "utility_account_tier_threshold_id": account_threshold_id,
            },
        )
    )
    await session.commit()
    return {
        "rate_plan_version": {
            "id": version.id,
            "plan_id": plan.id,
            "version": version.version,
            "effective_start": version.effective_start,
            "effective_end": version.effective_end,
            "source_artifact_sha256": version.source_hash,
        }
    }


@router.delete("/bill-rate-imports/{extraction_id}", status_code=204)
async def delete_bill_rate_import(
    extraction_id: str,
    request: Request,
    user: CurrentUser = Depends(require_permission("rates.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    extraction, upload = await _scoped_extraction(
        session,
        user_id=user.id,
        extraction_id=extraction_id,
        for_update=True,
    )
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="BILL_RATE_IMPORT_DELETED",
            target_type="utility_bill_rate_extraction",
            target_id=extraction.id,
            correlation_id=request.state.correlation_id,
            details={
                "artifact_sha256": upload.artifact_sha256,
                "prior_state": extraction.state,
                "published_rate_version_retained": extraction.resulting_rate_version_id,
                "original_pdf_bytes_retained": False,
            },
        )
    )
    await session.delete(extraction)
    await session.flush()
    await session.delete(upload)
    await session.commit()


@router.post("/billing/cycle-adjustments", status_code=201)
async def create_billing_cycle_adjustment(
    payload: BillingCycleAdjustmentRequest,
    request: Request,
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("billing.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    account = await session.scalar(
        select(UtilityAccount)
        .where(
            UtilityAccount.id == payload.utility_account_id,
            UtilityAccount.home_id == scoped_home_id,
        )
        .with_for_update()
    )
    if account is None:
        raise NotFound("utility account does not exist")
    now = datetime.now(UTC)
    cycle_start, _cycle_end = _current_billing_cycle_bounds(account, now)
    if payload.cycle_start_utc.astimezone(UTC) != cycle_start:
        raise InvalidRequest("cycle adjustment must target the current billing cycle start")
    through_utc = payload.through_utc.astimezone(UTC)
    if through_utc < cycle_start or through_utc > now:
        raise InvalidRequest("adjustment through timestamp must be within the current cycle")
    existing = await session.scalar(
        select(BillingCycleAdjustment.id).where(
            BillingCycleAdjustment.utility_account_id == account.id,
            BillingCycleAdjustment.cycle_start_utc == cycle_start,
            BillingCycleAdjustment.reason == "verified_cycle_to_date_seed",
        )
    )
    if existing is not None:
        raise RateWorkflowConflict("this billing cycle already has a verified energy seed")
    adjustment = BillingCycleAdjustment(
        utility_account_id=account.id,
        cycle_start_utc=cycle_start,
        energy_mwh=int(payload.energy_kwh * Decimal(1_000_000)),
        reason="verified_cycle_to_date_seed",
        evidence={
            "source": "administrator_verified_cycle_to_date",
            "note": payload.evidence_note,
            "through_utc": through_utc.isoformat(),
            "written_to_sensor_history": False,
        },
        created_by_user_id=user.id,
    )
    session.add(adjustment)
    await session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="BILLING_CYCLE_ADJUSTMENT_CREATED",
            target_type="billing_cycle_adjustment",
            target_id=adjustment.id,
            correlation_id=request.state.correlation_id,
            details={
                "utility_account_id": account.id,
                "cycle_start_utc": cycle_start.isoformat(),
                "energy_mwh": adjustment.energy_mwh,
                "through_utc": through_utc.isoformat(),
                "raw_history_modified": False,
            },
        )
    )
    await session.commit()
    return {
        "id": adjustment.id,
        "utility_account_id": adjustment.utility_account_id,
        "cycle_start_utc": adjustment.cycle_start_utc,
        "through_utc": through_utc,
        "energy_kwh": Decimal(adjustment.energy_mwh) / Decimal(1_000_000),
        "reason": adjustment.reason,
        "created_at": adjustment.created_at,
        "raw_history_modified": False,
    }


@router.get("/billing")
async def billing_overview(
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("billing.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    accounts = (
        await session.scalars(
            select(UtilityAccount).where(UtilityAccount.home_id == scoped_home_id)
        )
    ).all()
    now = datetime.now(UTC)
    plans: list[dict[str, object]] = []
    for account in accounts:
        selected_rates = (
            await session.execute(
                select(RateAssignment, RatePlanVersion, RatePlan)
                .join(
                    RatePlanVersion,
                    RatePlanVersion.id == RateAssignment.rate_plan_version_id,
                )
                .join(RatePlan, RatePlan.id == RatePlanVersion.rate_plan_id)
                .where(
                    RateAssignment.utility_account_id == account.id,
                    RateAssignment.effective_start <= now,
                    (RateAssignment.effective_end.is_(None) | (RateAssignment.effective_end > now)),
                    RatePlanVersion.state == "published",
                    RatePlanVersion.effective_start <= now,
                    (
                        RatePlanVersion.effective_end.is_(None)
                        | (RatePlanVersion.effective_end > now)
                    ),
                )
                .order_by(RateAssignment.effective_start.desc())
                .limit(2)
            )
        ).all()
        selected_rate = selected_rates[0] if len(selected_rates) == 1 else None
        assignment = selected_rate[0] if selected_rate else None
        version = selected_rate[1] if selected_rate else None
        plan = selected_rate[2] if selected_rate else None
        estimates = (
            await session.scalars(
                select(BillingEstimate)
                .join(
                    BillingEstimateSelection,
                    BillingEstimateSelection.billing_estimate_id == BillingEstimate.id,
                )
                .where(BillingEstimateSelection.utility_account_id == account.id)
                .order_by(BillingEstimate.scope_kind, BillingEstimate.scope_id)
            )
        ).all()
        cycle_start, cycle_end = _current_billing_cycle_bounds(account, now)
        one_version_for_cycle = bool(
            assignment is not None
            and version is not None
            and aware_utc(assignment.effective_start) <= cycle_start
            and (
                assignment.effective_end is None or aware_utc(assignment.effective_end) >= cycle_end
            )
            and aware_utc(version.effective_start) <= cycle_start
            and (version.effective_end is None or aware_utc(version.effective_end) >= cycle_end)
        )
        scope_end = now.replace(second=0, microsecond=0)
        home_total_branch = await session.scalar(
            select(Circuit).where(
                Circuit.home_id == scoped_home_id,
                Circuit.is_home_total.is_(True),
                Circuit.is_billing_source.is_(True),
                Circuit.aggregate_mode == "verified_sum",
                Circuit.non_overlapping_confirmed.is_(True),
            )
        )
        member_ids: tuple[str, ...] = ()
        if home_total_branch is not None:
            member_ids = tuple(
                (
                    await session.scalars(
                        select(Device.id)
                        .where(
                            Device.circuit_id == home_total_branch.id,
                            Device.home_id == scoped_home_id,
                            Device.include_in_aggregate.is_(True),
                        )
                        .order_by(Device.display_order, Device.id)
                    )
                ).all()
            )
        saved_energy_mwh = 0
        reading_coverage = Decimal("0")
        readings_waiting = 0
        expected_member_seconds = Decimal("0")
        reliable_member_seconds = Decimal("0")
        recovered_gap_mwh = 0
        recovered_gap_seconds = 0
        billing_adjustment_mwh = 0
        unresolved_connection_gap_count = 0
        unresolved_gap_events: list[TelemetryEnergyEvent] = []
        unresolved_counter_reset_count = 0
        adjustment = await session.scalar(
            select(BillingCycleAdjustment).where(
                BillingCycleAdjustment.utility_account_id == account.id,
                BillingCycleAdjustment.cycle_start_utc == cycle_start,
                BillingCycleAdjustment.reason == "verified_cycle_to_date_seed",
            )
        )
        automatic_start = cycle_start
        if adjustment is not None:
            billing_adjustment_mwh = adjustment.energy_mwh
            through_value = adjustment.evidence.get("through_utc")
            if isinstance(through_value, str):
                parsed_through = datetime.fromisoformat(through_value.replace("Z", "+00:00"))
                if parsed_through.utcoffset() is not None:
                    automatic_start = max(cycle_start, parsed_through.astimezone(UTC))
        if member_ids and scope_end > cycle_start:
            cycle_intervals = (
                await session.scalars(
                    select(NormalizedInterval)
                    .outerjoin(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
                    .join(Device, Device.id == NormalizedInterval.device_id)
                    .where(
                        NormalizedInterval.device_id.in_(member_ids),
                        or_(
                            NormalizedInterval.source_kind == "stateless_v2",
                            RawReading.reset_generation == Device.reset_generation,
                        ),
                        NormalizedInterval.source_authenticated.is_(True),
                        NormalizedInterval.start_utc >= automatic_start,
                        NormalizedInterval.end_utc <= scope_end,
                    )
                )
            ).all()
            saved_energy_mwh = sum(
                int(interval.energy_mwh)
                for interval in cycle_intervals
                if interval.energy_mwh is not None
            )
            reliable_seconds = sum(
                (
                    Decimal(str((interval.end_utc - interval.start_utc).total_seconds()))
                    * interval.completeness
                    for interval in cycle_intervals
                ),
                Decimal("0"),
            )
            if adjustment is not None:
                reliable_seconds += Decimal(
                    str((automatic_start - cycle_start).total_seconds())
                ) * Decimal(len(member_ids))
            expected_seconds = Decimal(str((scope_end - cycle_start).total_seconds())) * Decimal(
                len(member_ids)
            )
            reliable_member_seconds = reliable_seconds
            expected_member_seconds = expected_seconds
            reading_coverage = min(
                Decimal("1"),
                reliable_seconds / expected_seconds if expected_seconds else Decimal("0"),
            )
            recovered_events = list(
                (
                    await session.scalars(
                        select(TelemetryEnergyEvent).where(
                            TelemetryEnergyEvent.device_id.in_(member_ids),
                            TelemetryEnergyEvent.billing_status == "included",
                            TelemetryEnergyEvent.gap_end_utc > automatic_start,
                            TelemetryEnergyEvent.gap_end_utc <= scope_end,
                        )
                    )
                ).all()
            )
            recovered_gap_mwh = sum(
                int(item.recovered_energy_mwh or 0) for item in recovered_events
            )
            recovered_gap_seconds = sum(
                max(
                    0,
                    int(
                        (
                            aware_utc(item.gap_end_utc) - aware_utc(item.gap_start_utc)
                        ).total_seconds()
                    ),
                )
                for item in recovered_events
                if item.gap_start_utc is not None and item.gap_end_utc is not None
            )
            unresolved_gap_events = list(
                (
                    await session.scalars(
                        select(TelemetryEnergyEvent).where(
                            TelemetryEnergyEvent.device_id.in_(member_ids),
                            TelemetryEnergyEvent.billing_status == "unresolved",
                            TelemetryEnergyEvent.gap_end_utc > automatic_start,
                            TelemetryEnergyEvent.gap_start_utc < scope_end,
                        )
                    )
                ).all()
            )
            unresolved_connection_gap_count = len(unresolved_gap_events)
            unresolved_counter_reset_count = int(
                await session.scalar(
                    select(func.count(Alert.id)).where(
                        Alert.device_id.in_(member_ids),
                        Alert.alert_type == "pzem_energy_counter_reset",
                        Alert.state == "open",
                        Alert.opened_at >= automatic_start,
                        Alert.opened_at < scope_end,
                    )
                )
                or 0
            )
            for member_id in member_ids:
                if await session.get(DeviceTelemetryState, member_id) is not None:
                    continue
                latest_heartbeat = await session.scalar(
                    select(DeviceHeartbeat)
                    .where(DeviceHeartbeat.device_id == member_id)
                    .order_by(DeviceHeartbeat.received_at.desc())
                    .limit(1)
                )
                if latest_heartbeat is not None:
                    readings_waiting += latest_heartbeat.backlog

        gap_estimate = await _estimate_short_gap_energy(
            session,
            events=unresolved_gap_events,
            cycle_start=automatic_start,
            scope_end=scope_end,
            reading_coverage=reading_coverage,
            minimum_coverage=account.estimate_min_coverage,
            maximum_gap_seconds=account.max_estimatable_gap_seconds,
            unresolved_counter_resets=unresolved_counter_reset_count,
        )
        estimated_gap_mwh = Decimal(str(gap_estimate["estimated_mwh"]))
        estimated_lower_mwh = Decimal(str(gap_estimate["lower_mwh"]))
        estimated_upper_mwh = Decimal(str(gap_estimate["upper_mwh"]))
        known_gap_seconds = (
            recovered_gap_seconds
            + cast(int, gap_estimate["estimated_seconds"])
            + cast(int, gap_estimate["unknown_seconds"])
        )
        coverage_gap_seconds = max(
            0,
            int(expected_member_seconds - min(expected_member_seconds, reliable_member_seconds)),
        )
        unclassified_gap_seconds = max(0, coverage_gap_seconds - known_gap_seconds)
        unknown_gap_count = cast(int, gap_estimate["unknown_count"]) + (
            1 if unclassified_gap_seconds else 0
        )
        unknown_gap_seconds = cast(int, gap_estimate["unknown_seconds"]) + unclassified_gap_seconds
        energy_quality = EnergyQuality(
            measured_kwh=Decimal(saved_energy_mwh) / Decimal(1_000_000),
            recovered_kwh=Decimal(recovered_gap_mwh) / Decimal(1_000_000),
            estimated_kwh=estimated_gap_mwh / Decimal(1_000_000),
            estimate_lower_kwh=estimated_lower_mwh / Decimal(1_000_000),
            estimate_upper_kwh=estimated_upper_mwh / Decimal(1_000_000),
            unknown_gap_count=unknown_gap_count,
            unknown_gap_seconds=unknown_gap_seconds,
            estimation_methods=cast(tuple[str, ...], gap_estimate["methods"]),
        )
        confidence, confidence_reasons = estimate_confidence(
            reading_coverage=reading_coverage,
            quality=energy_quality,
            high_coverage=account.estimate_high_coverage,
            minimum_coverage=account.estimate_min_coverage,
            unresolved_counter_resets=unresolved_counter_reset_count,
        )

        periods = (
            (
                await session.scalars(
                    select(RatePeriod).where(RatePeriod.rate_plan_version_id == version.id)
                )
            ).all()
            if version is not None
            else []
        )
        local_now = now.astimezone(ZoneInfo(account.timezone))
        season = (
            season_from_storage(version.season_definitions, local_now)
            if version is not None
            else "summer"
            if local_now.month in (6, 7, 8, 9)
            else "winter"
        )
        seasonal_periods = [period for period in periods if period.season in (season, "all")]
        tier_1_period = next(
            (
                period
                for period in sorted(seasonal_periods, key=lambda item: item.tier_start_kwh)
                if period.tier_start_kwh == 0 and period.tier_end_kwh is not None
            ),
            None,
        )
        tier_2_period = next(
            (
                period
                for period in sorted(seasonal_periods, key=lambda item: item.tier_start_kwh)
                if period.tier_start_kwh > 0 and period.tier_end_kwh is None
            ),
            None,
        )
        cycle_days = (
            cycle_end.astimezone(ZoneInfo(account.timezone)).date()
            - cycle_start.astimezone(ZoneInfo(account.timezone)).date()
        ).days
        account_daily_threshold = (
            await resolve_utility_account_tier_threshold(
                session,
                utility_account_id=account.id,
                rate_plan_id=version.rate_plan_id,
                season=season,
                instant=now,
            )
            if version is not None
            else None
        )
        account_cycle_threshold = (
            await resolve_assigned_utility_account_cycle_tier_threshold(
                session,
                utility_account_id=account.id,
                timezone=account.timezone,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            if version is not None
            else None
        )
        configured_daily_baseline = (
            account.summer_baseline_kwh_per_day
            if season == "summer"
            else account.winter_baseline_kwh_per_day
        )
        tier_1_allowance = (
            account_cycle_threshold.total_kwh
            if account_cycle_threshold is not None
            else configured_daily_baseline * cycle_days
            if configured_daily_baseline is not None
            else version.tier_threshold_kwh_per_day * cycle_days
            if version is not None
            and one_version_for_cycle
            and version.tier_threshold_kwh_per_day is not None
            and version.tier_threshold_season in (season, "all")
            else None
        )
        billing_adjustment_kwh = Decimal(billing_adjustment_mwh) / Decimal(1_000_000)
        saved_usage_kwh = energy_quality.saved_usage_kwh + billing_adjustment_kwh
        current_usage_kwh = saved_usage_kwh + energy_quality.estimated_kwh
        tier_state = "not_confirmed"
        tier_quality = EnergyQuality(
            measured_kwh=energy_quality.measured_kwh + billing_adjustment_kwh,
            recovered_kwh=energy_quality.recovered_kwh,
            estimated_kwh=energy_quality.estimated_kwh,
            estimate_lower_kwh=energy_quality.estimate_lower_kwh,
            estimate_upper_kwh=energy_quality.estimate_upper_kwh,
            unknown_gap_count=energy_quality.unknown_gap_count,
            unknown_gap_seconds=energy_quality.unknown_gap_seconds,
            estimation_methods=energy_quality.estimation_methods,
        )
        if tier_1_allowance is not None and unresolved_counter_reset_count == 0:
            tier_state = tier_state_for_quality(
                quality=tier_quality,
                threshold_kwh=tier_1_allowance,
            )
        local_cycle_start = cycle_start.astimezone(ZoneInfo(account.timezone)).date()
        local_scope_end = scope_end.astimezone(ZoneInfo(account.timezone)).date()
        elapsed_cycle_days = min(cycle_days, max(0, (local_scope_end - local_cycle_start).days))
        tier_breakdown: dict[str, object] | None = None
        calculated_energy_charge: Decimal | None = None
        calculated_fixed_charge: Decimal | None = None
        calculated_total: Decimal | None = None
        projection: dict[str, object] = {
            "status": "insufficient_data",
            "confidence": None,
            "confidence_reasons": ["published_tier_schedule_unavailable"],
            "projected_usage_kwh": None,
            "projected_tier_1_usage_kwh": None,
            "projected_tier_2_usage_kwh": None,
            "projected_tier_1_cost": None,
            "projected_tier_2_cost": None,
            "projected_service_charge": None,
            "projected_total": None,
        }
        has_non_daily_fixed_charge = bool(
            version
            and (
                version.monthly_fixed_charge
                or version.minimum_charge
                or version.meter_charge
                or version.other_fixed_charge
                or any(
                    isinstance(item, dict)
                    and item.get("charge") != "daily_fixed_charge"
                    and Decimal(str(item.get("amount", "0"))) != 0
                    for item in version.fixed_charges
                )
            )
        )
        if (
            tier_1_allowance is not None
            and tier_1_period is not None
            and tier_2_period is not None
            and version is not None
            and one_version_for_cycle
            and unknown_gap_count == 0
            and unresolved_counter_reset_count == 0
        ):
            known = tiered_cost(
                usage_kwh=current_usage_kwh,
                threshold_kwh=tier_1_allowance,
                tier_1_rate=tier_1_period.price_per_kwh,
                tier_2_rate=tier_2_period.price_per_kwh,
                service_days=Decimal(elapsed_cycle_days),
                daily_service_charge=version.daily_fixed_charge,
            )
            calculated_energy_charge = known.tier_1_cost + known.tier_2_cost
            calculated_fixed_charge = None if has_non_daily_fixed_charge else known.service_charge
            calculated_total = None if has_non_daily_fixed_charge else known.total
            tier_breakdown = {
                "tier_1": {
                    "usage_kwh": known.tier_1_usage_kwh,
                    "allowance_kwh": known.threshold_kwh,
                    "remaining_kwh": max(
                        Decimal("0"), known.threshold_kwh - known.tier_1_usage_kwh
                    ),
                    "rate_per_kwh": tier_1_period.price_per_kwh,
                    "cost": known.tier_1_cost,
                },
                "tier_2": {
                    "usage_kwh": known.tier_2_usage_kwh,
                    "starts_above_kwh": known.threshold_kwh,
                    "rate_per_kwh": tier_2_period.price_per_kwh,
                    "cost": known.tier_2_cost,
                },
                "service_charge_to_date": (
                    None if has_non_daily_fixed_charge else known.service_charge
                ),
                "total_to_date": None if has_non_daily_fixed_charge else known.total,
                "calculation_basis": "estimated" if energy_quality.estimated_kwh else "exact",
                "cost_range": (
                    None
                    if not energy_quality.estimated_kwh
                    else {
                        "lower": tiered_cost(
                            usage_kwh=tier_quality.lower_usage_kwh,
                            threshold_kwh=tier_1_allowance,
                            tier_1_rate=tier_1_period.price_per_kwh,
                            tier_2_rate=tier_2_period.price_per_kwh,
                            service_days=Decimal(elapsed_cycle_days),
                            daily_service_charge=version.daily_fixed_charge,
                        ).total,
                        "upper": tiered_cost(
                            usage_kwh=tier_quality.upper_usage_kwh,
                            threshold_kwh=tier_1_allowance,
                            tier_1_rate=tier_1_period.price_per_kwh,
                            tier_2_rate=tier_2_period.price_per_kwh,
                            service_days=Decimal(elapsed_cycle_days),
                            daily_service_charge=version.daily_fixed_charge,
                        ).total,
                    }
                ),
            }
            if has_non_daily_fixed_charge:
                projection["confidence_reasons"] = [
                    "exact_non_daily_fixed_charge_projection_unavailable"
                ]
            else:
                projection = billing_projection(
                    reliable_usage_kwh=current_usage_kwh,
                    reliable_elapsed_hours=Decimal(str((scope_end - cycle_start).total_seconds()))
                    / Decimal(3600),
                    total_cycle_days=cycle_days,
                    threshold_kwh=tier_1_allowance,
                    tier_1_rate=tier_1_period.price_per_kwh,
                    tier_2_rate=tier_2_period.price_per_kwh,
                    daily_service_charge=version.daily_fixed_charge,
                    reading_coverage=reading_coverage,
                    unresolved_counter_resets=unresolved_counter_reset_count,
                    unresolved_connection_gaps=unresolved_connection_gap_count,
                    estimated_energy_kwh=energy_quality.estimated_kwh,
                    unknown_gap_count=unknown_gap_count,
                    minimum_reliable_hours=Decimal(account.projection_minimum_hours),
                    high_coverage=account.estimate_high_coverage,
                    minimum_coverage=account.estimate_min_coverage,
                )
        home_estimate = next(
            (
                estimate
                for estimate in estimates
                if home_total_branch is not None
                and estimate.scope_id == home_total_branch.id
                and estimate.scope_kind == "full_account"
            ),
            None,
        )
        home_estimate_matches_rate = bool(
            home_estimate is not None
            and version is not None
            and home_estimate.rate_plan_version_id == version.id
        )
        estimate_is_current = bool(
            home_estimate_matches_rate
            and tier_state != "not_confirmed"
            and recovered_gap_mwh == 0
            and billing_adjustment_mwh == 0
        )
        if tier_breakdown is not None and estimate_is_current and home_estimate is not None:
            tier_breakdown["service_charge_to_date"] = Decimal(
                home_estimate.fixed_charge_microdollars
            ) / Decimal(1_000_000)
            tier_breakdown["total_to_date"] = Decimal(home_estimate.total_microdollars) / Decimal(
                1_000_000
            )
        availability_reasons: list[dict[str, str]] = []
        if not selected_rates:
            availability_reasons.append(
                {
                    "code": "active_rate_plan_not_assigned",
                    "message": "An active rate plan is not assigned for this date.",
                    "severity": "error",
                }
            )
        elif len(selected_rates) > 1:
            availability_reasons.append(
                {
                    "code": "active_rate_plan_ambiguous",
                    "message": "More than one active rate assignment applies to this date.",
                    "severity": "error",
                }
            )
        if home_total_branch is None or not member_ids:
            availability_reasons.append(
                {
                    "code": "billing_source_service_branch_not_configured",
                    "message": "A verified Main service billing source is not configured.",
                    "severity": "error",
                }
            )
        if version is not None and not one_version_for_cycle:
            availability_reasons.append(
                {
                    "code": "rate_not_effective_for_full_billing_cycle",
                    "message": "The active rate version does not cover the complete billing cycle.",
                    "severity": "error",
                }
            )
        if version is not None and version.pricing_model in ("tiered", "seasonal_tiered"):
            if tier_1_allowance is None:
                availability_reasons.append(
                    {
                        "code": "home_baseline_not_configured",
                        "message": "Home baseline is not configured for this billing cycle.",
                        "severity": "error",
                    }
                )
            if tier_1_period is None or tier_2_period is None:
                availability_reasons.append(
                    {
                        "code": "active_rate_plan_incomplete",
                        "message": (
                            "The active tiered rate plan is missing a Tier 1 or Tier 2 price."
                        ),
                        "severity": "error",
                    }
                )
        if unresolved_counter_reset_count:
            availability_reasons.append(
                {
                    "code": "unresolved_counter_reset",
                    "message": "A meter reset prevents supported billing estimation.",
                    "severity": "error",
                }
            )
        if unknown_gap_count:
            availability_reasons.append(
                {
                    "code": "unknown_gap_energy",
                    "message": (
                        f"Energy remains unknown across {unknown_gap_count} billing gap"
                        f"{'s' if unknown_gap_count != 1 else ''}."
                    ),
                    "severity": "error",
                }
            )
        elif energy_quality.estimated_kwh:
            availability_reasons.append(
                {
                    "code": "estimated_missing_energy",
                    "message": (
                        f"Some energy is estimated because "
                        f"{(Decimal('1') - reading_coverage) * Decimal('100'):.2f}% "
                        "of expected reading time is missing."
                    ),
                    "severity": "warning",
                }
            )
        elif energy_quality.recovered_kwh:
            availability_reasons.append(
                {
                    "code": "cumulative_energy_recovered",
                    "message": (
                        "Gap energy was recovered from the authenticated cumulative meter total; "
                        "the exact power pattern remains unavailable."
                    ),
                    "severity": "info",
                }
            )
        tou_gap_unallocated = bool(
            version is not None
            and version.pricing_model
            in (
                "time_of_use",
                "seasonal_time_of_use",
                "time_of_use_with_baseline_credit",
            )
            and (energy_quality.recovered_kwh or energy_quality.estimated_kwh)
        )
        if tou_gap_unallocated:
            availability_reasons.append(
                {
                    "code": "tou_gap_time_allocation_unavailable",
                    "message": (
                        "Recovered or estimated gap energy cannot be assigned to a TOU period "
                        "exactly; only measured-period cost is exact."
                    ),
                    "severity": "warning",
                }
            )
        blocking_reason_codes = {
            item["code"] for item in availability_reasons if item["severity"] == "error"
        }
        calculation_state = billing_calculation_state(
            has_blocking_reason=bool(blocking_reason_codes),
            has_estimated_energy=bool(energy_quality.estimated_kwh),
            tou_gap_unallocated=tou_gap_unallocated,
        )
        current_rate_plan = {
            "rate_plan_id": plan.id if plan else None,
            "rate_plan_version_id": version.id if version else None,
            "name": plan.name if plan else None,
            "utility_name": plan.utility_name if plan else account.utility_name,
            "rate_class": plan.rate_class if plan else None,
            "effective_start": version.effective_start if version else None,
            "currently_used": version is not None,
            "tier_1_price_per_kwh": tier_1_period.price_per_kwh
            if tier_1_period is not None
            else None,
            "tier_2_price_per_kwh": tier_2_period.price_per_kwh
            if tier_2_period is not None
            else None,
            "daily_service_charge": version.daily_fixed_charge if version else None,
            "daily_baseline_allowance_kwh": (
                account_daily_threshold.kwh_per_day
                if account_daily_threshold is not None
                else account.summer_baseline_kwh_per_day
                if season == "summer" and account.summer_baseline_kwh_per_day is not None
                else account.winter_baseline_kwh_per_day
                if season == "winter" and account.winter_baseline_kwh_per_day is not None
                else version.tier_threshold_kwh_per_day
                if version is not None
                else None
            ),
            "daily_baseline_source": (
                "account_effective_evidence"
                if account_daily_threshold is not None
                else "settings_seasonal_baseline"
                if configured_daily_baseline is not None
                else "legacy_rate_version"
                if version is not None and version.tier_threshold_kwh_per_day is not None
                else None
            ),
            "generation_service": account.cca_provider or "SCE",
            "generation_service_kind": account.generation_service_kind,
            "currency": account.currency,
            "baseline_region": account.baseline_region,
            "all_electric": account.all_electric,
            "medical_baseline": account.medical_baseline,
            "heat_pump_allocation": account.heat_pump_allocation,
        }
        current_billing_cycle = {
            "start_utc": cycle_start,
            "end_utc": cycle_end,
            "service_branch_id": home_total_branch.id if home_total_branch else None,
            "service_branch_name": home_total_branch.name if home_total_branch else None,
            "saved_usage_kwh": saved_usage_kwh if member_ids else None,
            "current_usage_kwh": current_usage_kwh if member_ids else None,
            "reading_coverage": reading_coverage if member_ids else None,
            "readings_waiting_to_sync": readings_waiting if member_ids else None,
            "recovered_gap_energy_kwh": Decimal(recovered_gap_mwh) / Decimal(1_000_000),
            "measured_energy_kwh": energy_quality.measured_kwh,
            "estimated_missing_energy_kwh": energy_quality.estimated_kwh,
            "estimated_missing_energy_lower_kwh": energy_quality.estimate_lower_kwh,
            "estimated_missing_energy_upper_kwh": energy_quality.estimate_upper_kwh,
            "unknown_energy_kwh": None if unknown_gap_count else Decimal("0"),
            "unknown_gap_seconds": unknown_gap_seconds,
            "billing_adjustment_kwh": billing_adjustment_kwh,
            "energy_quality": {
                "measured_kwh": energy_quality.measured_kwh,
                "recovered_kwh": energy_quality.recovered_kwh,
                "estimated_kwh": energy_quality.estimated_kwh,
                "unknown_kwh": None if unknown_gap_count else Decimal("0"),
                "unknown_gap_count": unknown_gap_count,
                "unknown_gap_seconds": unknown_gap_seconds,
                "estimation_methods": energy_quality.estimation_methods,
                "estimate_details": gap_estimate["details"],
                "raw_history_modified": False,
            },
            "unresolved_connection_gap_count": unresolved_connection_gap_count,
            "unresolved_counter_reset_count": unresolved_counter_reset_count,
            "tier_state": tier_state,
            "tier_confirmation_rule": (
                "cycle_total_including_recovered_and_bounded_estimated_energy"
            ),
            "tier_1_allowance_kwh": tier_1_allowance,
            "tier_1_remaining_kwh": max(Decimal("0"), tier_1_allowance - current_usage_kwh)
            if tier_1_allowance is not None and tier_state != "not_confirmed"
            else None,
            "amount_above_tier_1_kwh": max(Decimal("0"), current_usage_kwh - tier_1_allowance)
            if tier_1_allowance is not None and tier_state != "not_confirmed"
            else None,
            "estimated_energy_charges": (
                Decimal(home_estimate.energy_cost_microdollars) / Decimal(1_000_000)
                if estimate_is_current and home_estimate is not None
                else (calculated_energy_charge)
            ),
            "estimated_fixed_charges": (
                Decimal(home_estimate.fixed_charge_microdollars) / Decimal(1_000_000)
                if estimate_is_current and home_estimate is not None
                else calculated_fixed_charge
            ),
            "estimated_total": (
                Decimal(home_estimate.total_microdollars) / Decimal(1_000_000)
                if estimate_is_current and home_estimate is not None
                else calculated_total
            ),
            "tier_breakdown": tier_breakdown,
            "projection": projection,
            "calculation_state": calculation_state,
            "confidence": None if blocking_reason_codes else confidence,
            "confidence_reasons": confidence_reasons,
            "availability_reasons": availability_reasons,
            "cost_to_date": (
                tier_breakdown.get("total_to_date")
                if tier_breakdown is not None
                else Decimal(home_estimate.total_microdollars) / Decimal(1_000_000)
                if tou_gap_unallocated and home_estimate_matches_rate and home_estimate is not None
                else None
            ),
            "measured_cost_to_date": (
                Decimal(home_estimate.total_microdollars) / Decimal(1_000_000)
                if tou_gap_unallocated and home_estimate_matches_rate and home_estimate is not None
                else None
            ),
            "cost_range": tier_breakdown.get("cost_range") if tier_breakdown is not None else None,
            "cost_basis": (
                "measured_periods_exact_gap_energy_unallocated"
                if tou_gap_unallocated
                else "cycle_total"
            ),
            "tou_unallocated_gap_energy_kwh": (
                energy_quality.recovered_kwh + energy_quality.estimated_kwh
                if tou_gap_unallocated
                else Decimal("0")
            ),
        }
        plans.append(
            {
                "utility_account_id": account.id,
                "plan_name": plan.name if plan else None,
                "rate_version_id": version.id if version else None,
                "effective_start": version.effective_start if version else None,
                "cost_scope": account.cost_scope,
                "baseline_credit_included": bool(
                    version
                    and version.baseline_credit_per_kwh > 0
                    and account.cost_scope == "full_account"
                ),
                "fixed_charges_included": account.cost_scope == "full_account",
                "cca_or_direct_access": account.cca_provider,
                "home_total_branch": {
                    "id": home_total_branch.id,
                    "name": home_total_branch.name,
                    "description": home_total_branch.description,
                    "purpose": home_total_branch.purpose,
                    "is_home_total": home_total_branch.is_home_total,
                    "non_overlapping_confirmed": home_total_branch.non_overlapping_confirmed,
                    "device_ids": member_ids,
                }
                if home_total_branch is not None
                else None,
                "current_rate_plan": current_rate_plan,
                "current_billing_cycle": current_billing_cycle,
                "estimates": [
                    {
                        "id": estimate.id,
                        "kind": estimate.estimate_kind,
                        "scope_kind": estimate.scope_kind,
                        "scope_id": estimate.scope_id,
                        "member_device_ids": estimate.member_device_ids,
                        "rate_plan_version_id": estimate.rate_plan_version_id,
                        "scope_start_utc": estimate.scope_start_utc,
                        "scope_end_utc": estimate.scope_end_utc,
                        "sensor_energy_kwh": Decimal(estimate.sensor_energy_mwh)
                        / Decimal(1_000_000),
                        "energy_cost": Decimal(estimate.energy_cost_microdollars)
                        / Decimal(1_000_000),
                        "fixed_charge": Decimal(estimate.fixed_charge_microdollars)
                        / Decimal(1_000_000),
                        "credits": Decimal(estimate.credit_microdollars) / Decimal(1_000_000),
                        "total": Decimal(estimate.total_microdollars) / Decimal(1_000_000),
                        "completeness": estimate.completeness,
                        "missing_intervals": estimate.missing_intervals,
                        "calculated_at": estimate.calculated_at,
                    }
                    for estimate in estimates
                ],
            }
        )
    return {
        "accounts": plans,
        "usage_source": "authenticated PZEM-004T sensor intervals only",
        "rate_import_notice": "PDFs create reviewed reusable rate-plan drafts only.",
    }


@router.get("/rate-sources/candidates")
async def list_rate_source_candidates(
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("rates.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    rows = (
        await session.execute(
            select(RateCandidate, RateSourceRevision, RateSource)
            .join(
                RateSourceRevision,
                RateSourceRevision.id == RateCandidate.source_revision_id,
            )
            .join(RateSource, RateSource.id == RateSourceRevision.source_id)
            .where(
                select(RateSyncRun.id)
                .where(
                    RateSyncRun.home_id == scoped_home_id,
                    RateSyncRun.revision_id == RateSourceRevision.id,
                )
                .exists()
            )
            .order_by(RateCandidate.created_at.desc(), RateCandidate.id.desc())
            .limit(100)
        )
    ).all()
    reviews = {
        review.candidate_id: review
        for review in (
            await session.scalars(
                select(RateCandidateReview).where(
                    RateCandidateReview.home_id == scoped_home_id,
                    RateCandidateReview.candidate_id.in_(
                        [candidate.id for candidate, _, _ in rows]
                    ),
                )
            )
        ).all()
    }
    return {
        "home_id": scoped_home_id,
        "candidates": [
            {
                "id": candidate.id,
                "state": candidate.state,
                "created_at": candidate.created_at,
                "reviewed_at": candidate.reviewed_at,
                "source": {
                    "id": source.id,
                    "name": source.name,
                    "url": source.https_url,
                    "revision_id": revision.id,
                    "artifact_sha256": revision.artifact_sha256,
                    "retrieved_at": revision.retrieved_at,
                    "parser_version": revision.parser_version,
                },
                "normalized_rates": candidate.normalized_rates,
                "validation_evidence": candidate.validation_evidence,
                "diff": candidate.diff,
                "manual_approval_required": True,
                "workflow": safe_review(reviews.get(candidate.id)),
            }
            for candidate, revision, source in rows
        ],
    }


@router.get("/rate-sources/catalog")
async def list_sce_rate_catalog(
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("rates.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Return the latest explicit result for every discovered official SCE plan."""

    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    queried_rows = (
        await session.execute(
            select(SceCatalogEntry, RateSourceRevision, RateSource)
            .join(
                RateSourceRevision,
                RateSourceRevision.id == SceCatalogEntry.source_revision_id,
            )
            .join(RateSource, RateSource.id == RateSourceRevision.source_id)
            .where(
                select(RateSyncRun.id)
                .where(
                    RateSyncRun.home_id == scoped_home_id,
                    RateSyncRun.revision_id == RateSourceRevision.id,
                )
                .exists()
            )
            .order_by(
                RateSourceRevision.retrieved_at.desc(),
                SceCatalogEntry.updated_at.desc(),
                SceCatalogEntry.id.desc(),
            )
        )
    ).all()

    def valid_catalog_entry(entry: SceCatalogEntry) -> bool:
        if entry.canonical_name in SUPPORTED_PLAN_NAMES:
            return True
        normalized = entry.normalized_plan if isinstance(entry.normalized_plan, dict) else {}
        discovery = normalized.get("discovery_evidence")
        return (
            isinstance(discovery, dict)
            and discovery.get("kind") == "primary_plan_heading"
            and discovery.get("crawler_version") == CATALOG_CRAWLER_VERSION
        )

    # Old broad-discovery rows remain immutable source evidence, but cannot be
    # active catalog plans. Future structurally discovered primary headings are
    # retained as parser-update records through the bounded evidence marker.
    rows = [row for row in queried_rows if valid_catalog_entry(row[0])]
    available_revision_ids = {revision.id for _entry, revision, _source in rows}

    now = datetime.now(UTC)
    active_names = set(
        (
            await session.scalars(
                select(RatePlan.name)
                .join(RatePlanVersion, RatePlanVersion.rate_plan_id == RatePlan.id)
                .join(
                    RateAssignment,
                    RateAssignment.rate_plan_version_id == RatePlanVersion.id,
                )
                .join(
                    UtilityAccount,
                    UtilityAccount.id == RateAssignment.utility_account_id,
                )
                .where(
                    UtilityAccount.home_id == scoped_home_id,
                    RateAssignment.effective_start <= now,
                    (RateAssignment.effective_end.is_(None) | (RateAssignment.effective_end > now)),
                    RatePlanVersion.state == "published",
                )
            )
        ).all()
    )
    latest_catalog_run = await session.scalar(
        select(RateSyncRun)
        .join(RateSource, RateSource.id == RateSyncRun.source_id)
        .where(
            RateSyncRun.home_id == scoped_home_id,
            RateSource.name == SCE_CATALOG_SOURCE_NAME,
            RateSyncRun.completed_at.is_not(None),
        )
        .order_by(RateSyncRun.completed_at.desc(), RateSyncRun.id.desc())
        .limit(1)
    )
    complete_catalog_runs = (
        await session.scalars(
            select(RateSyncRun)
            .join(RateSource, RateSource.id == RateSyncRun.source_id)
            .where(
                RateSyncRun.home_id == scoped_home_id,
                RateSource.name == SCE_CATALOG_SOURCE_NAME,
                RateSyncRun.event_code == "SCE_CATALOG_CRAWL_COMPLETE",
                RateSyncRun.completed_at.is_not(None),
                RateSyncRun.revision_id.is_not(None),
            )
            .order_by(RateSyncRun.completed_at.desc(), RateSyncRun.id.desc())
            .limit(50)
        )
    ).all()
    last_complete_catalog_run: RateSyncRun | None = None
    for complete_run in complete_catalog_runs:
        candidate_manifest = complete_run.evidence.get("catalog_crawl_manifest")
        candidate_plans = (
            candidate_manifest.get("plans") if isinstance(candidate_manifest, dict) else None
        )
        candidate_entry_count = sum(
            revision.id == complete_run.revision_id for _entry, revision, _source in rows
        )
        if (
            isinstance(candidate_manifest, dict)
            and isinstance(candidate_plans, list)
            and complete_run.revision_id in available_revision_ids
            and _catalog_manifest_proves_discovery_closure(candidate_manifest)
            and candidate_entry_count == len(candidate_plans)
        ):
            last_complete_catalog_run = complete_run
            break
    last_success = (
        last_complete_catalog_run.completed_at if last_complete_catalog_run is not None else None
    )
    latest_manifest: dict[str, object] | None = None
    if latest_catalog_run is not None:
        raw_manifest = latest_catalog_run.evidence.get("catalog_crawl_manifest")
        if isinstance(raw_manifest, dict):
            latest_manifest = raw_manifest
    raw_closure = latest_manifest.get("closure") if latest_manifest is not None else None
    closure = raw_closure if isinstance(raw_closure, dict) else {}
    latest_manifest_revision_id = (
        latest_catalog_run.revision_id
        if latest_catalog_run is not None and latest_manifest is not None
        else None
    )
    if latest_manifest_revision_id not in available_revision_ids:
        latest_manifest_revision_id = None
    complete_revision_id = (
        last_complete_catalog_run.revision_id if last_complete_catalog_run is not None else None
    )
    latest_snapshot_by_name: dict[str, tuple[SceCatalogEntry, RateSourceRevision, RateSource]] = {}
    complete_snapshot_by_name: dict[
        str, tuple[SceCatalogEntry, RateSourceRevision, RateSource]
    ] = {}
    for entry, revision, source in rows:
        if revision.id == latest_manifest_revision_id:
            latest_snapshot_by_name.setdefault(
                entry.canonical_name,
                (entry, revision, source),
            )
        if revision.id == complete_revision_id:
            complete_snapshot_by_name.setdefault(
                entry.canonical_name,
                (entry, revision, source),
            )

    if latest_manifest_revision_id is None and not complete_snapshot_by_name and rows:
        fallback_revision_id = rows[0][1].id
        for entry, revision, source in rows:
            if revision.id == fallback_revision_id:
                latest_snapshot_by_name.setdefault(
                    entry.canonical_name,
                    (entry, revision, source),
                )

    manifest_plans = latest_manifest.get("plans") if latest_manifest is not None else None
    manifest_plan_count = len(manifest_plans) if isinstance(manifest_plans, list) else -1
    catalog_ready = (
        latest_catalog_run is not None
        and latest_catalog_run.event_code == "SCE_CATALOG_CRAWL_COMPLETE"
        and latest_manifest is not None
        and _catalog_manifest_proves_discovery_closure(latest_manifest)
        and len(latest_snapshot_by_name) == manifest_plan_count
    )
    latest_by_name = (
        dict(latest_snapshot_by_name)
        if catalog_ready
        else {**complete_snapshot_by_name, **latest_snapshot_by_name}
    )
    plans_silently_omitted = 0 if catalog_ready else None
    raw_completeness_reason = closure.get("reason")
    completeness_reason = (
        str(raw_completeness_reason)
        if isinstance(raw_completeness_reason, str) and raw_completeness_reason
        else "bounded_crawl_not_yet_completed"
        if latest_catalog_run is None
        else "latest_official_check_failed_before_closure_manifest"
    )

    entries: list[dict[str, object]] = []
    effective_dates: list[str] = []
    for canonical_name, latest_result in latest_by_name.items():
        latest_entry, latest_revision, _latest_source = latest_result
        selected_result = latest_result
        retained_last_known_good = False
        complete_result = complete_snapshot_by_name.get(canonical_name)
        if not catalog_ready and complete_result is not None:
            selected_result = complete_result
            retained_last_known_good = True
        entry, revision, source = selected_result
        plan = entry.normalized_plan if isinstance(entry.normalized_plan, dict) else {}
        raw_periods = plan.get("periods")
        periods: list[object] = raw_periods if isinstance(raw_periods, list) else []
        seasons = sorted(
            {
                str(period.get("season"))
                for period in periods
                if isinstance(period, dict) and period.get("season") is not None
            }
        )
        metadata = plan.get("catalog_metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        effective_start = metadata.get("effective_start")
        if isinstance(effective_start, str) and effective_start:
            effective_dates.append(effective_start)
        season_definitions = metadata.get("season_definitions")
        if not isinstance(season_definitions, dict | list):
            season_definitions = {}
        schedule: list[dict[str, object]] = []
        for period in periods:
            if not isinstance(period, dict):
                continue
            start_minute = period.get("start_minute")
            end_minute = period.get("end_minute")
            if not isinstance(start_minute, int) or isinstance(start_minute, bool):
                start_minute = None
            if not isinstance(end_minute, int) or isinstance(end_minute, bool):
                end_minute = None

            def local_time(value: int | None) -> str | None:
                if value is None or value < 0 or value > 1440:
                    return None
                if value == 1440:
                    return "24:00"
                return f"{value // 60:02d}:{value % 60:02d}"

            raw_components = period.get("rate_components", plan.get("rate_components"))
            components: list[dict[str, object] | str]
            if isinstance(raw_components, list):
                components = [
                    component for component in raw_components if isinstance(component, dict | str)
                ]
            elif isinstance(raw_components, str) and raw_components:
                components = [
                    {
                        "component": raw_components,
                        "amount_per_kwh": None,
                        "source_status": "combined_only",
                    }
                ]
            else:
                components = []
            schedule.append(
                {
                    "season": period.get("season"),
                    "day_type": period.get("day_type"),
                    "period_name": period.get("name"),
                    "start_minute": start_minute,
                    "end_minute": end_minute,
                    "local_start_time": local_time(start_minute),
                    "local_end_time": local_time(end_minute),
                    "price_per_kwh": period.get("price_per_kwh"),
                    "currency": period.get("currency", metadata.get("currency", "USD")),
                    "energy_unit": period.get("unit", "kWh"),
                    "rate_components": components,
                    "tier": {
                        "lower_bound_kwh": period.get("tier_min_kwh"),
                        "upper_bound_kwh": period.get("tier_max_kwh"),
                        "boundary_inclusive": period.get("boundary_inclusive", True),
                        "threshold_basis": period.get(
                            "threshold_basis", plan.get("tier_threshold_basis")
                        ),
                        "threshold_value": period.get("threshold_value"),
                    },
                    "source_label": period.get("source_label"),
                }
            )
        entries.append(
            {
                "id": entry.id,
                "canonical_name": entry.canonical_name,
                "public_plan_name": entry.public_plan_name,
                "official_schedule_code": entry.official_schedule_code,
                "plan_type": entry.plan_type,
                "enrollment_status": entry.enrollment_status,
                "eligibility": entry.eligibility,
                "eligibility_requirements": [
                    {
                        "requirement": requirement,
                        "verification": "home_confirmation_required",
                    }
                    for requirement in entry.eligibility
                ],
                "description": plan.get("description"),
                "effective_start": effective_start,
                "effective_end": metadata.get("effective_end"),
                "timezone": metadata.get("timezone", "America/Los_Angeles"),
                "currency": metadata.get("currency", "USD"),
                "energy_unit": "kWh",
                "source_version": revision.artifact_sha256,
                "holiday_treatment": metadata.get("holiday_treatment", "unresolved"),
                "season_definitions": season_definitions,
                "seasons": seasons,
                "day_types": sorted(
                    {
                        str(period.get("day_type"))
                        for period in periods
                        if isinstance(period, dict) and period.get("day_type") is not None
                    }
                ),
                "period_count": len(periods),
                "periods": schedule,
                "schedule": schedule,
                "daily_fixed_charge": plan.get("daily_fixed_charge"),
                "monthly_fixed_charge": plan.get("monthly_fixed_charge"),
                "minimum_charge": plan.get("minimum_charge"),
                "meter_charge": plan.get("meter_charge"),
                "other_fixed_charge": plan.get("other_fixed_charge"),
                "baseline_credit_per_kwh": plan.get("baseline_credit_per_kwh"),
                "tier_threshold_basis": plan.get("tier_threshold_basis"),
                "rate_precision": plan.get("rate_precision", "unverified"),
                "exact_rates_verified": plan.get("rate_precision") == "approved_tariff_exact",
                "rate_component_scope": plan.get("rate_components"),
                "baseline_credit_scope": plan.get("baseline_credit_scope"),
                "verification_state": entry.discovery_state,
                "latest_discovery_state": latest_entry.discovery_state,
                "latest_discovery_revision_id": latest_revision.id,
                "last_known_good_retained": retained_last_known_good,
                "exclusion_reason": entry.exclusion_reason,
                "currently_used": entry.canonical_name in active_names,
                "source": {
                    "level": entry.source_level,
                    "name": source.name,
                    "url": entry.source_url,
                    "revision_id": revision.id,
                    "artifact_sha256": revision.artifact_sha256,
                    "retrieved_at": revision.retrieved_at,
                    "parser_version": revision.parser_version,
                },
            }
        )
    entries.sort(key=lambda item: str(item["public_plan_name"]).casefold())
    raw_manifest_counts = latest_manifest.get("counts") if latest_manifest is not None else None
    manifest_counts = raw_manifest_counts if isinstance(raw_manifest_counts, dict) else {}

    def latest_health_count(key: str, fallback: int) -> int:
        value = manifest_counts.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else fallback

    return {
        "home_id": scoped_home_id,
        "summary": {
            "plans_discovered": latest_health_count(
                "plans_discovered",
                len(latest_snapshot_by_name) or len(entries),
            ),
            "plans_parsed": latest_health_count(
                "plans_parsed",
                sum(
                    item[0].discovery_state == "parsed" for item in latest_snapshot_by_name.values()
                ),
            ),
            "plans_requiring_parser_updates": latest_health_count(
                "plans_requiring_parser_updates",
                sum(
                    item[0].discovery_state == "requires_parser"
                    for item in latest_snapshot_by_name.values()
                ),
            ),
            "plans_explicitly_excluded": latest_health_count(
                "plans_explicitly_excluded",
                sum(
                    item[0].discovery_state == "excluded"
                    for item in latest_snapshot_by_name.values()
                ),
            ),
            "plans_silently_omitted": plans_silently_omitted,
            "last_successful_official_check": last_success,
            "current_catalog_effective_date": max(effective_dates) if effective_dates else None,
            "open_plans": sum(
                item[0].enrollment_status == "open_or_eligibility_required"
                for item in latest_by_name.values()
            ),
            "eligibility_required_plans": sum(
                bool(item[0].eligibility) for item in latest_by_name.values()
            ),
            "existing_customer_only_plans": sum(
                item[0].enrollment_status == "existing_customers_only"
                for item in latest_by_name.values()
            ),
        },
        "plans": entries,
        "source_policy": "official_public_sce_only",
        "inventory_scope": "bounded_official_multi_document_crawl",
        "catalog_completeness": "closure_proved" if catalog_ready else "crawl_incomplete",
        "catalog_ready": catalog_ready,
        "completeness_reason": completeness_reason,
        "live_source_access_performed": False,
    }


@router.get("/rate-sources/runs")
async def list_rate_source_runs(
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("rates.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    runs = (
        await session.execute(
            select(RateSyncRun, RateSource)
            .join(RateSource, RateSource.id == RateSyncRun.source_id)
            .where(RateSyncRun.home_id == scoped_home_id)
            .order_by(RateSyncRun.started_at.desc(), RateSyncRun.id.desc())
            .limit(100)
        )
    ).all()
    return {
        "home_id": scoped_home_id,
        "runs": [
            {
                "id": run.id,
                "source_id": run.source_id,
                "source_name": source.name,
                "source_type": source.source_type,
                "state": run.state,
                "event_code": run.event_code,
                "correlation_id": run.correlation_id,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "requested_url": run.requested_url,
                "final_url": run.final_url,
                "http_status": run.http_status,
                "response_bytes": run.response_bytes,
                "revision_id": run.revision_id,
                "error_code": run.error_code,
                "evidence": run.evidence,
            }
            for run, source in runs
        ],
    }


def _safe_rate_run(
    run: RateSyncRun | None,
    source: RateSource | None,
) -> dict[str, object] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "source_id": run.source_id,
        "source_name": source.name if source is not None else None,
        "source_type": source.source_type if source is not None else None,
        "source_url": source.https_url if source is not None else None,
        "state": run.state,
        "event_code": run.event_code,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "revision_id": run.revision_id,
        "error_code": run.error_code,
        "initiator": run.evidence.get("initiator"),
    }


@router.get("/rate-sources/status")
async def rate_source_status(
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("rates.view")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    official_source = await session.scalar(
        select(RateSource).where(RateSource.https_url == str(settings.sce_rate_source_url))
    )
    home_runs = select(RateSyncRun).where(
        RateSyncRun.home_id == scoped_home_id,
        RateSyncRun.source_id
        == (
            official_source.id if official_source is not None else "official-source-not-configured"
        ),
    )
    last_run = await session.scalar(
        home_runs.order_by(RateSyncRun.started_at.desc(), RateSyncRun.id.desc()).limit(1)
    )
    last_success = await session.scalar(
        home_runs.where(
            RateSyncRun.completed_at.is_not(None),
            RateSyncRun.state.in_(("review_required", "unchanged")),
        )
        .order_by(RateSyncRun.completed_at.desc(), RateSyncRun.id.desc())
        .limit(1)
    )
    last_failure = await session.scalar(
        home_runs.where(RateSyncRun.state == "failed")
        .order_by(RateSyncRun.completed_at.desc(), RateSyncRun.id.desc())
        .limit(1)
    )
    now = datetime.now(UTC)
    active_row = (
        await session.execute(
            select(RateAssignment, RatePlanVersion, RatePlan, UtilityAccount)
            .join(
                RatePlanVersion,
                RatePlanVersion.id == RateAssignment.rate_plan_version_id,
            )
            .join(RatePlan, RatePlan.id == RatePlanVersion.rate_plan_id)
            .join(
                UtilityAccount,
                UtilityAccount.id == RateAssignment.utility_account_id,
            )
            .where(
                UtilityAccount.home_id == scoped_home_id,
                RateAssignment.effective_start <= now,
                (RateAssignment.effective_end.is_(None) | (RateAssignment.effective_end > now)),
                RatePlanVersion.state == "published",
            )
            .order_by(RateAssignment.effective_start.desc(), RateAssignment.id.desc())
            .limit(1)
        )
    ).first()
    active: dict[str, object] = {"state": "not_configured"}
    if active_row is not None:
        assignment, version, plan, account = active_row
        provenance_row = (
            await session.execute(
                select(
                    RateCandidateReview,
                    RateCandidate,
                    RateSourceRevision,
                    RateSource,
                )
                .join(
                    RateCandidate,
                    RateCandidate.id == RateCandidateReview.candidate_id,
                )
                .join(
                    RateSourceRevision,
                    RateSourceRevision.id == RateCandidate.source_revision_id,
                )
                .join(RateSource, RateSource.id == RateSourceRevision.source_id)
                .where(
                    RateCandidateReview.home_id == scoped_home_id,
                    RateCandidateReview.rate_plan_version_id == version.id,
                )
                .limit(1)
            )
        ).first()
        provenance: dict[str, object] = {
            "source_artifact_sha256": version.source_hash,
            "origin": "reviewed_rate_plan_version",
        }
        if provenance_row is not None:
            review, candidate, revision, source = provenance_row
            provenance = {
                "source_artifact_sha256": revision.artifact_sha256,
                "origin": source.source_type,
                "source_name": source.name,
                "source_url": source.https_url or candidate.validation_evidence.get("source_url"),
                "source_revision_id": revision.id,
                "candidate_id": candidate.id,
                "review_id": review.id,
            }
        active = {
            "state": "active",
            "utility_account_id": account.id,
            "assignment_id": assignment.id,
            "rate_plan_version_id": version.id,
            "plan_name": plan.name,
            "effective_start": version.effective_start,
            "effective_end": version.effective_end,
            "provenance": provenance,
        }
    lkg_row = (
        await session.execute(
            select(RateCandidate, RateSourceRevision, RateSource)
            .join(
                RateSourceRevision,
                RateSourceRevision.id == RateCandidate.source_revision_id,
            )
            .join(RateSource, RateSource.id == RateSourceRevision.source_id)
            .where(
                select(RateSyncRun.id)
                .where(
                    RateSyncRun.home_id == scoped_home_id,
                    RateSyncRun.revision_id == RateSourceRevision.id,
                    RateSyncRun.state.in_(("review_required", "unchanged")),
                )
                .exists()
            )
            .order_by(RateSourceRevision.retrieved_at.desc(), RateSourceRevision.id.desc())
            .limit(1)
        )
    ).first()
    lkg: dict[str, object] = {"state": "unavailable"}
    if lkg_row is not None:
        candidate, revision, source = lkg_row
        active_provenance = active.get("provenance")
        active_source_hash = (
            active_provenance.get("source_artifact_sha256")
            if isinstance(active_provenance, dict)
            else None
        )
        lkg = {
            "state": "available",
            "candidate_id": candidate.id,
            "source_revision_id": revision.id,
            "source_artifact_sha256": revision.artifact_sha256,
            "retrieved_at": revision.retrieved_at,
            "source_name": source.name,
            "source_type": source.source_type,
            "source_url": source.https_url or candidate.validation_evidence.get("source_url"),
            "active_source_match": active_source_hash == revision.artifact_sha256,
        }
    scheduled: dict[str, object] = {"state": "not_configured"}
    if official_source is not None:
        next_check_at = (
            official_source.last_checked_at + timedelta(hours=official_source.check_interval_hours)
            if official_source.last_checked_at is not None
            else None
        )
        scheduled = {
            "state": "enabled" if official_source.enabled else "disabled",
            "source_id": official_source.id,
            "source_name": official_source.name,
            "source_url": official_source.https_url,
            "check_interval_hours": official_source.check_interval_hours,
            "next_check_at": next_check_at,
        }
    return {
        "home_id": scoped_home_id,
        "scheduled": scheduled,
        "last_run": _safe_rate_run(last_run, official_source),
        "last_success": _safe_rate_run(last_success, official_source),
        "last_failure": _safe_rate_run(last_failure, official_source),
        "active": active,
        "last_known_good": lkg,
    }


@router.post("/rate-sources/manual-candidates", status_code=201)
async def create_manual_rate_source_candidate(
    payload: ManualRateCandidateRequest,
    request: Request,
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("rates.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    candidate, revision, source, run, created = await create_manual_rate_candidate(
        session,
        payload=payload,
        home_id=scoped_home_id,
        actor_user_id=user.id,
        correlation_id=request.state.correlation_id,
    )
    await session.commit()
    return {
        "home_id": scoped_home_id,
        "created": created,
        "candidate_id": candidate.id,
        "revision_id": revision.id,
        "source_id": source.id,
        "run_id": run.id,
        "state": "review_required",
        "canonical_input_sha256": revision.artifact_sha256,
        "network_fetch_performed": False,
    }


@router.post("/rate-sources/candidates/{candidate_id}/review")
async def review_official_rate_candidate(
    candidate_id: str,
    payload: RateCandidateReviewRequest,
    request: Request,
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("rates.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    candidate, _, _ = await exact_home_candidate(
        session,
        candidate_id=candidate_id,
        home_id=scoped_home_id,
        for_update=True,
    )
    review = await review_rate_candidate(
        session,
        candidate=candidate,
        home_id=scoped_home_id,
        payload=payload,
        actor_user_id=user.id,
        correlation_id=request.state.correlation_id,
    )
    await session.commit()
    return {
        "home_id": scoped_home_id,
        "candidate_id": candidate.id,
        "workflow": safe_review(review),
    }


@router.post("/rate-sources/candidates/{candidate_id}/reject")
async def reject_official_rate_candidate(
    candidate_id: str,
    request: Request,
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("rates.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    candidate, _, _ = await exact_home_candidate(
        session,
        candidate_id=candidate_id,
        home_id=scoped_home_id,
        for_update=True,
    )
    review = await reject_rate_candidate(
        session,
        candidate=candidate,
        home_id=scoped_home_id,
        actor_user_id=user.id,
        correlation_id=request.state.correlation_id,
    )
    await session.commit()
    return {
        "home_id": scoped_home_id,
        "candidate_id": candidate.id,
        "workflow": safe_review(review),
    }


@router.delete("/rate-sources/candidates/{candidate_id}", status_code=204)
async def delete_rate_source_candidate(
    candidate_id: str,
    request: Request,
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("rates.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    candidate, revision, _source = await exact_home_candidate(
        session,
        candidate_id=candidate_id,
        home_id=scoped_home_id,
        for_update=True,
    )
    reviews = (
        await session.scalars(
            select(RateCandidateReview)
            .where(RateCandidateReview.candidate_id == candidate.id)
            .with_for_update()
        )
    ).all()
    if any(review.state != "rejected" for review in reviews):
        raise RateWorkflowConflict(
            "reviewed, published, or activated candidates must be retained as rate provenance"
        )
    if any(review.home_id != scoped_home_id for review in reviews):
        raise RateWorkflowConflict("candidate is shared with another home and cannot be deleted")
    other_home_reference = await session.scalar(
        select(RateSyncRun.id)
        .where(
            RateSyncRun.revision_id == revision.id,
            RateSyncRun.home_id.is_not(None),
            RateSyncRun.home_id != scoped_home_id,
        )
        .limit(1)
    )
    if other_home_reference is not None:
        raise RateWorkflowConflict("candidate is shared with another home and cannot be deleted")
    for review in reviews:
        await session.delete(review)
    await session.flush()
    await session.delete(candidate)
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="RATE_CANDIDATE_DELETED",
            target_type="rate_candidate",
            target_id=candidate.id,
            correlation_id=request.state.correlation_id,
            details={
                "home_id": scoped_home_id,
                "source_artifact_sha256": revision.artifact_sha256,
                "published_rate_provenance_deleted": False,
            },
        )
    )
    await session.commit()


@router.post("/rate-sources/candidates/{candidate_id}/publish", status_code=201)
async def publish_official_rate_candidate(
    candidate_id: str,
    request: Request,
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("rates.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    candidate, _, _ = await exact_home_candidate(
        session,
        candidate_id=candidate_id,
        home_id=scoped_home_id,
        for_update=True,
    )
    review = await session.scalar(
        select(RateCandidateReview)
        .where(
            RateCandidateReview.candidate_id == candidate.id,
            RateCandidateReview.home_id == scoped_home_id,
        )
        .with_for_update()
    )
    if review is None:
        raise RateWorkflowConflict("candidate must be reviewed before publication")
    plan, version = await publish_rate_candidate(
        session,
        candidate=candidate,
        review=review,
        actor_user_id=user.id,
        correlation_id=request.state.correlation_id,
    )
    await session.commit()
    return {
        "home_id": scoped_home_id,
        "candidate_id": candidate.id,
        "workflow": safe_review(review),
        "rate_plan_version": {
            "id": version.id,
            "plan_id": plan.id,
            "plan_name": plan.name,
            "version": version.version,
            "effective_start": version.effective_start,
            "effective_end": version.effective_end,
            "source_artifact_sha256": version.source_hash,
            "state": version.state,
        },
    }


@router.post("/rate-sources/candidates/{candidate_id}/activate", status_code=201)
async def activate_official_rate_candidate(
    candidate_id: str,
    payload: RateCandidateActivationRequest,
    request: Request,
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("rates.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    candidate, _, _ = await exact_home_candidate(
        session,
        candidate_id=candidate_id,
        home_id=scoped_home_id,
        for_update=True,
    )
    review = await session.scalar(
        select(RateCandidateReview)
        .where(
            RateCandidateReview.candidate_id == candidate.id,
            RateCandidateReview.home_id == scoped_home_id,
        )
        .with_for_update()
    )
    if review is None:
        raise RateWorkflowConflict("candidate must be published before activation")
    assignment = await activate_rate_candidate(
        session,
        candidate=candidate,
        review=review,
        utility_account_id=payload.utility_account_id,
        actor_user_id=user.id,
        correlation_id=request.state.correlation_id,
    )
    await session.commit()
    return {
        "home_id": scoped_home_id,
        "candidate_id": candidate.id,
        "workflow": safe_review(review),
        "assignment": {
            "id": assignment.id,
            "utility_account_id": assignment.utility_account_id,
            "rate_plan_version_id": assignment.rate_plan_version_id,
            "effective_start": assignment.effective_start,
            "effective_end": assignment.effective_end,
        },
    }


@router.post("/rate-sources/check-now", status_code=202)
async def check_rate_sources_now(
    request: Request,
    payload: RateSourceCheckRequest | None = None,
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("rates.sync")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    source_url = (
        payload.source_url if payload and payload.source_url else str(settings.sce_rate_source_url)
    )
    source = (
        await ensure_default_sce_catalog_source(session)
        if source_url.rstrip("/") == SCE_CATALOG_URL
        else await ensure_default_sce_source(session, source_url)
    )
    result = await sync_official_rate_source(
        session,
        settings,
        source,
        home_id=scoped_home_id,
        actor_user_id=user.id,
        correlation_id=request.state.correlation_id,
    )
    await session.commit()
    return {
        "run_id": result.run_id,
        "state": result.state,
        "event_code": result.event_code,
        "revision_id": result.revision_id,
        "candidate_id": result.candidate_id,
        "error_code": result.error_code,
    }
