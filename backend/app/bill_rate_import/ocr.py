from __future__ import annotations

import io
from typing import cast

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytesseract  # type: ignore[import-untyped]
from PIL import Image

from ..errors import BillRateImportError


def local_tesseract_ocr(pdf_data: bytes, page_number: int) -> str:
    """Render one PDF page locally and OCR it without a network service."""

    try:
        document = pdfium.PdfDocument(pdf_data)
        page = document[page_number - 1]
        bitmap = page.render(scale=2.5, rotation=0)
        image: Image.Image = bitmap.to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        rendered = Image.open(io.BytesIO(buffer.getvalue()))
        text = pytesseract.image_to_string(rendered, lang="eng", timeout=12)
    except RuntimeError as exc:
        raise BillRateImportError("local OCR timed out or failed") from exc
    except Exception as exc:
        raise BillRateImportError("local OCR could not render the document") from exc
    finally:
        if "document" in locals():
            document.close()
    if len(text.strip()) < 30:
        raise BillRateImportError("local OCR did not find reusable rate details")
    return cast(str, text)
