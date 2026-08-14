"""Isolated utility-bill rate extraction.

This package deliberately has no imports from ingestion, History, rollups,
forecasting, calibration, or gap-repair services.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .parser import extract_rate_plan_from_pdf, extract_rate_plan_from_text

__all__ = ["extract_rate_plan_from_pdf", "extract_rate_plan_from_text"]


def __getattr__(name: str) -> object:
    # Keep package import side-effect free so the trusted launcher can establish
    # its boundary without importing a PDF parser or native parser dependency.
    if name == "extract_rate_plan_from_pdf":
        from .parser import extract_rate_plan_from_pdf

        return extract_rate_plan_from_pdf
    if name == "extract_rate_plan_from_text":
        from .parser import extract_rate_plan_from_text

        return extract_rate_plan_from_text
    raise AttributeError(name)
