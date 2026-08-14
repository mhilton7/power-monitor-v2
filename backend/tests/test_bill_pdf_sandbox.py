from __future__ import annotations

import asyncio
import io
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import pytest
from backend.app.bill_rate_import import isolated
from backend.app.errors import BillRateImportError
from backend.tests.test_bill_rate_boundary import SCHEDULE
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]


def _valid_pdf() -> bytes:
    output = io.BytesIO()
    canvas = Canvas(output)
    y = 760
    for line in (line for line in SCHEDULE.splitlines() if line):
        canvas.drawString(40, y, line)
        y -= 18
    canvas.showPage()
    canvas.save()
    return output.getvalue()


def test_portable_parser_is_explicitly_test_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_ENV", "production")
    with pytest.raises(RuntimeError, match="only when PM_ENV=test"):
        isolated.extract_rate_plan_portable_for_tests(_valid_pdf())


@pytest.mark.asyncio
async def test_readiness_retries_failure_then_caches_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    results = iter((False, True))
    calls = 0

    async def self_test() -> bool:
        nonlocal calls
        calls += 1
        return next(results)

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(isolated, "_run_sandbox_self_test", self_test)
    monkeypatch.setattr(isolated, "_health_cache", (None, False))

    assert await isolated.pdf_sandbox_is_ready() is False
    assert calls == 1

    clock[0] += isolated._HEALTH_FAILURE_TTL_SECONDS - 1
    assert await isolated.pdf_sandbox_is_ready() is False
    assert calls == 1

    clock[0] += 2
    assert await isolated.pdf_sandbox_is_ready() is True
    assert calls == 2

    clock[0] += isolated._HEALTH_SUCCESS_TTL_SECONDS - 1
    assert await isolated.pdf_sandbox_is_ready() is True
    assert calls == 2


@pytest.mark.asyncio
async def test_production_parser_has_no_unsandboxed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workdir = Path(".test-runtime")
    monkeypatch.setattr(isolated, "_new_work_directory", lambda: workdir)
    monkeypatch.setattr(isolated, "_remove_work_directory", lambda _path: None)

    async def unavailable(*_args: object, **_kwargs: object) -> object:
        raise OSError("launcher missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unavailable)
    with pytest.raises(BillRateImportError, match="launcher is unavailable"):
        await isolated.extract_rate_plan_isolated(_valid_pdf())


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux kernel boundary test")
def test_landlock_seccomp_boundary_denies_secret_env_file_and_network(tmp_path: Path) -> None:
    helper = Path(__file__).with_name("pdf_sandbox_linux_helper.py")
    workdir = Path("/tmp") / f"pm-pdf-sandbox-test-{os.getpid()}-probe"  # noqa: S108
    sentinel = Path("/tmp") / f"pm-pdf-sandbox-sentinel-{os.getpid()}"  # noqa: S108
    workdir.mkdir(mode=0o700)
    sentinel.write_bytes(b"sentinel-secret-must-not-escape")
    try:
        completed = subprocess.run(  # noqa: S603
            [sys.executable, str(helper), "probe", str(workdir), str(sentinel)],
            # Even an empty input asks subprocess to provision an anonymous stdin pipe.
            input=b"",
            check=False,
            capture_output=True,
            timeout=20,
            env={**os.environ, "PM_PDF_SANDBOX_SENTINEL_ENV": "must-be-cleared"},
        )
    finally:
        sentinel.unlink(missing_ok=True)
        workdir.rmdir()
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert json.loads(completed.stdout) == {
        "environment_allowlist": True,
        "filesystem_denied": True,
        "network_syscalls_denied": True,
        "private_tmp": True,
        "schema_id": "pm-pdf-sandbox-self-test/1.0.0",
        "sensitive_mounts_inaccessible": True,
        "status": "ok",
    }


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux kernel boundary test")
def test_valid_pdf_parses_after_the_same_kernel_boundary() -> None:
    helper = Path(__file__).with_name("pdf_sandbox_linux_helper.py")
    workdir = Path("/tmp") / f"pm-pdf-sandbox-test-{os.getpid()}-parse"  # noqa: S108
    workdir.mkdir(mode=0o700)
    try:
        completed = subprocess.run(  # noqa: S603
            [sys.executable, str(helper), "parse", str(workdir)],
            input=_valid_pdf(),
            check=False,
            capture_output=True,
            timeout=20,
            env={**os.environ, "PM_PDF_SANDBOX_SENTINEL_ENV": "must-be-cleared"},
        )
    finally:
        workdir.rmdir()
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    output = json.loads(completed.stdout)
    assert set(output) == {"categories", "draft", "schema_id", "status"}
    assert output["schema_id"] == "pm-bill-rate-sandbox-output/1.0.0"
    assert output["status"] == "ok"
    assert output["categories"] == []
    assert output["draft"]["rate_plan_name"] == "TOU-D-4-9PM"
