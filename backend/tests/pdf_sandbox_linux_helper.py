from __future__ import annotations

import os
import sys
from pathlib import Path

_REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPOSITORY))

from backend.app.bill_rate_import import sandbox_launcher  # noqa: E402


def _read_paths(repository: Path) -> tuple[tuple[Path, int], ...]:
    read_execute = sandbox_launcher._LANDLOCK_READ_EXECUTE
    read_directory = sandbox_launcher._LANDLOCK_READ_DIRECTORY
    read_file = sandbox_launcher._LANDLOCK_READ_FILE
    read_write = sandbox_launcher._LANDLOCK_READ_WRITE_FILE
    candidates = (
        (repository, read_execute),
        (Path(sys.base_prefix), read_execute),
        (Path(sys.prefix), read_execute),
        (Path("/usr/lib"), read_directory),
        (Path("/lib"), read_directory),
        (Path("/etc/ld.so.cache"), read_file),
        (Path("/dev/null"), read_write),
        (Path("/dev/urandom"), read_file),
    )
    unique: dict[Path, int] = {}
    for path, access in candidates:
        if path.exists():
            unique[path.resolve()] = unique.get(path.resolve(), 0) | access
    return tuple(unique.items())


def main() -> int:
    if len(sys.argv) not in (3, 4):
        return 64
    mode = sys.argv[1]
    workdir = Path(sys.argv[2]).resolve()
    sentinel = sys.argv[3] if len(sys.argv) == 4 else None
    sandbox_launcher._establish_boundary(
        workdir,
        15,
        require_tmpfs=False,
        trusted_read_paths=_read_paths(_REPOSITORY),
    )
    from backend.app.bill_rate_import.sandbox_worker import main as worker_main

    if mode == "probe":
        return worker_main(("self-test", sentinel) if sentinel else ("self-test",))
    if mode == "parse":
        return worker_main(("parse", str(10 * 1024 * 1024)))
    return 64


if __name__ == "__main__":
    os.environ["PM_PDF_SANDBOX_SENTINEL_ENV"] = "must-be-cleared"
    raise SystemExit(main())
