from __future__ import annotations

import re
import tomllib
from pathlib import Path

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
