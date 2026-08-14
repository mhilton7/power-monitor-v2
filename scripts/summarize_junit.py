#!/usr/bin/env python3
"""Summarize one or more JUnit XML files and fail on empty/failed evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.etree import ElementTree


def integer_attribute(element: ElementTree.Element, name: str) -> int:
    try:
        return int(element.attrib.get(name, "0"))
    except ValueError as exc:
        raise ValueError(f"invalid JUnit {name} attribute") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("inputs", type=Path, nargs="+")
    args = parser.parse_args()

    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    files: list[str] = []
    for path in args.inputs:
        content = path.read_bytes()
        if len(content) > 50 * 1024 * 1024 or b"<!DOCTYPE" in content or b"<!ENTITY" in content:
            raise ValueError(f"{path} is not safe bounded JUnit XML")
        root = ElementTree.fromstring(content)  # noqa: S314 - DTD/entity rejected above
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        if not suites:
            raise ValueError(f"{path} contains no JUnit test suites")
        for suite in suites:
            for key in totals:
                totals[key] += integer_attribute(suite, key)
        files.append(path.name)
    if totals["tests"] <= 0:
        raise ValueError("JUnit evidence contains zero tests")
    if totals["failures"] or totals["errors"]:
        raise ValueError("JUnit evidence contains a failure or error")
    result = {
        "schema": "pm-test-summary/1.0.0",
        "status": "passed",
        **totals,
        "files": files,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
