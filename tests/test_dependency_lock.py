from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

from backend.app.constants import VERSION
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)$")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_production_dependencies_are_exact_and_present_in_lock() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock: dict[str, str] = {}
    for raw_line in (ROOT / "backend" / "requirements.lock").read_text(
        encoding="utf-8"
    ).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = LOCK_PATTERN.fullmatch(line)
        assert match, f"production lock entry must be an exact name==version pin: {line}"
        name, version = match.groups()
        canonical = _canonical(name)
        assert canonical not in lock, f"duplicate locked dependency: {name}"
        lock[canonical] = version

    for value in project["dependencies"]:
        requirement = Requirement(value)
        pins = list(requirement.specifier)
        assert len(pins) == 1 and pins[0].operator == "==", value
        assert lock[_canonical(requirement.name)] == pins[0].version

    assert len(lock) >= len(project["dependencies"]), "transitive closure was not locked"


def test_release_version_metadata_is_consistent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads(
        (ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")
    )

    assert VERSION == "0.1.0-rc.3"
    assert project["version"] == "0.1.0rc3"
    assert package["version"] == VERSION
    assert package_lock["version"] == VERSION
    assert package_lock["packages"][""]["version"] == VERSION
    for relative in ("backend/Dockerfile", "frontend/Dockerfile", "backup/Dockerfile"):
        dockerfile = (ROOT / relative).read_text(encoding="utf-8")
        assert f"ARG VERSION={VERSION}" in dockerfile

    main = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    assert "from .constants import VERSION" in main
    assert 'version="0.1.0-rc.' not in main
    openapi = json.loads(
        (ROOT / "shared/openapi/power-meter-v2.openapi.json").read_text(encoding="utf-8")
    )
    assert openapi["info"]["version"] == VERSION
    assert (
        hashlib.sha256(
            (ROOT / "shared/openapi/power-meter-v2.openapi.json").read_bytes()
        ).hexdigest()
        == "7caada9c6295f4c201fd7ce7d383822e6b5785a960022de8355e3b6acc9a4e2c"
    )
