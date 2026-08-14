from __future__ import annotations

from scripts.generate_contracts import generated_files


def test_generated_contracts_are_committed_without_drift() -> None:
    differences = [
        str(path)
        for path, expected in generated_files().items()
        if not path.is_file() or path.read_bytes() != expected
    ]
    assert differences == [], f"regenerate shared contracts: {differences}"
