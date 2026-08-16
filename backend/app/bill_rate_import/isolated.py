from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..constants import MAX_PDF_BYTES
from ..errors import BillRateImportError
from ..schemas.billing import RatePlanDraft
from .parser import extract_rate_plan_from_pdf
from .sandbox_worker import EXTRACTION_SCHEMA_ID, SELF_TEST_SCHEMA_ID

_LAUNCHER: Final[tuple[str, ...]] = (
    "/usr/local/bin/python",
    "-I",
    "-m",
    "backend.app.bill_rate_import.sandbox_launcher",
)
_MAX_OUTPUT_BYTES: Final = 256 * 1024
_MAX_SELF_TEST_BYTES: Final = 8 * 1024
_LAUNCH_ENVIRONMENT: Final[dict[str, str]] = {
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
}
_PROHIBITED_CATEGORIES: Final = frozenset(
    {
        "CUSTOMER_IDENTITY",
        "METER_IDENTIFIER",
        "BILL_USAGE",
        "BILL_TOTAL",
        "PAYMENT",
    }
)


class _SandboxSuccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_id: Literal["pm-bill-rate-sandbox-output/1.0.0"]
    status: Literal["ok"]
    draft: RatePlanDraft
    categories: Annotated[tuple[str, ...], Field(max_length=5)]


class _SandboxRejected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_id: Literal["pm-bill-rate-sandbox-output/1.0.0"]
    status: Literal["rejected"]
    error_code: Literal[
        "CHARGES_PAGE_NOT_FOUND",
        "DOCUMENT_REJECTED",
        "EXTRACTION_TIMED_OUT",
        "PDF_ENCRYPTED",
        "PDF_INVALID",
        "PDF_PAGE_LIMIT",
        "PDF_TEXT_UNAVAILABLE",
        "PDF_TOO_LARGE",
        "RATE_LINES_NOT_FOUND",
        "RATE_NAME_NOT_FOUND",
        "UNSUPPORTED_RATE_STRUCTURE",
        "UTILITY_NOT_RECOGNIZED",
    ]


_REJECTION_DETAILS: Final[dict[str, str]] = {
    "CHARGES_PAGE_NOT_FOUND": "No SCE rate-detail charges page was found in the PDF.",
    "DOCUMENT_REJECTED": "The isolated rate extractor rejected the document.",
    "EXTRACTION_TIMED_OUT": "Bill rate extraction timed out.",
    "PDF_ENCRYPTED": "Encrypted or password-protected PDFs are not accepted.",
    "PDF_INVALID": "The upload is not a valid PDF.",
    "PDF_PAGE_LIMIT": "The PDF page count is outside the configured limit.",
    "PDF_TEXT_UNAVAILABLE": "The PDF has no usable text layer and local OCR failed.",
    "PDF_TOO_LARGE": "The PDF exceeds the configured size limit.",
    "RATE_LINES_NOT_FOUND": "Required reusable SCE rate lines are missing.",
    "RATE_NAME_NOT_FOUND": "No supported reusable SCE rate-plan name was found.",
    "UNSUPPORTED_RATE_STRUCTURE": "The SCE rate structure is not supported.",
    "UTILITY_NOT_RECOGNIZED": "The rate-detail page is not recognized as SCE.",
}


class _SandboxSelfTest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_id: Literal["pm-pdf-sandbox-self-test/1.0.0"]
    status: Literal["ok"]
    environment_allowlist: Literal[True]
    filesystem_denied: Literal[True]
    network_syscalls_denied: Literal[True]
    private_tmp: Literal[True]
    sensitive_mounts_inaccessible: Literal[True]


_HEALTH_SUCCESS_TTL_SECONDS: Final = 300.0
_HEALTH_FAILURE_TTL_SECONDS: Final = 5.0
_health_cache: tuple[float | None, bool] = (None, False)
_health_lock = asyncio.Lock()


def _health_cache_is_fresh(now: float) -> bool:
    checked_at, ready = _health_cache
    if checked_at is None:
        return False
    ttl = _HEALTH_SUCCESS_TTL_SECONDS if ready else _HEALTH_FAILURE_TTL_SECONDS
    return now - checked_at < ttl


def extract_rate_plan_portable_for_tests(
    data: bytes,
) -> tuple[RatePlanDraft, tuple[str, ...]]:
    """Portable parser path restricted to the explicit test environment."""

    if os.environ.get("PM_ENV") != "test":
        raise RuntimeError("portable PDF parsing is available only when PM_ENV=test")
    return extract_rate_plan_from_pdf(data)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _decode_closed_json(output: bytes) -> object:
    if not output or output.startswith(b"\xef\xbb\xbf"):
        raise ValueError("empty or BOM-prefixed sandbox output")
    return json.loads(output, object_pairs_hook=_reject_duplicate_json_keys)


async def _read_capped(stream: asyncio.StreamReader, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(min(64 * 1024, maximum_bytes + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum_bytes:
            raise BillRateImportError("isolated bill rate extraction exceeded its output limit")


async def _feed_stdin(stream: asyncio.StreamWriter, data: bytes) -> None:
    try:
        stream.write(data)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        return
    finally:
        stream.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await stream.wait_closed()


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        # A negative POSIX pid addresses the process group created for the sandbox.
        os.kill(-process.pid, 9)
    except (AttributeError, OSError):
        process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        return


async def _invoke_sandbox(
    arguments: Sequence[str],
    *,
    input_bytes: bytes,
    timeout_seconds: int,
    output_limit: int,
    inherited_environment: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *_LAUNCHER,
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=inherited_environment or _LAUNCH_ENVIRONMENT,
        )
    except (OSError, NotImplementedError) as exc:
        raise BillRateImportError("required PDF sandbox launcher is unavailable") from exc
    if process.stdin is None or process.stdout is None:
        await _terminate(process)
        raise BillRateImportError("required PDF sandbox IPC could not be established")
    feed_task = asyncio.create_task(_feed_stdin(process.stdin, input_bytes))
    try:
        async with asyncio.timeout(timeout_seconds):
            output = await _read_capped(process.stdout, output_limit)
            await feed_task
            return_code = await process.wait()
    except TimeoutError as exc:
        await _terminate(process)
        raise BillRateImportError("bill rate extraction exceeded its total-time limit") from exc
    except BaseException:
        await _terminate(process)
        raise
    finally:
        if not feed_task.done():
            feed_task.cancel()
            with suppress(asyncio.CancelledError):
                await feed_task
    return return_code, output


def _new_work_directory() -> Path:
    path = Path(tempfile.mkdtemp(prefix="pm-pdf-sandbox-", dir="/tmp"))
    path.chmod(0o700)
    return path


def _remove_work_directory(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise BillRateImportError("sandbox temporary-data cleanup failed") from exc


async def extract_rate_plan_isolated(
    data: bytes, *, timeout_seconds: int = 30
) -> tuple[RatePlanDraft, tuple[str, ...]]:
    """Parse inside the mandatory Linux Landlock/seccomp production boundary."""

    if len(data) > MAX_PDF_BYTES:
        raise BillRateImportError("PDF exceeds the configured size limit")
    workdir = _new_work_directory()
    try:
        return_code, output = await _invoke_sandbox(
            ("parse", str(workdir), str(timeout_seconds)),
            input_bytes=data,
            timeout_seconds=timeout_seconds + 2,
            output_limit=_MAX_OUTPUT_BYTES,
        )
    finally:
        _remove_work_directory(workdir)
    if return_code != 0:
        try:
            rejected = _SandboxRejected.model_validate(_decode_closed_json(output))
        except (UnicodeDecodeError, ValueError, ValidationError) as exc:
            raise BillRateImportError(
                "The isolated rate extractor returned an invalid rejection.",
                code="DOCUMENT_REJECTED",
            ) from exc
        raise BillRateImportError(_REJECTION_DETAILS[rejected.error_code], code=rejected.error_code)
    try:
        decoded = _decode_closed_json(output)
        result = _SandboxSuccess.model_validate(decoded)
    except (UnicodeDecodeError, ValueError, ValidationError) as exc:
        raise BillRateImportError("isolated bill rate extraction returned invalid output") from exc
    if result.schema_id != EXTRACTION_SCHEMA_ID:
        raise BillRateImportError("isolated bill rate extraction returned an unknown schema")
    if tuple(sorted(set(result.categories))) != result.categories or any(
        value not in _PROHIBITED_CATEGORIES for value in result.categories
    ):
        raise BillRateImportError("isolated bill rate extraction returned invalid categories")
    if result.draft.source_artifact_sha256 != hashlib.sha256(data).hexdigest():
        raise BillRateImportError("isolated bill rate extraction returned mismatched lineage")
    return result.draft, result.categories


async def _run_sandbox_self_test(timeout_seconds: int = 5) -> bool:
    workdir = _new_work_directory()
    sentinel_fd, sentinel_name = tempfile.mkstemp(prefix="pm-pdf-sandbox-sentinel-", dir="/tmp")
    sentinel = Path(sentinel_name)
    try:
        os.write(sentinel_fd, b"sandbox-must-not-read-this")
    finally:
        os.close(sentinel_fd)
    inherited_environment = dict(_LAUNCH_ENVIRONMENT)
    inherited_environment["PM_PDF_SANDBOX_SENTINEL_ENV"] = "sandbox-must-clear-this"
    try:
        return_code, output = await _invoke_sandbox(
            ("self-test", str(workdir), str(timeout_seconds), str(sentinel)),
            input_bytes=b"",
            timeout_seconds=timeout_seconds + 2,
            output_limit=_MAX_SELF_TEST_BYTES,
            inherited_environment=inherited_environment,
        )
    finally:
        sentinel.unlink(missing_ok=True)
        _remove_work_directory(workdir)
    if return_code != 0:
        return False
    try:
        result = _SandboxSelfTest.model_validate(_decode_closed_json(output))
    except (UnicodeDecodeError, ValueError, ValidationError):
        return False
    return result.schema_id == SELF_TEST_SCHEMA_ID


async def pdf_sandbox_is_ready(*, force: bool = False) -> bool:
    """Return cached machine evidence that the production boundary is enforceable."""

    global _health_cache
    now = time.monotonic()
    if not force and _health_cache_is_fresh(now):
        return _health_cache[1]
    async with _health_lock:
        now = time.monotonic()
        if not force and _health_cache_is_fresh(now):
            return _health_cache[1]
        try:
            ready = await _run_sandbox_self_test()
        except BillRateImportError:
            ready = False
        _health_cache = (time.monotonic(), ready)
        return ready
