from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from backend.app.bill_rate_import.parser import extract_rate_plan_from_pdf
from backend.app.errors import BillRateImportError
from backend.app.main import session_factory
from backend.app.models import (
    RateCandidate,
    RatePlanVersion,
    RateSourceRevision,
    RawReading,
    UtilityBillRateUpload,
)
from httpx import AsyncClient
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
    "Tier 1 (within baseline) 100 kWh x $0.17862",
    "Tier 2 (over baseline) 40 kWh x $0.27961",
    "Wildfire fund charge 140 kWh x $0.00591",
    "Generation charges- Cost to generate your electricity",
    "Energy-Summer",
    "Tier 1 (within baseline) 100 kWh x $0.11761",
    "Tier 2 (over baseline) 40 kWh x $0.11761",
    "Other charges or credits",
    "Fixed recovery charge 140 kWh x $0.00619",
    "Subtotal of your new charges $52.90",
    "State tax 140 kWh x $0.00030",
    "Your Delivery charges include:",
    "Approximate explanatory chart Tier 1 $9.99/kWh Tier 2 $8.88/kWh",
    "Your Generation charges include: values already included above",
    "Your summer baseline allowance:",
    "579.0 kWh",
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
    assert draft.candidate_complete is False
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
    serialized = draft.model_dump_json().lower()
    assert all(period.tier_end_kwh is None for period in draft.periods)
    assert all(period.tier_start_kwh == 0 for period in draft.periods)
    assert "579" not in serialized
    assert "9.99" not in serialized
    assert "8.88" not in serialized
    assert "amount due" not in serialized
    assert categories == ()


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
async def test_bill_dates_are_metadata_and_incomplete_summer_rates_remain_review_only(
    owner_client: AsyncClient,
) -> None:
    uploaded = await owner_client.post(
        "/api/v1/bill-rate-imports",
        files={"document": ("summer-rates.pdf", _pdf([RATE_ONLY_CHARGES_PAGE]), "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    extraction = uploaded.json()["extraction"]
    assert extraction["publication_scope"] == "review_only"
    assert extraction["publishable_effective_start"] is None
    assert extraction["publishable_effective_end"] is None
    assert extraction["billing_period_start"] == "2026-06-22"
    assert "retain the existing configured threshold" in extraction["tier_threshold_basis"]

    rejected = await owner_client.post(
        f"/api/v1/bill-rate-imports/{extraction['id']}/publish",
        json={
            "effective_start": "2026-06-22T07:00:00Z",
            "effective_end": None,
            "administrator_confirmed_effective_date": True,
            "assign_to_utility_account_id": None,
        },
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "RATE_CANDIDATE_INCOMPLETE"


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
        assert draft.billing_period_days is None


def test_page_number_and_customer_period_baseline_are_not_required() -> None:
    page = tuple(
        line
        for line in RATE_ONLY_CHARGES_PAGE
        if line not in {"Page 3 of 6", "Your summer baseline allowance:", "579.0 kWh"}
    )
    draft, _categories = extract_rate_plan_from_pdf(_pdf([page]))
    assert draft.rate_plan_name == "DOMESTIC"
    assert draft.tier_threshold_basis is not None
    assert "579" not in draft.model_dump_json()


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
    page[page.index("Fixed recovery charge 140 kWh x $0.00619")] = (
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
