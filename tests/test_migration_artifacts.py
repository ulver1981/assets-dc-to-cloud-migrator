import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import import_schema_structure
import import_data_csv
from migration_artifacts import (
    ArtifactError,
    SchemaArtifacts,
    assert_unique_csv_stems,
    csv_inventory,
    record_phase_outcome,
    stale_phases,
)


def write_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("DC_Key,Name\nTEST-1,Test\n", encoding="utf-8")


class MigrationArtifactTests(unittest.TestCase):
    def test_inventory_discovers_root_and_nested_csv_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_dir = Path(tmp) / "exports" / "Test Schema" / "csv"
            write_csv(csv_dir / "Root.csv")
            write_csv(csv_dir / "Legacy" / "Nested.csv")

            inventory = csv_inventory(csv_dir)

            self.assertEqual([item.relative_path.as_posix() for item in inventory],
                             ["Legacy/Nested.csv", "Root.csv"])
            self.assertEqual(sum(item.is_nested for item in inventory), 1)

    def test_duplicate_csv_stems_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_dir = Path(tmp) / "csv"
            write_csv(csv_dir / "Type.csv")
            write_csv(csv_dir / "Legacy" / "Type.csv")

            with self.assertRaisesRegex(ArtifactError, "Ambiguous CSV"):
                assert_unique_csv_stems(csv_inventory(csv_dir))

    def test_manifest_records_versioned_phase_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifacts = SchemaArtifacts("Test Schema", base / "exports", base / "mappings")
            input_file = artifacts.schema_dir / "schema_structure.json"
            input_file.parent.mkdir(parents=True)
            input_file.write_text("{}", encoding="utf-8")

            manifest = record_phase_outcome(
                artifacts,
                "export-schema",
                "success",
                inputs=[input_file],
                counts={"objectTypes": 2},
                transformations=[{
                    "classification": "cloud-transformation",
                    "attribute": "Description",
                }],
            )

            self.assertEqual(manifest["version"], 1)
            persisted = json.loads(artifacts.manifest_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["phases"]["export-schema"]["counts"],
                             {"objectTypes": 2})
            self.assertEqual(
                persisted["phases"]["export-schema"]["transformations"][0]["classification"],
                "cloud-transformation",
            )

            input_file.write_text('{"changed": true}', encoding="utf-8")
            self.assertEqual(stale_phases(artifacts), ["export-schema"])

    def test_fix_refs_requires_exported_csv_before_configuration_or_api_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            schema_dir = base / "exports" / "Test Schema"
            mappings_dir = base / "mappings"
            schema_dir.mkdir(parents=True)
            mappings_dir.mkdir()
            (schema_dir / "schema_structure.json").write_text(
                json.dumps({"schema": {"id": 1}, "objectTypes": []}),
                encoding="utf-8",
            )
            (mappings_dir / "Test_Schema_mapping.json").write_text(
                json.dumps({"cloudSchemaId": "10", "objectTypeMapping": {}}),
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                import_schema_structure.fix_type_value_refs(
                    "Test Schema", base / "exports", mappings_dir
                )

            self.assertIn("Run export-data before fix-refs.", output.getvalue())

    @patch("import_schema_structure.config.validate")
    def test_fix_refs_uses_normalized_mapping_name_after_prerequisites(self, validate):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            schema_name = "Test Schema"
            schema_dir = base / "exports" / schema_name
            mappings_dir = base / "mappings"
            (schema_dir / "csv").mkdir(parents=True)
            mappings_dir.mkdir()
            (schema_dir / "schema_structure.json").write_text(
                json.dumps({"schema": {"id": 1}, "objectTypes": []}),
                encoding="utf-8",
            )
            (schema_dir / "_attr_meta.json").write_text("{}", encoding="utf-8")
            (schema_dir / "csv" / "Example.csv").write_text(
                "DC_Key,Name\nEX-1,Example\n", encoding="utf-8"
            )
            (mappings_dir / "Test_Schema_mapping.json").write_text(
                json.dumps({"cloudSchemaId": "10", "objectTypeMapping": {}}),
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                import_schema_structure.fix_type_value_refs(
                    schema_name, base / "exports", mappings_dir, dry_run=True
                )

            validate.assert_called_once()
            self.assertIn("No fixes needed", output.getvalue())

    def test_malformed_object_mapping_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            mapping_file = Path(tmp) / "objects.json"
            mapping_file.write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(ArtifactError, "malformed object mapping"):
                import_data_csv._incremental_save(mapping_file, {"TEST-1": "1"})

    @patch("import_schema_structure.config.validate")
    @patch("import_schema_structure.cloud_get")
    def test_mapping_reconciliation_previews_without_writing(self, cloud_get, validate):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifacts = SchemaArtifacts("Test Schema", base / "exports", base / "mappings")
            artifacts.schema_dir.mkdir(parents=True)
            artifacts.mappings_dir.mkdir()
            artifacts.structure_file.write_text(json.dumps({
                "objectTypes": [{"id": 1, "name": "Root"}, {"id": 2, "name": "Missing"}]
            }), encoding="utf-8")
            artifacts.mapping_file.write_text(json.dumps({
                "cloudSchemaId": "old", "objectTypeMapping": {"1": "old-id"}
            }), encoding="utf-8")
            cloud_get.return_value = [{"id": "10", "name": "Root"}]

            result = import_schema_structure.reconcile_mapping(
                "Test Schema", "99", artifacts.exports_dir, artifacts.mappings_dir
            )

            self.assertFalse(result["written"])
            self.assertEqual(result["unmatched"], [{"dcTypeId": 2, "name": "Missing"}])
            persisted = json.loads(artifacts.mapping_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["cloudSchemaId"], "old")

    @patch("import_schema_structure.config.validate")
    @patch("import_schema_structure.cloud_get")
    def test_mapping_reconciliation_writes_only_when_requested(self, cloud_get, validate):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifacts = SchemaArtifacts("Test Schema", base / "exports", base / "mappings")
            artifacts.schema_dir.mkdir(parents=True)
            artifacts.mappings_dir.mkdir()
            artifacts.structure_file.write_text(json.dumps({
                "objectTypes": [{"id": 1, "name": "Root"}]
            }), encoding="utf-8")
            artifacts.mapping_file.write_text(json.dumps({
                "cloudSchemaId": "old", "objectTypeMapping": {}
            }), encoding="utf-8")
            cloud_get.return_value = [{"id": "10", "name": "Root"}]

            result = import_schema_structure.reconcile_mapping(
                "Test Schema", "99", artifacts.exports_dir, artifacts.mappings_dir,
                write=True,
            )

            self.assertTrue(result["written"])
            persisted = json.loads(artifacts.mapping_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["cloudSchemaId"], "99")
            self.assertEqual(persisted["objectTypeMapping"], {"1": "10"})


if __name__ == "__main__":
    unittest.main()
