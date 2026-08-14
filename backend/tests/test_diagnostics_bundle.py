from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from backend.app.main import session_factory
from backend.app.models import ApplicationLog, user_home_scopes
from sqlalchemy import select


@pytest.mark.asyncio
async def test_diagnostics_bundle_is_allowlisted_redacted_and_checksummed(owner_client) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        home_id = await session.scalar(select(user_home_scopes.c.home_id))
        assert home_id is not None
        session.add(
            ApplicationLog(
                event_code="DIAGNOSTIC_TEST",
                level="warning",
                home_id=home_id,
                correlation_id="diagnostic-test-correlation",
                details={
                    "state": "failed",
                    "error_code": "TEST_FAILURE",
                    "authorization": "Bearer should-never-escape",
                    "password": "should-never-escape",
                    "raw_ocr_text": "customer-sensitive-text",
                    "result": "authorization: should-be-redacted",
                    "nested": {"token": "should-never-escape"},
                },
            )
        )
        await session.commit()

    response = await owner_client.get("/api/v1/diagnostics/bundle")
    assert response.status_code == 200, response.text
    assert response.headers["X-Content-SHA256"] == hashlib.sha256(response.content).hexdigest()
    assert b"should-never-escape" not in response.content
    assert b"customer-sensitive-text" not in response.content

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {
            "health.json",
            "application-logs.jsonl",
            "manifest.json",
        }
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_id"] == "pm-diagnostics-bundle/1.0.0"
        assert manifest["archive_sha256_delivery"] == "X-Content-SHA256 response header"
        for member in manifest["members"]:
            content = archive.read(member["path"])
            assert member["size_bytes"] == len(content)
            assert member["sha256"] == hashlib.sha256(content).hexdigest()
        lines = archive.read("application-logs.jsonl").splitlines()
        event = next(
            json.loads(line) for line in lines if b'"event_code":"DIAGNOSTIC_TEST"' in line
        )
        assert event["details"] == {
            "error_code": "TEST_FAILURE",
            "result": "[REDACTED]",
            "state": "failed",
        }
        assert event["excluded_detail_fields"] == [
            "authorization",
            "nested",
            "password",
            "raw_ocr_text",
        ]
