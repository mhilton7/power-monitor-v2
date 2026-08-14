from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    # The dedicated runtime starts with -S and -P. Replace, rather than append to,
    # sys.path so the worker can import only its frozen standard library and the
    # allowlisted parser dependency closure bundled in the image.
    runtime = Path("/opt/pm-pdf-sandbox/runtime/usr/local")
    standard_library = runtime / "lib/python3.13"
    sys.path[:] = [
        str(standard_library / "python313.zip"),
        str(standard_library),
        str(standard_library / "lib-dynload"),
        str(standard_library / "site-packages"),
    ]
    from backend.app.bill_rate_import.sandbox_worker import main as worker_main

    return worker_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
