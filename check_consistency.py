"""
check_consistency.py

Exhaustive consistency check for migrated Jira Assets schemas.

For EVERY object-reference attribute of EVERY migrated object, verifies that:
  1. The referenced DC key exists in the object mappings (target was migrated)
  2. The Cloud object actually holds the correct reference

This is NOT a sample — it checks every single relationship.

Usage:
    python check_consistency.py --schema-name "Example Inventory"
    python check_consistency.py --schema-name "Example Services"
    python check_consistency.py --all          # check all migrated schemas

Output:
    exports/<Schema Name>/consistency_report.json
    Detailed summary printed to stdout
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from collections import defaultdict

import requests

import config
from migration_artifacts import (
    ArtifactError,
    SchemaArtifacts,
    assert_unique_csv_stems,
    csv_inventory,
    read_json,
    record_phase_outcome,
)
from migration_http import request_json

EXPORTS_DIR = Path("exports")
MAPPINGS_DIR = Path("mappings")

# Shared session for connection pooling
_session = requests.Session()


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def cloud_get(path: str, params: dict = None, retries: int = 3):
    return request_json(
        _session,
        "GET",
        f"{config.CLOUD_API_BASE}{path}",
        auth=config.cloud_auth(),
        headers=config.cloud_headers(),
        params=params,
        timeout=60,
        retries=retries,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cloud object loading (batch)
# ─────────────────────────────────────────────────────────────────────────────

def load_all_cloud_objects(cloud_type_id: str) -> dict:
    """
    Load ALL objects of a Cloud type with attributes.
    Returns {cloud_obj_id: {"name": ..., "dc_key": ..., "refs": {attr_name: [cloud_obj_ids]}}}.
    """
    objects = {}
    page = 1
    page_size = 500
    while True:
        data = cloud_get("/iql/objects", params={
            "iql": f"objectTypeId = {cloud_type_id}",
            "resultsPerPage": page_size,
            "page": page,
            "includeAttributes": "true",
        })
        entries = data.get("objectEntries", [])
        for obj in entries:
            obj_id = str(obj["id"])
            dc_key = ""
            refs: dict = {}
            for attr in obj.get("attributes", []):
                ota = attr.get("objectTypeAttribute") or {}
                attr_name = ota.get("name", "")
                if attr_name == "DC_Key":
                    vals = attr.get("objectAttributeValues") or []
                    if vals:
                        dc_key = str(vals[0].get("displayValue") or vals[0].get("value") or "")
                elif int(ota.get("type", 0)) == 1:
                    # Object-reference attribute
                    ref_ids = []
                    for v in (attr.get("objectAttributeValues") or []):
                        ref_obj = v.get("referencedObject")
                        if ref_obj:
                            ref_ids.append(str(ref_obj.get("id", "")))
                        elif v.get("value"):
                            ref_ids.append(str(v["value"]))
                    if ref_ids:
                        refs[attr_name] = ref_ids
            objects[obj_id] = {"name": obj.get("name", ""), "dc_key": dc_key, "refs": refs}
        total = data.get("totalFilterCount", 0)
        if not entries or len(objects) >= total:
            break
        page += 1
    return objects


# ─────────────────────────────────────────────────────────────────────────────
# Core consistency check
# ─────────────────────────────────────────────────────────────────────────────

def check_schema_consistency(schema_name: str) -> dict:
    """
    Run exhaustive consistency check for one schema.
    Returns the report dict.
    """
    artifacts = SchemaArtifacts(schema_name, EXPORTS_DIR, MAPPINGS_DIR)
    mapping_file = artifacts.mapping_file
    objects_file = artifacts.objects_file
    schema_dir = artifacts.schema_dir
    csv_dir = artifacts.csv_dir
    meta_file = artifacts.attr_meta_file

    # Validate files exist
    for p, label in [(mapping_file, "mapping"), (objects_file, "objects"),
                     (csv_dir, "csv dir"), (meta_file, "attr meta")]:
        if not p.exists():
            print(f"  ERROR: {p} not found ({label}). Run the full migration first.")
            return {"error": f"Missing {label}: {p}"}

    # Load data
    try:
        mapping = read_json(mapping_file)
        schema_objects = read_json(objects_file)
        attr_meta = read_json(meta_file)
        csv_files = csv_inventory(csv_dir)
        assert_unique_csv_stems(csv_files)
    except ArtifactError as exc:
        print(f"  ERROR: {exc}")
        return {"error": str(exc)}
    if not all(isinstance(value, dict) for value in (mapping, schema_objects, attr_meta)):
        return {"error": "Mapping, object mapping, and attribute metadata must be JSON objects."}

    cloud_schema_id = str(mapping["cloudSchemaId"])
    dc_type_map = {str(k): str(v) for k, v in mapping["objectTypeMapping"].items()}

    # Load ALL object mappings from all schemas (for cross-schema ref resolution)
    all_objects: dict = {}
    for f in MAPPINGS_DIR.glob("*_objects.json"):
        try:
            all_objects.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass

    # Build reverse map: cloud_obj_id → dc_key (from all schemas)
    cloud_id_to_dc_key: dict = {str(v): k for k, v in all_objects.items()}

    print(f"\n  Schema: {schema_name} (Cloud {cloud_schema_id})")
    print(f"  Object mappings loaded: {len(schema_objects)} this schema, {len(all_objects)} total")

    # Get Cloud types
    cloud_types = cloud_get(f"/objectschema/{cloud_schema_id}/objecttypes")
    cloud_type_names = {str(t["id"]): t["name"] for t in cloud_types}

    report = {
        "schema": schema_name,
        "cloudSchemaId": cloud_schema_id,
        "types": [],
        "summary": {
            "total_objects": 0,
            "total_refs_expected": 0,
            "total_refs_ok": 0,
            "total_refs_missing_target": 0,
            "total_refs_broken": 0,
            "total_refs_extra": 0,
        },
    }

    # Process each type
    for dc_type_id_str, cloud_type_id in dc_type_map.items():
        dc_type_id = int(dc_type_id_str)
        type_name = cloud_type_names.get(str(cloud_type_id), f"DC_{dc_type_id}")

        # Find the right CSV
        csv_file = None
        for csv_artifact in csv_files:
            f = csv_artifact.path
            stem = csv_artifact.stem
            # Handle __dcTypeId suffix
            import re
            m = re.search(r"__(\d+)$", stem)
            if m:
                if m.group(1) == dc_type_id_str:
                    csv_file = f
                    break
            else:
                # Match by name (resolve _ to space or /)
                if stem == type_name or stem.replace("_", " ") == type_name:
                    csv_file = f
                    break

        # Also find attr_meta key
        meta_key = None
        for k in attr_meta:
            mk = re.search(r"__(\d+)$", k)
            if mk and mk.group(1) == dc_type_id_str:
                meta_key = k
                break
            elif k == type_name or k.replace("_", " ") == type_name:
                meta_key = k
                break

        if not csv_file:
            # Not all types have CSVs (could be empty)
            continue

        type_meta = attr_meta.get(meta_key, {}) if meta_key else {}

        # Identify obj-ref columns
        objref_cols = {name for name, info in type_meta.items() if info.get("type") == 1}
        if not objref_cols:
            continue  # no obj-ref attrs, nothing to check

        # Load CSV data (source of truth for expected references)
        with open(csv_file, encoding="utf-8") as f:
            csv_rows = list(csv.DictReader(f))

        # Load all Cloud objects of this type (for actual state verification)
        print(f"  [{type_name}] loading {len(csv_rows)} expected rows + Cloud objects …",
              end=" ", flush=True)
        cloud_objects = load_all_cloud_objects(str(cloud_type_id))
        print(f"{len(cloud_objects)} cloud objects")

        # Build cloud dc_key → cloud_obj lookup for this type
        cloud_by_dc_key: dict = {}
        for cid, cobj in cloud_objects.items():
            if cobj["dc_key"]:
                cloud_by_dc_key[cobj["dc_key"]] = (cid, cobj)

        type_report = {
            "type": type_name,
            "cloudTypeId": cloud_type_id,
            "objects_in_csv": len(csv_rows),
            "objects_in_cloud": len(cloud_objects),
            "refs_expected": 0,
            "refs_ok": 0,
            "refs_missing_target": 0,
            "refs_broken": 0,
            "refs_extra": 0,
            "issues": [],
        }

        for row in csv_rows:
            dc_key = row.get("DC_Key", "").strip()
            if not dc_key:
                continue

            cloud_entry = cloud_by_dc_key.get(dc_key)
            if not cloud_entry:
                # Object wasn't migrated (known error — e.g. missing mandatory field)
                continue

            cloud_obj_id, cloud_obj = cloud_entry
            report["summary"]["total_objects"] += 1

            for col in objref_cols:
                raw_val = row.get(col, "").strip()
                if not raw_val:
                    continue

                # Expected DC keys for this ref
                expected_dc_keys = [k.strip() for k in raw_val.split("|") if k.strip()]

                # Expected Cloud object IDs
                expected_cloud_ids = set()
                missing_targets = []
                for ek in expected_dc_keys:
                    cid = all_objects.get(ek)
                    if cid:
                        expected_cloud_ids.add(str(cid))
                    else:
                        missing_targets.append(ek)

                # Actual Cloud refs for this attr
                actual_cloud_ids = set(cloud_obj["refs"].get(col, []))

                type_report["refs_expected"] += len(expected_dc_keys)

                # Check missing targets (DC object not migrated)
                for mk in missing_targets:
                    type_report["refs_missing_target"] += 1
                    type_report["issues"].append({
                        "dc_key": dc_key,
                        "attr": col,
                        "issue": "MISSING_TARGET",
                        "detail": f"Referenced DC key '{mk}' not found in any objects mapping",
                    })

                # Check expected refs that are present in Cloud
                matched = expected_cloud_ids & actual_cloud_ids
                type_report["refs_ok"] += len(matched)

                # Broken refs: expected on Cloud but not present
                broken = expected_cloud_ids - actual_cloud_ids
                for bid in broken:
                    broken_dc_key = cloud_id_to_dc_key.get(bid, bid)
                    type_report["refs_broken"] += 1
                    type_report["issues"].append({
                        "dc_key": dc_key,
                        "attr": col,
                        "issue": "BROKEN_REF",
                        "detail": f"Expected ref to Cloud obj {bid} (DC: {broken_dc_key}) but not found on Cloud object",
                    })

                # Extra refs: present on Cloud but not expected from DC
                extra = actual_cloud_ids - expected_cloud_ids
                for eid in extra:
                    extra_dc_key = cloud_id_to_dc_key.get(eid, eid)
                    type_report["refs_extra"] += 1
                    type_report["issues"].append({
                        "dc_key": dc_key,
                        "attr": col,
                        "issue": "EXTRA_REF",
                        "detail": f"Cloud has ref to obj {eid} (DC: {extra_dc_key}) but not in DC source data",
                    })

        # Type summary
        ok = type_report["refs_ok"]
        total = type_report["refs_expected"]
        missing = type_report["refs_missing_target"]
        broken = type_report["refs_broken"]
        extra = type_report["refs_extra"]
        status = "OK" if broken == 0 and extra == 0 else "ISSUES"
        print(f"    refs: {ok}/{total} OK, {missing} missing targets, "
              f"{broken} broken, {extra} extra -> {status}")

        # Aggregate
        report["summary"]["total_refs_expected"] += total
        report["summary"]["total_refs_ok"] += ok
        report["summary"]["total_refs_missing_target"] += missing
        report["summary"]["total_refs_broken"] += broken
        report["summary"]["total_refs_extra"] += extra

        # Only include issues in report (keep it manageable)
        if type_report["issues"]:
            report["types"].append(type_report)
        else:
            # Include but without issues array to keep report clean
            report["types"].append({k: v for k, v in type_report.items() if k != "issues"})

    return report


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Exhaustive consistency check for migrated Jira Assets obj-ref relationships."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--schema-name", help="Check a single schema by name")
    group.add_argument("--all", action="store_true", help="Check all migrated schemas")
    parser.add_argument("--exports-dir", default="exports")
    parser.add_argument("--mappings-dir", default="mappings")
    args = parser.parse_args()

    global EXPORTS_DIR, MAPPINGS_DIR
    EXPORTS_DIR = Path(args.exports_dir)
    MAPPINGS_DIR = Path(args.mappings_dir)

    config.validate()

    schemas_to_check = []
    if args.all:
        for f in MAPPINGS_DIR.glob("*_mapping.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                schemas_to_check.append(data["schemaName"])
            except Exception:
                pass
        if not schemas_to_check:
            print("No migrated schemas found in mappings/.")
            sys.exit(1)
        print(f"Checking {len(schemas_to_check)} schema(s): {schemas_to_check}")
    else:
        schemas_to_check = [args.schema_name]

    all_reports = []
    grand_summary = {
        "total_objects": 0,
        "total_refs_expected": 0,
        "total_refs_ok": 0,
        "total_refs_missing_target": 0,
        "total_refs_broken": 0,
        "total_refs_extra": 0,
    }

    print(f"\n{'='*60}")
    print(f"  CONSISTENCY CHECK")
    print(f"{'='*60}")

    for schema_name in schemas_to_check:
        report = check_schema_consistency(schema_name)
        all_reports.append(report)

        if "error" not in report:
            # Save per-schema report
            artifacts = SchemaArtifacts(schema_name, EXPORTS_DIR, MAPPINGS_DIR)
            schema_dir = artifacts.schema_dir
            schema_dir.mkdir(parents=True, exist_ok=True)
            report_file = schema_dir / "consistency_report.json"
            report_file.write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            record_phase_outcome(
                artifacts,
                "check",
                "success" if not report["summary"]["total_refs_broken"]
                and not report["summary"]["total_refs_extra"] else "warning",
                inputs=[artifacts.mapping_file, artifacts.objects_file, artifacts.attr_meta_file],
                counts=report["summary"],
                errors=[
                    {
                        "classification": "unresolved-reference",
                        "count": report["summary"]["total_refs_missing_target"],
                    },
                    {
                        "classification": "final-api-failure",
                        "count": report["summary"]["total_refs_broken"],
                    },
                ],
            )
            print(f"  Report saved: {report_file}")

            # Aggregate totals
            s = report.get("summary", {})
            for key in grand_summary:
                grand_summary[key] += s.get(key, 0)

    # Grand summary
    print(f"\n{'='*60}")
    print(f"  GRAND SUMMARY")
    print(f"{'='*60}")
    print(f"  Objects checked       : {grand_summary['total_objects']}")
    print(f"  References expected   : {grand_summary['total_refs_expected']}")
    print(f"  References OK         : {grand_summary['total_refs_ok']}")
    print(f"  Missing targets       : {grand_summary['total_refs_missing_target']}")
    print(f"     (DC object not migrated — cannot create reference)")
    print(f"  Broken references     : {grand_summary['total_refs_broken']}")
    print(f"     (Target exists but ref not on Cloud object — DATA LOSS)")
    print(f"  Extra references      : {grand_summary['total_refs_extra']}")
    print(f"     (Ref on Cloud but not in DC source — unexpected)")

    broken = grand_summary["total_refs_broken"]
    extra = grand_summary["total_refs_extra"]
    if broken == 0 and extra == 0:
        print(f"\n  [OK] ALL REFERENCES CONSISTENT")
        print(f"    (missing targets are expected: source objects weren't migrated)")
    else:
        print(f"\n  [ERR] INCONSISTENCIES DETECTED")
        if broken > 0:
            print(f"    {broken} broken references need investigation")
        if extra > 0:
            print(f"    {extra} extra references need investigation")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
