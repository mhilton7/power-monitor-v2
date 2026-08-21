from __future__ import annotations

import io
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from backend.app.bill_rate_import.parser import extract_rate_plan_from_pdf
from backend.app.errors import BillRateImportError
from backend.app.main import session_factory
from backend.app.models import (
    RateCandidate,
    RatePeriod,
    RatePlanVersion,
    RateSourceRevision,
    RawReading,
    UtilityAccount,
    UtilityAccountTierThreshold,
    UtilityBillRateExtraction,
    UtilityBillRateUpload,
)
from backend.app.schemas.billing import RatePlanDraft
from backend.app.services.cost_engine import (
    RateVersion,
    fixed_charge_microdollars,
    fixed_charges_from_storage,
)
from httpx import AsyncClient
from pydantic import ValidationError
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]
from sqlalchemy import func, select

RATE_ONLY_CHARGES_PAGE = (
    "Details of your new charges",
    "SCE rate evidence",
    "Your rate: DOMESTIC",
    "Billing period: 06/22/26 to 07/21/26 (30 days)",
    "Delivery charges- Cost to deliver your electricity",
    "Base services charge 30 days x $0.76900",
    "Energy-Summer",
    "Tier 1 (within baseline) 579 kWh x $0.17862",
    "Tier 2 (over baseline) 372 kWh x $0.27961",
    "Wildfire fund charge 951 kWh x $0.00591",
    "Generation charges- Cost to generate your electricity",
    "Energy-Summer",
    "Tier 1 (within baseline) 579 kWh x $0.11761",
    "Tier 2 (over baseline) 372 kWh x $0.11761",
    "Other charges or credits",
    "Fixed recovery charge 951 kWh x $0.00619",
    "Subtotal of your new charges $52.90",
    "State tax 140 kWh x $0.00030",
    "Your Delivery charges include:",
    "Approximate explanatory chart Tier 1 $9.99/kWh Tier 2 $8.88/kWh",
    "Your Generation charges include: values already included above",
    "Your summer baseline allowance:",
    "579.0 kWh",
    "Your Total Usage: 951 kWh",
    "Page 3 of 6",
)


def _pdf(pages: list[tuple[str, ...]]) -> bytes:
    output = io.BytesIO()
    document = Canvas(output)
    for lines in pages:
        y = 760
        for line in lines:
            document.drawString(36, y, line)
            y -= 20
        document.showPage()
    document.save()
    return output.getvalue()


def test_isolated_domestic_charges_page_extracts_only_reusable_rate_evidence() -> None:
    draft, categories = extract_rate_plan_from_pdf(_pdf([RATE_ONLY_CHARGES_PAGE]))

    assert draft.rate_plan_name == "DOMESTIC"
    assert draft.plan_classification == "seasonal_tiered"
    assert draft.holiday_treatment == "not_applicable"
    assert draft.billing_period_start == date(2026, 6, 22)
    assert draft.billing_period_end == date(2026, 7, 21)
    assert draft.billing_period_days == 30
    assert draft.candidate_complete is True
    assert draft.verified_seasons == ("summer",)
    assert draft.tier_threshold_rule is not None
    assert draft.tier_threshold_rule.source_allowance_kwh == Decimal("579.0")
    assert draft.tier_threshold_rule.source_billing_days == 30
    assert draft.tier_threshold_rule.kwh_per_day == Decimal("19.3")
    assert draft.tier_threshold_rule.tier1_boundary_inclusive is True
    assert [period.price_per_kwh for period in draft.periods] == [
        Decimal("0.30863000"),
        Decimal("0.40962000"),
    ]
    assert [period.delivery_per_kwh for period in draft.periods] == [
        Decimal("0.17862000"),
        Decimal("0.27961000"),
    ]
    assert [period.generation_per_kwh for period in draft.periods] == [
        Decimal("0.11761000"),
        Decimal("0.11761000"),
    ]
    assert {charge.name: charge.amount for charge in draft.reusable_charges} == {
        "Base Services Charge": Decimal("0.76900000"),
        "Wildfire fund charge": Decimal("0.00591000"),
        "Fixed recovery charge": Decimal("0.00619000"),
        "State tax": Decimal("0.00030000"),
    }
    # The source digest is opaque hexadecimal evidence and can legitimately
    # contain decimal-looking substrings such as the bill's allowance value.
    serialized = draft.model_dump_json(exclude={"source_artifact_sha256"}).lower()
    assert draft.periods[0].tier_start_kwh == 0
    assert draft.periods[0].tier_end_kwh == Decimal("579.0")
    assert draft.periods[1].tier_start_kwh == Decimal("579.0")
    assert draft.periods[1].tier_end_kwh is None
    assert "579" in serialized
    assert "9.99" not in serialized
    assert "8.88" not in serialized
    assert "amount due" not in serialized
    assert categories == ()


def test_tier_threshold_cannot_claim_an_unverified_season() -> None:
    draft, _categories = extract_rate_plan_from_pdf(_pdf([RATE_ONLY_CHARGES_PAGE]))
    payload = draft.model_dump()
    payload["tier_threshold_rule"]["season"] = "winter"

    with pytest.raises(ValidationError, match="threshold season"):
        RatePlanDraft.model_validate(payload)


def test_complete_statement_selects_the_charges_page_and_preserves_page_lineage() -> None:
    draft, _categories = extract_rate_plan_from_pdf(
        _pdf(
            [
                ("SCE statement cover", "Account summary intentionally omitted from parsing"),
                ("Service messages", "No reusable rate table on this page"),
                RATE_ONLY_CHARGES_PAGE,
                ("Terms and conditions", "No reusable rate table on this page"),
            ]
        )
    )
    assert {field.source.page for field in draft.fields} == {3}


@pytest.mark.asyncio
async def test_bill_dates_are_metadata_and_complete_summer_threshold_is_publishable(
    owner_client: AsyncClient,
) -> None:
    uploaded = await owner_client.post(
        "/api/v1/bill-rate-imports",
        files={"document": ("summer-rates.pdf", _pdf([RATE_ONLY_CHARGES_PAGE]), "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    extraction = uploaded.json()["extraction"]
    assert extraction["publication_scope"] == "complete_schedule"
    assert extraction["publishable_effective_start"] is None
    assert extraction["publishable_effective_end"] is None
    assert extraction["billing_period_start"] == "2026-06-22"
    assert extraction["tier_threshold_basis"] == "bill_baseline_allowance"
    assert extraction["tier_threshold_rule"] == {
        "rule_type": "daily_allowance",
        "season": "summer",
        "kwh_per_day": "19.3",
        "source_allowance_kwh": "579.0",
        "source_billing_days": 30,
        "tier1_boundary_inclusive": True,
    }

    async with session_factory() as session:
        account_id = await session.scalar(select(UtilityAccount.id))
    assert account_id is not None
    published = await owner_client.post(
        f"/api/v1/bill-rate-imports/{extraction['id']}/publish",
        json={
            "effective_start": "2026-06-22T07:00:00Z",
            "effective_end": None,
            "administrator_confirmed_effective_date": True,
            "assign_to_utility_account_id": account_id,
        },
    )
    assert published.status_code == 201, published.text
    async with session_factory() as session:
        version = await session.get(RatePlanVersion, published.json()["rate_plan_version"]["id"])
        assert version is not None
        assert version.tier_threshold_kwh_per_day is None
        assert version.tier_threshold_source_kwh is None
        assert version.tier_threshold_source_days is None
        threshold = await session.scalar(
            select(UtilityAccountTierThreshold).where(
                UtilityAccountTierThreshold.utility_account_id == account_id,
                UtilityAccountTierThreshold.rate_plan_id == version.rate_plan_id,
            )
        )
        assert threshold is not None
        assert threshold.kwh_per_day == Decimal("19.30000000")
        assert threshold.source_allowance_kwh == Decimal("579.00000000")
        periods = (
            await session.scalars(
                select(RatePeriod)
                .where(RatePeriod.rate_plan_version_id == version.id)
                .order_by(RatePeriod.tier_start_kwh)
            )
        ).all()
        assert [(period.tier_start_kwh, period.tier_end_kwh) for period in periods] == [
            (Decimal("0"), Decimal("1")),
            (Decimal("1"), None),
        ]


@pytest.mark.asyncio
async def test_bill_publish_normalizes_and_executes_meter_and_other_fixed_charges(
    owner_client: AsyncClient,
) -> None:
    uploaded = await owner_client.post(
        "/api/v1/bill-rate-imports",
        files={"document": ("fixed-rates.pdf", _pdf([RATE_ONLY_CHARGES_PAGE]), "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    extraction_id = uploaded.json()["extraction"]["id"]
    async with session_factory() as session:
        extraction = await session.get(UtilityBillRateExtraction, extraction_id)
        account_id = await session.scalar(select(UtilityAccount.id))
        assert extraction is not None and account_id is not None
        extraction.reusable_price_components = [
            *extraction.reusable_price_components,
            {
                "name": "Utility meter charge",
                "kind": "meter_fixed",
                "amount": "2.00",
                "unit": "USD/month",
            },
            {
                "name": "Account fixed adjustment",
                "kind": "other_fixed",
                "amount": "1.25",
                "unit": "USD/month",
                "applies": "per_account_per_cycle",
            },
        ]
        await session.commit()
    published = await owner_client.post(
        f"/api/v1/bill-rate-imports/{extraction_id}/publish",
        json={
            "effective_start": "2026-06-22T07:00:00Z",
            "effective_end": None,
            "administrator_confirmed_effective_date": True,
            "assign_to_utility_account_id": account_id,
        },
    )
    assert published.status_code == 201, published.text
    async with session_factory() as session:
        stored = await session.get(
            RatePlanVersion,
            published.json()["rate_plan_version"]["id"],
        )
        assert stored is not None
        assert stored.meter_charge == Decimal("2")
        assert stored.other_fixed_charge == Decimal("1.25")
        assert {item["charge"] for item in stored.fixed_charges} >= {
            "daily_fixed_charge",
            "meter_charge",
            "other_fixed_charge",
        }
        assert not {item.get("kind") for item in stored.fixed_charges} & {
            "meter_fixed",
            "other_fixed",
        }
        domain = RateVersion(
            id=stored.id,
            timezone=stored.timezone,
            effective_start=datetime(2026, 6, 22, 7, tzinfo=UTC),
            effective_end=None,
            periods=(),
            fixed_charges=fixed_charges_from_storage(stored.fixed_charges),
        )
        assert (
            fixed_charge_microdollars(
                domain,
                date(2026, 7, 1),
                date(2026, 8, 1),
                scope="full_account",
                meter_count=1,
            )
            == 27_089_000
        )


@pytest.mark.parametrize(
    "replacement",
    [
        None,
        "Billing period: not a usable date range",
        "Billing period: 06/22/24 to 07/21/24 (30 days)",
        "Billing period: 06/22/36 to 07/21/36 (30 days)",
    ],
)
def test_billing_dates_never_determine_extraction_success(replacement: str | None) -> None:
    page = tuple(
        replacement if line.startswith("Billing period:") and replacement is not None else line
        for line in RATE_ONLY_CHARGES_PAGE
        if replacement is not None or not line.startswith("Billing period:")
    )
    draft, _categories = extract_rate_plan_from_pdf(_pdf([page]))
    assert [period.price_per_kwh for period in draft.periods] == [
        Decimal("0.30863000"),
        Decimal("0.40962000"),
    ]
    if replacement is None or "not a usable" in replacement:
        assert draft.billing_period_start is None
        assert draft.billing_period_end is None
        assert draft.billing_period_days == 30
        assert draft.candidate_complete is True


@pytest.mark.parametrize("allowance_text", ["579 kWh", "579.0 kWh", "579.00 KWH"])
def test_baseline_allowance_decimal_spelling_normalizes_exactly(allowance_text: str) -> None:
    page = tuple(allowance_text if line == "579.0 kWh" else line for line in RATE_ONLY_CHARGES_PAGE)
    draft, _categories = extract_rate_plan_from_pdf(_pdf([page]))
    assert draft.tier_threshold_rule is not None
    assert draft.tier_threshold_rule.source_allowance_kwh == Decimal("579")
    assert draft.tier_threshold_rule.kwh_per_day == Decimal("19.3")


def test_threshold_requires_total_usage_to_reconcile_with_both_tiers() -> None:
    page = tuple(
        "Your Total Usage: 950 kWh" if line == "Your Total Usage: 951 kWh" else line
        for line in RATE_ONLY_CHARGES_PAGE
    )
    draft, _categories = extract_rate_plan_from_pdf(_pdf([page]))
    assert draft.tier_threshold_rule is None
    assert draft.candidate_complete is False


@pytest.mark.asyncio
async def test_missing_day_count_preserves_allowance_until_review_correction(
    owner_client: AsyncClient,
) -> None:
    page = tuple(
        "Base services charge $0.76900 per day" if line.startswith("Base services charge") else line
        for line in RATE_ONLY_CHARGES_PAGE
        if not line.startswith("Billing period:")
    )
    uploaded = await owner_client.post(
        "/api/v1/bill-rate-imports",
        files={"document": ("summer-no-days.pdf", _pdf([page]), "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    extraction = uploaded.json()["extraction"]
    assert extraction["candidate_complete"] is False
    assert extraction["publication_scope"] == "review_only"
    assert extraction["tier_threshold_rule"]["source_allowance_kwh"] == "579.0"
    assert extraction["tier_threshold_rule"]["source_billing_days"] is None
    assert extraction["tier_threshold_rule"]["kwh_per_day"] is None

    corrected = await owner_client.patch(
        f"/api/v1/bill-rate-imports/{extraction['id']}",
        json={"field": "billing_period_days", "corrected_value": "30"},
    )
    assert corrected.status_code == 200, corrected.text
    fixed = corrected.json()["extraction"]
    assert fixed["candidate_complete"] is True
    assert fixed["publication_scope"] == "complete_schedule"
    assert fixed["tier_threshold_rule"]["kwh_per_day"] == "19.3"
    assert fixed["tier_threshold_rule"]["source_billing_days"] == 30


def test_page_number_and_customer_period_baseline_are_not_required() -> None:
    page = tuple(
        line
        for line in RATE_ONLY_CHARGES_PAGE
        if line not in {"Page 3 of 6", "Your summer baseline allowance:", "579.0 kWh"}
    )
    draft, _categories = extract_rate_plan_from_pdf(_pdf([page]))
    assert draft.rate_plan_name == "DOMESTIC"
    assert draft.tier_threshold_basis == "review_required"
    assert draft.candidate_complete is False
    assert "579" not in draft.model_dump_json(exclude={"source_artifact_sha256"})


def test_full_utility_name_is_an_independent_sce_signal() -> None:
    page = tuple(
        "Southern California Edison" if line == "SCE rate evidence" else line
        for line in RATE_ONLY_CHARGES_PAGE
    )
    draft, _categories = extract_rate_plan_from_pdf(_pdf([page]))
    assert draft.utility_name == "Southern California Edison"


def test_exact_decimal_components_reconcile_but_bad_printed_total_fails() -> None:
    page = list(RATE_ONLY_CHARGES_PAGE)
    delivery_start = page.index("Delivery charges- Cost to deliver your electricity")
    generation_start = page.index("Generation charges- Cost to generate your electricity")
    page[delivery_start + 3] = "Tier 1 (within baseline) 579 kWh x $0.17862 = $103.42"
    page[delivery_start + 4] = "Tier 2 (over baseline) 372 kWh x $0.27961 = $104.01"
    page[delivery_start + 5] = "Wildfire fund charge 951 kWh x $0.00591 = $5.62"
    page[generation_start + 2] = "Tier 1 (within baseline) 579 kWh x $0.11761 = $68.10"
    page[generation_start + 3] = "Tier 2 (over baseline) 372 kWh x $0.11761 = $43.75"
    page[page.index("Fixed recovery charge 951 kWh x $0.00619")] = (
        "Fixed recovery charge 951 kWh x $0.00619 = $5.89"
    )
    printed_total = page.index("Subtotal of your new charges $52.90") + 1
    page.insert(printed_total, "Your new charges $354.15")
    draft, _categories = extract_rate_plan_from_pdf(_pdf([tuple(page)]))
    assert draft.periods[0].price_per_kwh == Decimal("0.30863000")
    assert draft.periods[1].price_per_kwh == Decimal("0.40962000")

    page[printed_total] = "Your new charges $999.99"
    with pytest.raises(BillRateImportError) as mismatch:
        extract_rate_plan_from_pdf(_pdf([tuple(page)]))
    assert mismatch.value.code == "RATE_RECONCILIATION_FAILED"


@pytest.mark.asyncio
async def test_semantically_identical_rates_with_different_dates_reuse_the_candidate(
    owner_client: AsyncClient,
) -> None:
    first = await owner_client.post(
        "/api/v1/bill-rate-imports",
        files={"document": ("first.pdf", _pdf([RATE_ONLY_CHARGES_PAGE]), "application/pdf")},
    )
    assert first.status_code == 201, first.text
    changed_date_page = tuple(
        "Billing period: 06/22/24 to 07/21/24 (30 days)"
        if line.startswith("Billing period:")
        else line
        for line in RATE_ONLY_CHARGES_PAGE
    )
    second = await owner_client.post(
        "/api/v1/bill-rate-imports",
        files={"document": ("later.pdf", _pdf([changed_date_page]), "application/pdf")},
    )
    assert second.status_code == 201, second.text
    assert second.json()["semantic_candidate_unchanged"] is True
    assert second.json()["extraction"]["id"] == first.json()["extraction"]["id"]
    listed = await owner_client.get("/api/v1/bill-rate-imports")
    assert len(listed.json()["extractions"]) == 1


@pytest.mark.asyncio
async def test_failed_extraction_leaves_rates_lkg_and_sensor_history_unchanged(
    owner_client: AsyncClient,
) -> None:
    async def snapshot() -> tuple[int, int, int, int, int]:
        async with session_factory() as session:
            return (
                int(await session.scalar(select(func.count(RatePlanVersion.id))) or 0),
                int(await session.scalar(select(func.count(RateSourceRevision.id))) or 0),
                int(await session.scalar(select(func.count(RateCandidate.id))) or 0),
                int(await session.scalar(select(func.count(RawReading.id))) or 0),
                int(await session.scalar(select(func.count(UtilityBillRateUpload.id))) or 0),
            )

    before = await snapshot()
    missing_base = tuple(
        line for line in RATE_ONLY_CHARGES_PAGE if not line.startswith("Base services charge")
    )
    rejected = await owner_client.post(
        "/api/v1/bill-rate-imports",
        files={"document": ("missing-base.pdf", _pdf([missing_base]), "application/pdf")},
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "BASE_CHARGE_NOT_FOUND"
    assert await snapshot() == before


def test_domestic_parser_tolerates_line_wrapping() -> None:
    wrapped = list(RATE_ONLY_CHARGES_PAGE)
    wrapped[7:9] = (
        "Tier 1 (within baseline) 100 kWh",
        "x $0.17862",
        "Tier 2 (over baseline) 40 kWh",
        "x $0.27961",
    )
    draft, _categories = extract_rate_plan_from_pdf(_pdf([tuple(wrapped)]))
    assert draft.periods[0].delivery_per_kwh == Decimal("0.17862000")
    assert draft.periods[1].delivery_per_kwh == Decimal("0.27961000")


def test_specific_document_diagnostics_are_preserved() -> None:
    with pytest.raises(BillRateImportError) as missing:
        extract_rate_plan_from_pdf(_pdf([("SCE statement summary", "No reusable rates here")]))
    assert missing.value.code == "CHARGES_PAGE_NOT_FOUND"

    not_sce = tuple(
        "Other utility" if line == "SCE rate evidence" else line for line in RATE_ONLY_CHARGES_PAGE
    )
    with pytest.raises(BillRateImportError) as utility:
        extract_rate_plan_from_pdf(_pdf([not_sce]))
    assert utility.value.code == "UTILITY_NOT_RECOGNIZED"

    source = PdfReader(io.BytesIO(_pdf([RATE_ONLY_CHARGES_PAGE])))
    writer = PdfWriter()
    writer.append_pages_from_reader(source)
    writer.encrypt("secret")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    with pytest.raises(BillRateImportError) as protected:
        extract_rate_plan_from_pdf(encrypted.getvalue())
    assert protected.value.code == "PDF_ENCRYPTED"
