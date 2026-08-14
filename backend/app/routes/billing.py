from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from anyio import Path as AsyncPath
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..bill_rate_import.isolated import (
    extract_rate_plan_isolated,
    extract_rate_plan_portable_for_tests,
)
from ..config import Settings, get_settings
from ..constants import MAX_PDF_BYTES
from ..db import get_session
from ..errors import BillRateImportError, InvalidRequest, NotFound
from ..models import (
    AuditEvent,
    BillingEstimate,
    BillingEstimateSelection,
    Device,
    IntervalCostSelection,
    NormalizedInterval,
    RateAssignment,
    RateCandidate,
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
from ..schemas.api import RateCorrectionRequest, RatePublishRequest
from ..security.auth import CurrentUser, require_permission
from ..security.crypto import encrypt_secret
from ..services.rate_sync import ensure_default_sce_source, sync_official_rate_source

router = APIRouter(prefix="/api/v1", tags=["billing"])
# Compatibility seam for existing API tests that replace the parser with a sanitized
# fixture. It is reached only under PM_ENV=test; production always calls the sandbox.
extract_rate_plan_from_pdf = extract_rate_plan_portable_for_tests


async def _user_homes(session: AsyncSession, user_id: str) -> tuple[str, ...]:
    return tuple(
        (
            await session.scalars(
                select(user_home_scopes.c.home_id).where(user_home_scopes.c.user_id == user_id)
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
    if len(homes) != 1:
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
        "cca_or_direct_access_indicator": row.cca_or_direct_access_indicator,
        "season_definitions": row.season_definitions,
        "day_type_definitions": row.day_type_definitions,
        "tou_period_definitions": row.tou_period_definitions,
        "tier_threshold_definitions": row.tier_threshold_definitions,
        "reusable_price_components": row.reusable_price_components,
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
    upload = UtilityBillRateUpload(
        home_id=scoped_home_id,
        artifact_sha256=draft.source_artifact_sha256,
        encrypted_artifact_path=None,
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
    if settings.retain_bill_artifacts:
        settings.bill_artifact_dir.mkdir(parents=True, exist_ok=True)
        encrypted = encrypt_secret(settings.master_key, data, context=upload.id.encode())
        target = settings.bill_artifact_dir / f"{upload.id}.pdf.enc"
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(encrypted)
        os.replace(temporary, target)
        upload.encrypted_artifact_path = str(target)
    extraction = UtilityBillRateExtraction(
        upload_id=upload.id,
        utility_name=draft.utility_name,
        rate_plan_name=draft.rate_plan_name,
        rate_class=draft.rate_class,
        cca_or_direct_access_indicator=draft.cca_or_direct_access_indicator,
        season_definitions=[
            {"name": "summer", "months": list(draft.summer_months)},
            {"name": "winter", "months": list(draft.winter_months)},
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
        reusable_price_components=[
            charge.model_dump(mode="json") for charge in draft.reusable_charges
        ],
        baseline_allocation_rule=draft.baseline_allocation_rule,
        baseline_credit_rate=draft.baseline_credit_rate,
        effective_start_candidate=draft.effective_start_candidate,
        effective_end_candidate=draft.effective_end_candidate,
        source_evidence=[field.model_dump(mode="json") for field in draft.fields],
        parser_version=draft.parser_version,
        state="review_required",
    )
    session.add(extraction)
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
                "retained_encrypted": bool(upload.encrypted_artifact_path),
            },
        )
    )
    await session.commit()
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
    user: CurrentUser = Depends(require_permission("rates.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    actor_homes = select(user_home_scopes.c.home_id).where(user_home_scopes.c.user_id == user.id)
    rows = (
        await session.execute(
            select(UtilityBillRateExtraction, UtilityBillRateUpload)
            .join(
                UtilityBillRateUpload,
                UtilityBillRateUpload.id == UtilityBillRateExtraction.upload_id,
            )
            .where(UtilityBillRateUpload.home_id.in_(actor_homes))
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
    plan = await session.scalar(
        select(RatePlan).where(
            RatePlan.name == extraction.rate_plan_name,
            RatePlan.utility_name == extraction.utility_name,
            RatePlan.rate_class == extraction.rate_class,
        )
    )
    if plan is None:
        plan = RatePlan(
            name=extraction.rate_plan_name,
            utility_name=extraction.utility_name,
            rate_class=extraction.rate_class,
        )
        session.add(plan)
        await session.flush()
    version_number = (
        int(
            await session.scalar(
                select(func.max(RatePlanVersion.version)).where(
                    RatePlanVersion.rate_plan_id == plan.id
                )
            )
            or 0
        )
        + 1
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
        pricing_model="time_of_use",
        daily_fixed_charge=daily,
        monthly_fixed_charge=monthly,
        baseline_credit_per_kwh=extraction.baseline_credit_rate or Decimal("0"),
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
        session.add(
            RateAssignment(
                utility_account_id=account.id,
                rate_plan_version_id=version.id,
                effective_start=version.effective_start,
                effective_end=version.effective_end,
                assigned_by_user_id=user.id,
            )
        )
        # Selected-cost rows are mutable pointers into immutable cost evidence.
        # Invalidate only pointers in the new assignment's home/effective range;
        # the worker will create a new CostRun/IntervalCost and atomically select
        # it without deleting the prior calculation.
        affected_conditions = [
            Device.home_id == account.home_id,
            NormalizedInterval.start_utc >= version.effective_start,
        ]
        if version.effective_end is not None:
            affected_conditions.append(NormalizedInterval.end_utc <= version.effective_end)
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
async def reject_bill_rate_import(
    extraction_id: str,
    user: CurrentUser = Depends(require_permission("rates.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    extraction, upload = await _scoped_extraction(
        session,
        user_id=user.id,
        extraction_id=extraction_id,
        for_update=True,
    )
    extraction.state = "rejected"
    extraction.reviewer_user_id = user.id
    extraction.reviewed_at = datetime.now(UTC)
    if upload.encrypted_artifact_path:
        target = AsyncPath(upload.encrypted_artifact_path)
        if await target.exists():
            await target.unlink()
        upload.encrypted_artifact_path = None
        upload.artifact_deleted_at = datetime.now(UTC)
    await session.commit()


@router.get("/billing")
async def billing_overview(
    user: CurrentUser = Depends(require_permission("billing.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    homes = await _user_homes(session, user.id)
    accounts = (
        await session.scalars(select(UtilityAccount).where(UtilityAccount.home_id.in_(homes)))
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
            .order_by(RateCandidate.created_at.desc(), RateCandidate.id.desc())
            .limit(100)
        )
    ).all()
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
        await session.scalars(
            select(RateSyncRun)
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
            for run in runs
        ],
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
    source = await ensure_default_sce_source(session)
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
