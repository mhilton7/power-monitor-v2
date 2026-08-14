from __future__ import annotations

import io

import pytest
from backend.app.bill_rate_import.parser import extract_rate_plan_from_pdf
from backend.app.constants import MAX_PDF_BYTES
from backend.app.errors import BillRateImportError
from backend.tests.test_bill_rate_boundary import SCHEDULE
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]


def _pdf_pages(pages: list[list[str]], *, rotate: bool = False) -> bytes:
    output = io.BytesIO()
    canvas = Canvas(output)
    for lines in pages:
        if rotate:
            canvas.setPageRotation(90)
        y = 760
        for line in lines:
            canvas.drawString(40, y, line)
            y -= 18
        canvas.showPage()
    canvas.save()
    return output.getvalue()


def test_digital_rotated_and_multipage_pdf_has_page_evidence() -> None:
    lines = [line for line in SCHEDULE.splitlines() if line]
    data = _pdf_pages([lines[:6], lines[6:]], rotate=True)
    draft, categories = extract_rate_plan_from_pdf(data)
    assert draft.rate_plan_name == "TOU-D-4-9PM"
    assert len(draft.periods) == 13
    assert {field.source.page for field in draft.fields} == {1, 2}
    assert categories == ()


def test_scanned_pdf_uses_only_the_supplied_local_ocr_worker() -> None:
    image = Image.new("RGB", (1200, 1600), "white")
    ImageDraw.Draw(image).text((50, 50), "SCE rate schedule image", fill="black")
    output = io.BytesIO()
    canvas = Canvas(output)
    canvas.drawImage(ImageReader(image), 0, 0, width=612, height=792)
    canvas.showPage()
    canvas.save()
    calls: list[int] = []

    def ocr(_data: bytes, page_number: int) -> str:
        calls.append(page_number)
        return SCHEDULE

    draft, _categories = extract_rate_plan_from_pdf(output.getvalue(), local_ocr=ocr)
    assert draft.rate_plan_name == "TOU-D-4-9PM"
    assert calls == [1]


def test_malformed_oversized_encrypted_and_no_rate_pdf_fail_closed() -> None:
    with pytest.raises(BillRateImportError):
        extract_rate_plan_from_pdf(b"%PDF-1.7 malformed")
    with pytest.raises(BillRateImportError):
        extract_rate_plan_from_pdf(b"%PDF-" + b"0" * MAX_PDF_BYTES)

    source = PdfReader(io.BytesIO(_pdf_pages([["No reusable rates are present here."]])))
    writer = PdfWriter()
    writer.append_pages_from_reader(source)
    writer.encrypt("password")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    with pytest.raises(BillRateImportError):
        extract_rate_plan_from_pdf(encrypted.getvalue())

    totals = _pdf_pages(
        [["Customer Jane Example", "Account 123", "Total kWh 900", "Amount Due $500"]]
    )
    with pytest.raises(BillRateImportError):
        extract_rate_plan_from_pdf(totals)


def test_ocr_timeout_is_redacted_to_a_typed_import_error() -> None:
    scanned = _pdf_pages([["x"]])

    def timeout(_data: bytes, _page_number: int) -> str:
        raise TimeoutError("sensitive OCR details")

    with pytest.raises(BillRateImportError, match="local OCR timed out or failed"):
        extract_rate_plan_from_pdf(scanned, local_ocr=timeout)
