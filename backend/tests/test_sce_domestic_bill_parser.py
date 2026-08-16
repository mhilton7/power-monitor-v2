from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from backend.app.bill_rate_import.parser import extract_rate_plan_from_pdf
from backend.app.errors import BillRateImportError
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

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
    "Fixed recovery charge 140 kWh x $0.00619",
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
