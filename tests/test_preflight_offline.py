import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import migrate


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_csv(path: Path, data_rows: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "DC_Key,Name\n" + "\n".join(data_rows)
    if data_rows:
        content += "\n"
    path.write_text(content, encoding="utf-8")


class PreflightOfflineTests(unittest.TestCase):
    def test_legacy_csv_stem_keeps_current_export_convention(self):
        self.assertEqual(migrate._legacy_csv_stem("NO/Contracts"), "NO_Contracts")
        self.assertEqual(migrate._legacy_csv_stem(r"NO\Contracts"), "NO_Contracts")

    def test_preflight_counts_root_and_nested_csvs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            exports_dir = base / "exports"
            mappings_dir = base / "mappings"
            schema_name = "Test Schema"
            schema_dir = exports_dir / schema_name
            csv_dir = schema_dir / "csv"

            write_json(schema_dir / "schema_structure.json", {
                "schema": {"id": 1, "name": schema_name},
                "objectTypes": [
                    {"id": 1, "name": "Root"},
                    {"id": 2, "name": "Child"},
                ],
            })
            write_json(schema_dir / "_attr_meta.json", {})
            write_json(mappings_dir / "Test_Schema_mapping.json", {
                "schemaName": schema_name,
                "dcSchemaId": 1,
                "cloudSchemaId": "10",
                "objectTypeMapping": {"1": "11", "2": "12"},
            })
            write_json(mappings_dir / "Test_Schema_objects.json", {
                "T-1": "10001",
            })
            write_csv(csv_dir / "Root.csv", ["T-1,Root one"])
            write_csv(csv_dir / "Nested" / "Child.csv", [
                "T-2,Child one",
                "T-3,Child two",
            ])

            report = migrate._collect_preflight_report(
                schema_name, exports_dir, mappings_dir
            )

            self.assertEqual(report["counts"]["csv_root_files"], 1)
            self.assertEqual(report["counts"]["csv_recursive_files"], 2)
            self.assertEqual(report["counts"]["csv_root_rows"], 1)
            self.assertEqual(report["counts"]["csv_recursive_rows"], 3)
            self.assertEqual(report["counts"]["objects_mapped"], 1)
            self.assertTrue(any("Nested CSV files detected" in w
                                for w in report["warnings"]))

    def test_status_output_is_cp1252_safe_with_failed_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            exports_dir = base / "exports"
            mappings_dir = base / "mappings"
            schema_name = "Test Schema"
            schema_dir = exports_dir / schema_name

            write_json(schema_dir / "schema_structure.json", {
                "schema": {"id": 1, "name": schema_name},
                "objectTypes": [],
            })
            write_json(schema_dir / "failed_objects.json", [
                {"dc_key": "T-1", "error": "sample"}
            ])
            write_json(mappings_dir / "Test_Schema_mapping.json", {
                "schemaName": schema_name,
                "dcSchemaId": 1,
                "cloudSchemaId": "10",
                "objectTypeMapping": {},
            })

            args = SimpleNamespace(
                exports_dir=str(exports_dir),
                mappings_dir=str(mappings_dir),
                schema_name=None,
                all=True,
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                migrate.cmd_status(args)

            rendered = output.getvalue()
            rendered.encode("cp1252")
            self.assertIn("[WARN] Failed objects report present", rendered)


if __name__ == "__main__":
    unittest.main()
