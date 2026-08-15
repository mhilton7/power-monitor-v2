from __future__ import annotations

from pathlib import Path

import pytest
from backend.app.bill_rate_import.parser import extract_rate_plan_from_text
from backend.app.config import Settings
from backend.app.main import session_factory
from backend.app.models import UtilityBillRateUpload
from backend.tests.test_bill_rate_boundary import SCHEDULE
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError


def _install_sanitized_parser(monkeypatch: pytest.MonkeyPatch, digest: str) -> None:
    draft = extract_rate_plan_from_text(SCHEDULE, digest)
    monkeypatch.setattr(
        "backend.app.routes.billing.extract_rate_plan_from_pdf",
        lambda _data: (draft, ("BILL_USAGE", "CUSTOMER_IDENTITY")),
    )


@pytest.mark.asyncio
async def test_bill_import_never_retains_the_original_document(
    owner_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sanitized_parser(monkeypatch, "e" * 64)
    document = b"%PDF-1.7 source document that must not be retained"

    response = await owner_client.post(
        "/api/v1/bill-rate-imports",
        files={"document": ("rates.pdf", document, "application/pdf")},
    )

    assert response.status_code == 201, response.text
    async with session_factory() as session:
        upload = await session.scalar(select(UtilityBillRateUpload))
        retained_path = await session.scalar(
            text(
                "SELECT encrypted_artifact_path FROM utility_bill_rate_uploads "
                "WHERE id = :upload_id"
            ),
            {"upload_id": upload.id if upload is not None else "missing"},
        )
    assert upload is not None
    assert retained_path is None
    assert "retain_bill_artifacts" not in Settings.model_fields
    assert "bill_artifact_dir" not in Settings.model_fields

    route_source = (Path(__file__).parents[1] / "app/routes/billing.py").read_text(encoding="utf-8")
    assert "encrypt_secret" not in route_source
    assert "pdf.enc" not in route_source


@pytest.mark.asyncio
async def test_database_rejects_any_original_bill_artifact_path(
    owner_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sanitized_parser(monkeypatch, "f" * 64)
    response = await owner_client.post(
        "/api/v1/bill-rate-imports",
        files={"document": ("rates.pdf", b"%PDF-1.7 rate source", "application/pdf")},
    )
    assert response.status_code == 201, response.text

    async with session_factory() as session:
        upload_id = await session.scalar(select(UtilityBillRateUpload.id))
        assert upload_id is not None
        with pytest.raises(IntegrityError, match="no_original_artifact"):
            await session.execute(
                text(
                    "UPDATE utility_bill_rate_uploads "
                    "SET encrypted_artifact_path = :path WHERE id = :upload_id"
                ),
                {"path": "/prohibited/original-bill.pdf.enc", "upload_id": upload_id},
            )
            await session.commit()
        await session.rollback()
