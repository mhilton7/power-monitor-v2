from __future__ import annotations

import asyncio
import json

from .isolated import pdf_sandbox_is_ready


async def _check() -> int:
    ready = await pdf_sandbox_is_ready(force=True)
    print(
        json.dumps(
            {
                "pdf_sandbox": "enforced" if ready else "unavailable",
                "schema_id": "pm-pdf-sandbox-health/1.0.0",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if ready else 1


def main() -> int:
    return asyncio.run(_check())


if __name__ == "__main__":
    raise SystemExit(main())
