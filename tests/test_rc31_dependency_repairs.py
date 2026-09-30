from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pdf_security_patch_is_identical_in_api_and_isolated_parser() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = (ROOT / "backend/requirements.lock").read_text(encoding="utf-8")
    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")

    assert "pypdf==6.16.1" in project["dependencies"]
    assert "pypdf==6.16.1\n" in lock
    assert "      pypdf==6.16.1 \\" in dockerfile
    assert "pypdf==6.16.0" not in dockerfile


def test_inherited_alpine_security_fixes_and_available_packages_remain_exact() -> None:
    expected = {
        "backend/Dockerfile": ("libuuid=2.41.6-r1",),
        "frontend/Dockerfile": ("libexpat=2.8.5-r0", "libuuid=2.42.3-r1"),
        "gateway/Dockerfile": ("curl=8.22.0-r0", "libcurl=8.22.0-r0"),
        "backup/Dockerfile": ("jq=1.8.2-r0", "tzdata=2026d-r0", "libuuid=2.41.6-r1"),
    }
    for relative, pins in expected.items():
        dockerfile = (ROOT / relative).read_text(encoding="utf-8")
        for pin in pins:
            assert pin in dockerfile, (relative, pin)
        assert "@sha256:" in dockerfile


def test_gateway_security_patch_is_asserted_in_module_and_compiled_binary() -> None:
    module = (ROOT / "gateway/go.mod").read_text(encoding="utf-8")
    checksums = (ROOT / "gateway/go.sum").read_text(encoding="utf-8")
    dockerfile = (ROOT / "gateway/Dockerfile").read_text(encoding="utf-8")

    assert "go 1.26.6\n" in module
    assert "google.golang.org/grpc v1.83.2 // indirect" in module
    assert "google.golang.org/grpc v1.83.2 h1:" in checksums
    assert "google.golang.org/grpc v1.83.2/go.mod h1:" in checksums
    assert "grep -Fx 'google.golang.org/grpc v1.83.2'" in dockerfile
    assert r"google.golang.org/grpc[[:space:]]+v1\.83\.2[[:space:]]" in dockerfile
    assert "google.golang.org/grpc@v1.83.2/LICENSE" in dockerfile
    assert "go mod tidy -diff" in dockerfile
    assert "go mod verify" in dockerfile
