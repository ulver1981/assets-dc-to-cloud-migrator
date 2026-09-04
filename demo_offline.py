"""Run an offline artifact preflight using only the bundled synthetic fixture."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import migrate


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "offline_demo"
SCHEMA_NAME = "Example Inventory"


def run_demo() -> dict:
    """Copy the fixture to a temporary workspace and verify its local artifacts."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for source in FIXTURE_ROOT.rglob("*"):
            if source.is_file() and source.name != "expected_preflight_report.json":
                target = root / source.relative_to(FIXTURE_ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        report = migrate._collect_preflight_report(
            SCHEMA_NAME, root / "exports", root / "mappings"
        )
    expected = json.loads(
        (FIXTURE_ROOT / "expected_preflight_report.json").read_text(encoding="utf-8")
    )
    if report["counts"]["csv_recursive_files"] != expected["csv_recursive_files"]:
        raise RuntimeError("Synthetic fixture CSV count did not match the expectation.")
    if report["counts"]["object_types_in_schema"] != expected["object_types_in_schema"]:
        raise RuntimeError("Synthetic fixture type count did not match the expectation.")
    return report


if __name__ == "__main__":
    report = run_demo()
    print("Offline demo passed.")
    print(f"Object types: {report['counts']['object_types_in_schema']}")
    print(f"CSV files: {report['counts']['csv_recursive_files']}")
