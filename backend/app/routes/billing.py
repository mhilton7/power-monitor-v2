from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import delete, select
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
    AuditEvent,
    BillingEstimate,
    BillingEstimateSelection,
    Device,
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
    UtilityAccount,
    UtilityBillRateCorrection,
    UtilityBillRateExtraction,
    UtilityBillRateUpload,
    user_home_scopes,
)
from ..schemas.api import (
    ManualRateCandidateRequest,
    RateCandidateActivationRequest,
    RateCandidateReviewRequest,
    RateCorrectionRequest,
    RatePublishRequest,
)
from ..schemas.billing import RatePlanDraft, TierThresholdRuleDraft
from ..security.auth import CurrentUser, require_permission
from ..services.rate_sync import (
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
    review_rate_candidate,
    safe_review,
)

router = APIRouter(prefix="/api/v1", tags=["billing"])
# Compatibility seam for existing API tests that replace the parser with a sanitized
# fixture. It is reached only under PM_ENV=test; production always calls the sandbox.
extract_rate_plan_from_pdf = extract_rate_plan_portable_for_tests


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
    daily = Decimal("0")
    monthly = Decimal("0")
    for component in extraction.reusable_price_components:
        if component.get("kind") == "daily_fixed":
            daily += Decimal(str(component["amount"]))
        elif component.get("kind") == "monthly_fixed":
            monthly += Decimal(str(component["amount"]))
    version = RatePlanVersion(
        rate_plan_id=plan.id,
        version=version_number,
        effective_start=payload.effective_start.astimezone(UTC),
        effective_end=payload.effective_end.astimezone(UTC) if payload.effective_end else None,
        timezone="America/Los_Angeles",
        pricing_model=extraction.plan_classification,
        daily_fixed_charge=daily,
        monthly_fixed_charge=monthly,
        baseline_credit_per_kwh=extraction.baseline_credit_rate or Decimal("0"),
        tier_threshold_kwh_per_day=(
            threshold_rule.kwh_per_day if threshold_rule is not None else None
        ),
        tier_threshold_season=(threshold_rule.season if threshold_rule is not None else None),
        tier_threshold_source_kwh=(
            threshold_rule.source_allowance_kwh if threshold_rule is not None else None
        ),
        tier_threshold_source_days=(
            threshold_rule.source_billing_days if threshold_rule is not None else None
        ),
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
                tier_start_kwh=Decimal(str(period.get("tier_start_kwh", 0))),
                tier_end_kwh=Decimal(str(period["tier_end_kwh"]))
                if period.get("tier_end_kwh") is not None
                else None,
            )
        )
    # Child rows are flushed while the version is still a draft.  The ORM and
    # PostgreSQL guards reject every later mutation of a published version or
    # any of its schedule/holiday rows.
    await session.flush()
    version.state = "published"
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
            details={"source_artifact_sha256": upload.artifact_sha256},
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
        selected_rate = (
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
                .limit(1)
            )
        ).first()
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
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("rates.sync")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    scoped_home_id = await _resolve_user_home(session, user.id, home_id)
    source = await ensure_default_sce_source(session, str(settings.sce_rate_source_url))
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
