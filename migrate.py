"""
migrate.py  –  Unified CLI for Jira Assets DC→Cloud migration

Usage:
    python migrate.py <command> [options]

Commands:
    export-schema   --schema-id 7
    import-schema   --schema-name "Example Inventory" [--dry-run]
    fix-refs        --schema-name "Example Inventory" [--dry-run]
    export-data     --schema-id 7
    import-data     --schema-name "Example Inventory"
    validate        --schema-name "Example Inventory" [--sample 5]
    check           --schema-name "Example Inventory"   (or --all)
    preflight       --schema-name "Example Inventory"
    reconcile-mapping --schema-name "Example Inventory" --cloud-schema-id 19 [--write]
    delete-schema   --cloud-schema-id 19
    status          [--schema-name "Example Inventory" | --all]

Typical workflow for ordered multi-schema import:

  1. Export a source schema structure:
       python migrate.py export-schema --schema-id <DC_SCHEMA_ID>

  2. Import it to Cloud (creates mappings/<Schema>_mapping.json):
       python migrate.py import-schema --schema-name "Example Inventory"

  3. Export data (creates exports/<Schema>/csv/ + _attr_meta.json):
       python migrate.py export-data --schema-id <DC_SCHEMA_ID>

  4. Import data (creates mappings/<Schema>_objects.json):
       python migrate.py import-data --schema-name "Example Inventory"

  5. Validate:
       python migrate.py validate --schema-name "Example Inventory"

  6. Import dependent schemas after their prerequisite schema mappings exist.

  RESET (delete Cloud schema before re-import):
       python migrate.py delete-schema --cloud-schema-id <ID>

  CHECK STATUS:
       python migrate.py status --all
"""

import argparse
import json
import sys
import io
from collections import Counter
from datetime import datetime
from pathlib import Path

from migration_artifacts import (
    ArtifactError,
    SchemaArtifacts,
    csv_data_rows,
    csv_inventory,
    legacy_csv_stem,
    read_json,
    safe_schema_name,
    stale_phases,
)
from migration_http import request_json


# ─────────────────────────────────────────────────────────────────────────────
# Logging tee — duplicate stdout to a log file
# ─────────────────────────────────────────────────────────────────────────────

class _TeeWriter(io.TextIOBase):
    """Write to both the original stdout and a log file."""

    def __init__(self, original, log_file):
        self._original = original
        self._log = log_file

    def write(self, s):
        safe_text = _console_safe_text(s)
        self._original.write(safe_text)
        try:
            self._log.write(safe_text)
            self._log.flush()
        except Exception:
            pass
        return len(s)

    def flush(self):
        self._original.flush()
        try:
            self._log.flush()
        except Exception:
            pass


def _start_log(command: str) -> Path | None:
    """Start logging stdout+stderr to logs/<timestamp>_<command>.log."""
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = logs_dir / f"{ts}_{command}.log"
    try:
        log_fh = open(log_path, "w", encoding="utf-8")
        sys.stdout = _TeeWriter(sys.__stdout__, log_fh)
        sys.stderr = _TeeWriter(sys.__stderr__, log_fh)
        print(f"[log] Logging to {log_path}")
        return log_path
    except Exception as e:
        print(f"[log] WARNING: could not create log file: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

def _console_safe_text(text: str | None) -> str:
    """Replace display-only Unicode that can break legacy Windows consoles."""
    if not text:
        return ""
    replacements = {
        "→": "->",
        "–": "-",
        "—": "-",
        "…": "...",
        "✓": "[OK]",
        "✗": "[ERR]",
        "⚠": "[WARN]",
    }
    safe = text
    for old, new in replacements.items():
        safe = safe.replace(old, new)
    return safe


def _safe_schema_name(schema_name: str) -> str:
    """Return the legacy-safe schema name used for mapping files."""
    return safe_schema_name(schema_name)


def _legacy_csv_stem(type_name: str) -> str:
    """Return the CSV stem used by the current exporter."""
    return legacy_csv_stem(type_name)


def _read_json(path: Path):
    """Read JSON with a compact error for reporting."""
    try:
        return read_json(path), None
    except ArtifactError as exc:
        return None, str(exc)


def _csv_data_rows(csv_file: Path) -> int:
    """Count CSV data rows without parsing values."""
    try:
        return csv_data_rows(csv_file)
    except ArtifactError:
        return 0


def _csv_rows_total(csv_files: list[Path]) -> int:
    return sum(_csv_data_rows(fp) for fp in csv_files)


def _expected_csv_stems(object_types: list[dict]) -> set[str]:
    """
    Build expected CSV stems using the existing exporter convention.

    Empty object types may not have CSV files, so missing stems are warnings,
    not hard failures.
    """
    name_counts = Counter(t.get("name", "") for t in object_types)
    stems: set[str] = set()
    for ot in object_types:
        name = ot.get("name", "")
        stem = _legacy_csv_stem(name)
        if name_counts[name] > 1:
            stem = f"{stem}__{ot.get('id')}"
        stems.add(stem)
    return stems


def _count_report_items(report_file: Path):
    if not report_file.exists():
        return None
    data, err = _read_json(report_file)
    if err:
        return "unreadable"
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return len(data)
    return "unknown"


def _collect_preflight_report(schema_name: str, exports_dir: Path,
                              mappings_dir: Path) -> dict:
    """Inspect local artifacts only. No Cloud/DC calls and no file writes."""
    artifacts = SchemaArtifacts(schema_name, exports_dir, mappings_dir)
    schema_dir = artifacts.schema_dir
    csv_dir = artifacts.csv_dir
    mapping_file = artifacts.mapping_file
    objects_file = artifacts.objects_file
    struct_file = artifacts.structure_file
    meta_file = artifacts.attr_meta_file
    failed_file = schema_dir / "failed_objects.json"
    unresolved_file = schema_dir / "unresolved_refs.json"
    validation_file = schema_dir / "validation_report.json"
    consistency_file = schema_dir / "consistency_report.json"

    report = {
        "schema": schema_name,
        "paths": {
            "schema_dir": str(schema_dir),
            "csv_dir": str(csv_dir),
            "mapping_file": str(mapping_file),
            "objects_file": str(objects_file),
        },
        "counts": {
            "object_types_in_schema": 0,
            "object_types_mapped": 0,
            "objects_mapped": 0,
            "csv_root_files": 0,
            "csv_recursive_files": 0,
            "csv_root_rows": 0,
            "csv_recursive_rows": 0,
            "nested_csv_files": 0,
        },
        "files": {
            "schema_structure": struct_file.exists(),
            "attr_meta": meta_file.exists(),
            "mapping": mapping_file.exists(),
            "objects_mapping": objects_file.exists(),
            "validation_report": validation_file.exists(),
            "consistency_report": consistency_file.exists(),
            "failed_objects": failed_file.exists(),
            "unresolved_refs": unresolved_file.exists(),
        },
        "failed_objects_count": _count_report_items(failed_file),
        "unresolved_refs_count": _count_report_items(unresolved_file),
        "missing_csv_stems": [],
        "nested_csv_paths": [],
        "stale_phases": [],
        "warnings": [],
        "errors": [],
    }

    object_types: list[dict] = []
    if not schema_dir.exists():
        report["errors"].append(f"Schema export folder not found: {schema_dir}")
    if struct_file.exists():
        struct, err = _read_json(struct_file)
        if err:
            report["errors"].append(f"Cannot read schema_structure.json: {err}")
        else:
            object_types = struct.get("objectTypes", [])
            report["counts"]["object_types_in_schema"] = len(object_types)
    else:
        report["errors"].append(f"Missing schema structure: {struct_file}")

    if not meta_file.exists():
        report["warnings"].append(f"Missing attr metadata: {meta_file}")

    if mapping_file.exists():
        mapping, err = _read_json(mapping_file)
        if err:
            report["errors"].append(f"Cannot read mapping file: {err}")
        else:
            report["counts"]["object_types_mapped"] = len(
                mapping.get("objectTypeMapping", {})
            )
            report["cloud_schema_id"] = mapping.get("cloudSchemaId")
            report["dc_schema_id"] = mapping.get("dcSchemaId")
    else:
        report["errors"].append(f"Missing mapping file: {mapping_file}")

    if objects_file.exists():
        objects, err = _read_json(objects_file)
        if err:
            report["errors"].append(f"Cannot read objects mapping: {err}")
        elif isinstance(objects, dict):
            report["counts"]["objects_mapped"] = len(objects)
        else:
            report["errors"].append(f"Objects mapping is not a JSON object: {objects_file}")
    else:
        report["warnings"].append(f"Missing objects mapping: {objects_file}")

    if csv_dir.exists():
        inventory = csv_inventory(csv_dir)
        root_csvs = [entry.path for entry in inventory if not entry.is_nested]
        recursive_csvs = [entry.path for entry in inventory]
        nested_csvs = [entry.path for entry in inventory if entry.is_nested]
        report["counts"]["csv_root_files"] = len(root_csvs)
        report["counts"]["csv_recursive_files"] = len(recursive_csvs)
        report["counts"]["csv_root_rows"] = _csv_rows_total(root_csvs)
        report["counts"]["csv_recursive_rows"] = _csv_rows_total(recursive_csvs)
        report["counts"]["nested_csv_files"] = len(nested_csvs)
        report["nested_csv_paths"] = [str(fp.relative_to(csv_dir)) for fp in nested_csvs]

        if object_types:
            expected = _expected_csv_stems(object_types)
            present = {fp.stem for fp in recursive_csvs}
            missing = sorted(expected - present)
            report["missing_csv_stems"] = missing
            if missing:
                report["warnings"].append(
                    f"{len(missing)} expected CSV file(s) are absent; this can be OK for empty object types."
                )

        if nested_csvs:
            report["warnings"].append(
                "Nested CSV files detected. They are included in the canonical artifact inventory."
            )

        if report["counts"]["csv_root_rows"] != report["counts"]["csv_recursive_rows"]:
            report["warnings"].append(
                "Root CSV row count differs from the complete recursive inventory."
            )
    else:
        report["errors"].append(f"Missing CSV directory: {csv_dir}")

    objects_mapped = report["counts"]["objects_mapped"]
    recursive_rows = report["counts"]["csv_recursive_rows"]
    root_rows = report["counts"]["csv_root_rows"]
    if recursive_rows and objects_mapped and objects_mapped != recursive_rows:
        report["warnings"].append(
            f"Objects mapping count ({objects_mapped}) differs from recursive CSV rows ({recursive_rows})."
        )
    elif root_rows and objects_mapped and objects_mapped != root_rows:
        report["warnings"].append(
            f"Objects mapping count ({objects_mapped}) differs from root CSV rows ({root_rows})."
        )

    if report["failed_objects_count"]:
        report["warnings"].append(f"Failed objects report present: {failed_file}")
    if report["unresolved_refs_count"]:
        report["warnings"].append(f"Unresolved refs report present: {unresolved_file}")
    if not validation_file.exists():
        report["warnings"].append(f"Validation report missing: {validation_file}")
    if not consistency_file.exists():
        report["warnings"].append(f"Consistency report missing: {consistency_file}")
    try:
        report["stale_phases"] = stale_phases(artifacts)
    except ArtifactError as exc:
        report["errors"].append(str(exc))
    if report["stale_phases"]:
        report["warnings"].append(
            "Recorded outcomes are stale for: "
            + ", ".join(report["stale_phases"])
            + ". Re-run the affected phase."
        )

    return report


def cmd_preflight(args):
    """Inspect local migration artifacts without modifying files or calling APIs."""
    report = _collect_preflight_report(
        args.schema_name,
        Path(args.exports_dir),
        Path(args.mappings_dir),
    )

    counts = report["counts"]
    files = report["files"]
    print(f"\n{'='*70}")
    print(f"  PREFLIGHT  {report['schema']}")
    print(f"{'='*70}\n")
    print("  Local-only check: no Cloud/DC API calls and no file writes.\n")

    print("  Files")
    for label, ok in files.items():
        print(f"    {'[OK]' if ok else '[  ]'} {label}")

    print("\n  Counts")
    print(f"    object types in schema : {counts['object_types_in_schema']}")
    print(f"    object types mapped    : {counts['object_types_mapped']}")
    print(f"    objects mapped         : {counts['objects_mapped']}")
    print(f"    CSV root files         : {counts['csv_root_files']}")
    print(f"    CSV recursive files    : {counts['csv_recursive_files']}")
    print(f"    CSV root data rows     : {counts['csv_root_rows']}")
    print(f"    CSV recursive rows     : {counts['csv_recursive_rows']}")
    print(f"    nested CSV files       : {counts['nested_csv_files']}")

    if report["failed_objects_count"] is not None:
        print(f"    failed objects report  : {report['failed_objects_count']}")
    if report["unresolved_refs_count"] is not None:
        print(f"    unresolved refs report : {report['unresolved_refs_count']}")

    if report["nested_csv_paths"]:
        print("\n  Nested CSV samples")
        for path in report["nested_csv_paths"][:10]:
            print(f"    - {path}")
        remaining = len(report["nested_csv_paths"]) - 10
        if remaining > 0:
            print(f"    ... {remaining} more")

    if report["missing_csv_stems"]:
        print("\n  Missing CSV stems")
        for stem in report["missing_csv_stems"][:15]:
            print(f"    - {stem}")
        remaining = len(report["missing_csv_stems"]) - 15
        if remaining > 0:
            print(f"    ... {remaining} more")

    if report["warnings"]:
        print("\n  Warnings")
        for warning in report["warnings"]:
            print(f"    [WARN] {warning}")

    if report["errors"]:
        print("\n  Errors")
        for error in report["errors"]:
            print(f"    [ERR] {error}")
        print(f"\n{'='*70}\n")
        sys.exit(1)

    print(f"\n{'='*70}\n")


def cmd_export_schema(args):
    from export_schema_structure import export_schema
    export_schema(args.schema_id, Path(args.exports_dir))


def cmd_import_schema(args):
    from import_schema_structure import import_schema
    import_schema(
        args.schema_name,
        Path(args.exports_dir),
        Path(args.mappings_dir),
        dry_run=args.dry_run,
    )


def cmd_fix_refs(args):
    from import_schema_structure import fix_type_value_refs
    fix_type_value_refs(
        args.schema_name,
        Path(args.exports_dir),
        Path(args.mappings_dir),
        dry_run=args.dry_run,
    )


def cmd_reconcile_mapping(args):
    from import_schema_structure import reconcile_mapping
    reconcile_mapping(
        args.schema_name,
        args.cloud_schema_id,
        Path(args.exports_dir),
        Path(args.mappings_dir),
        write=args.write,
    )


def cmd_export_data(args):
    from export_data_csv import export_data
    export_data(args.schema_id, Path(args.exports_dir))


def cmd_import_data(args):
    from import_data_csv import import_data
    import_data(
        args.schema_name,
        Path(args.exports_dir),
        Path(args.mappings_dir),
    )


def cmd_validate(args):
    from validate_migration import validate
    validate(
        args.schema_name,
        Path(args.exports_dir),
        Path(args.mappings_dir),
        sample_size=args.sample,
    )


def cmd_check(args):
    import sys as _sys
    import check_consistency
    # Build equivalent argv and delegate to check_consistency.main()
    fake_argv = ["check_consistency.py"]
    if args.all:
        fake_argv.append("--all")
    else:
        fake_argv.extend(["--schema-name", args.schema_name])
    fake_argv.extend(["--exports-dir", args.exports_dir, "--mappings-dir", args.mappings_dir])
    saved = _sys.argv
    _sys.argv = fake_argv
    try:
        check_consistency.main()
    finally:
        _sys.argv = saved


def cmd_delete_schema(args):
    import requests
    import config
    config.validate()

    cloud_schema_id = args.cloud_schema_id
    mappings_dir = Path(args.mappings_dir)
    url = f"{config.CLOUD_API_BASE}/objectschema/{cloud_schema_id}"

    # Find which schema name corresponds to this Cloud ID (for mapping cleanup)
    schema_name_for_cleanup = None
    for f in mappings_dir.glob("*_mapping.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if str(data.get("cloudSchemaId")) == str(cloud_schema_id):
                schema_name_for_cleanup = data.get("schemaName")
                break
        except Exception:
            pass

    if args.confirm_delete:
        confirm = "yes"
    else:
        confirm = input(
            f"DELETE Cloud schema {cloud_schema_id}? This is an irreversible "
            "Cloud write. Type 'yes' to confirm: "
        )
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        return

    try:
        request_json(
            requests.Session(),
            "DELETE",
            url,
            auth=config.cloud_auth(),
            headers=config.cloud_headers(),
            timeout=30,
            success_statuses={200, 204},
        )
        print(f"Schema {cloud_schema_id} deleted.")
    except requests.HTTPError as exc:
        print(f"ERROR deleting Cloud schema {cloud_schema_id}: {exc}")
        raise

    # Auto-clean stale mapping files
    if schema_name_for_cleanup:
        safe = schema_name_for_cleanup.replace(" ", "_").replace("/", "-")
        cleaned = []
        for suffix in ("_mapping.json", "_objects.json"):
            fp = mappings_dir / f"{safe}{suffix}"
            if fp.exists():
                fp.unlink()
                cleaned.append(fp.name)
        if cleaned:
            print(f"Cleaned up stale mapping files: {', '.join(cleaned)}")
        else:
            print("No mapping files to clean up.")
    else:
        print("(Could not determine schema name — check mappings/ manually)")


def cmd_status(args):
    """Show migration status for all or a specific schema."""
    exports_dir = Path(args.exports_dir)
    mappings_dir = Path(args.mappings_dir)

    # Collect all known schemas from exports/ and mappings/
    schemas: dict = {}  # schema_name → status_dict

    # From exports/
    if exports_dir.exists():
        for d in sorted(exports_dir.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                inventory = csv_inventory(d / "csv")
                schemas[d.name] = {
                    "schema_exported": (d / "schema_structure.json").exists(),
                    "data_exported": bool(inventory),
                    "attr_meta": (d / "_attr_meta.json").exists(),
                    "validation_report": (d / "validation_report.json").exists(),
                    "consistency_report": (d / "consistency_report.json").exists(),
                    "failed_objects": (d / "failed_objects.json").exists(),
                    "unresolved_refs": (d / "unresolved_refs.json").exists(),
                    "cloud_schema_id": None,
                    "dc_schema_id": None,
                    "mapping_exists": False,
                    "objects_mapping_exists": False,
                    "object_count_mapped": 0,
                    "csv_count": len(inventory),
                    "nested_csv_count": sum(1 for entry in inventory if entry.is_nested),
                }
                # Read DC schema ID from schema_structure.json
                struct_file = d / "schema_structure.json"
                if struct_file.exists():
                    try:
                        sdata = json.loads(struct_file.read_text(encoding="utf-8"))
                        schemas[d.name]["dc_schema_id"] = sdata.get("schema", {}).get("id")
                    except Exception:
                        pass

    # Enrich from mappings/
    if mappings_dir.exists():
        for f in mappings_dir.glob("*_mapping.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                name = data.get("schemaName", f.stem.replace("_mapping", ""))
                if name not in schemas:
                    schemas[name] = {}
                schemas[name]["mapping_exists"] = True
                schemas[name]["cloud_schema_id"] = data.get("cloudSchemaId")
                schemas[name]["dc_schema_id"] = data.get("dcSchemaId")
                type_count = len(data.get("objectTypeMapping", {}))
                schemas[name]["type_count_mapped"] = type_count
            except Exception:
                pass

        for f in mappings_dir.glob("*_objects.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                # Match to schema by safe_name
                base = f.stem.replace("_objects", "")
                for name in schemas:
                    safe = name.replace(" ", "_").replace("/", "-")
                    if safe == base:
                        schemas[name]["objects_mapping_exists"] = True
                        schemas[name]["object_count_mapped"] = len(data)
                        break
            except Exception:
                pass

    # Filter to specific schema if requested
    if not getattr(args, "all", False) and args.schema_name:
        schemas = {k: v for k, v in schemas.items() if k == args.schema_name}

    if not schemas:
        print("No schemas found. Run export-schema first.")
        return

    # Display
    print(f"\n{'='*70}")
    print(f"  MIGRATION STATUS")
    print(f"{'='*70}\n")

    for name, s in schemas.items():
        print(f"  Schema: {name}")
        dc_id = s.get("dc_schema_id", "?")
        cloud_id = s.get("cloud_schema_id", "-")
        print(f"    DC schema ID    : {dc_id}")
        print(f"    Cloud schema ID : {cloud_id}")
        print()

        def _icon(ok):
            return "[OK]" if ok else "[  ]"

        print(f"    {_icon(s.get('schema_exported'))}  1. Schema structure exported")
        print(f"    {_icon(s.get('mapping_exists'))}  2. Schema imported to Cloud"
              f"  ({s.get('type_count_mapped', '?')} types)")
        csv_n = s.get('csv_count', 0)
        print(f"    {_icon(s.get('data_exported'))}  3. Data exported"
              f"  ({csv_n} CSV files)")
        if s.get("nested_csv_count"):
            print(f"       [WARN] Includes {s['nested_csv_count']} nested CSV file(s)")
        obj_n = s.get('object_count_mapped', 0)
        print(f"    {_icon(s.get('objects_mapping_exists'))}  4. Data imported to Cloud"
              f"  ({obj_n} objects)")
        print(f"    {_icon(s.get('validation_report'))}  5. Validated")
        print(f"    {_icon(s.get('consistency_report'))}  6. Consistency checked")

        # Warnings
        if s.get("failed_objects"):
            print(f"    [WARN] Failed objects report present (check exports/{name}/failed_objects.json)")
        if s.get("unresolved_refs"):
            print(f"    [WARN] Unresolved refs present (check exports/{name}/unresolved_refs.json)")
        print()

    print(f"{'='*70}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Argument parser
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Jira Assets DC to Cloud migration toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_console_safe_text(__doc__),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Shared options
    def add_dirs(p):
        p.add_argument("--exports-dir", default="exports")
        p.add_argument("--mappings-dir", default="mappings")

    # export-schema
    p = sub.add_parser("export-schema", help="Export schema structure from DC")
    p.add_argument("--schema-id", type=int, required=True, help="DC schema numeric ID")
    add_dirs(p)

    # import-schema
    p = sub.add_parser("import-schema", help="Import schema structure to Cloud")
    p.add_argument("--schema-name", required=True, help="Schema name (folder under exports/)")
    p.add_argument("--dry-run", action="store_true", help="Simulate without Cloud calls")
    add_dirs(p)

    # fix-refs
    p = sub.add_parser("fix-refs", help="Fix obj-ref typeValue bug (DC API workaround)")
    p.add_argument("--schema-name", required=True, help="Schema name")
    p.add_argument("--dry-run", action="store_true", help="Simulate without Cloud calls")
    add_dirs(p)

    # reconcile-mapping
    p = sub.add_parser(
        "reconcile-mapping",
        help="Preview or update a schema type mapping against an existing Cloud schema",
    )
    p.add_argument("--schema-name", required=True, help="Schema name")
    p.add_argument("--cloud-schema-id", required=True, help="Existing Cloud schema ID")
    p.add_argument("--write", action="store_true",
                   help="Persist the reconciled mapping after previewing")
    add_dirs(p)

    # export-data
    p = sub.add_parser("export-data", help="Export object data from DC to CSV")
    p.add_argument("--schema-id", type=int, required=True)
    add_dirs(p)

    # import-data
    p = sub.add_parser("import-data", help="Import CSV object data to Cloud")
    p.add_argument("--schema-name", required=True)
    add_dirs(p)

    # validate
    p = sub.add_parser("validate", help="Validate migration completeness")
    p.add_argument("--schema-name", required=True)
    p.add_argument("--sample", type=int, default=5,
                   help="Random objects to sample per type (default: 5)")
    add_dirs(p)

    # check
    p = sub.add_parser("check", help="Exhaustive obj-ref consistency check")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--schema-name", help="Check a single schema by name")
    grp.add_argument("--all", action="store_true", help="Check all migrated schemas")
    add_dirs(p)

    # preflight
    p = sub.add_parser("preflight", help="Inspect local artifacts without API calls")
    p.add_argument("--schema-name", required=True)
    add_dirs(p)

    # delete-schema
    p = sub.add_parser("delete-schema", help="Delete a Cloud schema (IRREVERSIBLE)")
    p.add_argument("--cloud-schema-id", required=True, help="Cloud schema ID to delete")
    p.add_argument("--confirm-delete", action="store_true",
                   help="Explicit noninteractive acknowledgement of irreversible deletion")
    add_dirs(p)

    # status
    p = sub.add_parser("status", help="Show migration status overview")
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--schema-name", help="Show status for one schema")
    grp.add_argument("--all", action="store_true", default=False,
                     help="Show status for all schemas (default)")
    add_dirs(p)

    args = parser.parse_args()

    # Start logging for commands that do real work
    log_commands = {"export-schema", "import-schema", "fix-refs", "export-data",
                    "import-data", "validate", "check", "reconcile-mapping",
                    "delete-schema"}
    if args.command in log_commands:
        _start_log(args.command)

    dispatch = {
        "export-schema": cmd_export_schema,
        "import-schema": cmd_import_schema,
        "fix-refs": cmd_fix_refs,
        "reconcile-mapping": cmd_reconcile_mapping,
        "export-data": cmd_export_data,
        "import-data": cmd_import_data,
        "validate": cmd_validate,
        "check": cmd_check,
        "preflight": cmd_preflight,
        "delete-schema": cmd_delete_schema,
        "status": cmd_status,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
