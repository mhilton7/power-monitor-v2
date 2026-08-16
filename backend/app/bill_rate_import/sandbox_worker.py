from __future__ import annotations

import errno
import json
import os
import socket
import sys
from collections.abc import Sequence
from typing import Final

from ..errors import BillRateImportError
from .ocr import local_tesseract_ocr
from .parser import PROHIBITED_PATTERNS, extract_rate_plan_from_pdf

EXTRACTION_SCHEMA_ID: Final = "pm-bill-rate-sandbox-output/1.0.0"
SELF_TEST_SCHEMA_ID: Final = "pm-pdf-sandbox-self-test/1.0.0"
_ALLOWED_ENVIRONMENT: Final = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHOME",
        "TESSDATA_PREFIX",
        "TMPDIR",
        "XDG_CACHE_HOME",
    }
)
_O_CLOEXEC: Final = 0x80000
_SAFE_REJECTION_CODES: Final = frozenset(
    {
        "CHARGES_PAGE_NOT_FOUND",
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
    }
)


def _write_closed(value: dict[str, object]) -> None:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _parse(maximum_bytes: int) -> int:
    data = sys.stdin.buffer.read(maximum_bytes + 1)
    if len(data) > maximum_bytes:
        _write_closed(
            {
                "error_code": "DOCUMENT_REJECTED",
                "schema_id": EXTRACTION_SCHEMA_ID,
                "status": "rejected",
            }
        )
        return 2
    try:
        draft, categories = extract_rate_plan_from_pdf(data, local_ocr=local_tesseract_ocr)
        # Defense in depth: every emitted category must come from the parser's
        # fixed classification vocabulary and never from document text.
        allowed_categories = frozenset(PROHIBITED_PATTERNS)
        if any(category not in allowed_categories for category in categories):
            raise ValueError("non-allowlisted category")
        _write_closed(
            {
                "categories": list(categories),
                "draft": draft.model_dump(mode="json"),
                "schema_id": EXTRACTION_SCHEMA_ID,
                "status": "ok",
            }
        )
        return 0
    except BillRateImportError as exc:
        _write_closed(
            {
                "error_code": exc.code
                if exc.code in _SAFE_REJECTION_CODES
                else "DOCUMENT_REJECTED",
                "schema_id": EXTRACTION_SCHEMA_ID,
                "status": "rejected",
            }
        )
        return 2
    except Exception:
        _write_closed(
            {
                "error_code": "DOCUMENT_REJECTED",
                "schema_id": EXTRACTION_SCHEMA_ID,
                "status": "rejected",
            }
        )
        return 2
    finally:
        data = b""


def _denied(operation: object) -> bool:
    try:
        if callable(operation):
            operation()
    except OSError as exc:
        return exc.errno in {errno.EACCES, errno.EPERM}
    return False


def _path_inaccessible(path: str, *, must_exist: bool) -> bool:
    try:
        descriptor = os.open(path, os.O_RDONLY | _O_CLOEXEC)
    except OSError as exc:
        accepted = {errno.EACCES, errno.EPERM}
        if not must_exist:
            accepted.add(errno.ENOENT)
        return exc.errno in accepted
    os.close(descriptor)
    return False


def _self_test(sentinel: str | None) -> int:
    workdir = os.environ.get("TMPDIR", "")
    private_tmp = False
    probe_file = os.path.join(workdir, "self-test")
    try:
        with open(probe_file, "xb") as write_stream:
            write_stream.write(b"ok")
        with open(probe_file, "rb") as read_stream:
            private_tmp = read_stream.read() == b"ok"
        os.unlink(probe_file)
    except OSError:
        private_tmp = False

    environment_allowlist = set(os.environ) == _ALLOWED_ENVIRONMENT
    filesystem_denied = bool(sentinel and _path_inaccessible(sentinel, must_exist=True))
    sensitive_mounts_inaccessible = all(
        _path_inaccessible(path, must_exist=False) for path in ("/run/secrets", "/data", "/app")
    )

    def open_socket() -> None:
        descriptor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        descriptor.close()

    network_syscalls = _denied(open_socket)
    passed = all(
        (
            environment_allowlist,
            filesystem_denied,
            network_syscalls,
            private_tmp,
            sensitive_mounts_inaccessible,
        )
    )
    _write_closed(
        {
            "environment_allowlist": environment_allowlist,
            "filesystem_denied": filesystem_denied,
            "network_syscalls_denied": network_syscalls,
            "private_tmp": private_tmp,
            "schema_id": SELF_TEST_SCHEMA_ID,
            "sensitive_mounts_inaccessible": sensitive_mounts_inaccessible,
            "status": "ok" if passed else "failed",
        }
    )
    return 0 if passed else 3


def main(arguments: Sequence[str] | None = None) -> int:
    values = tuple(arguments if arguments is not None else sys.argv[1:])
    if values and values[0] == "parse" and len(values) == 2:
        try:
            maximum_bytes = int(values[1])
        except ValueError:
            return 64
        return _parse(maximum_bytes)
    if values and values[0] == "self-test" and len(values) in (1, 2):
        return _self_test(values[1] if len(values) == 2 else None)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
