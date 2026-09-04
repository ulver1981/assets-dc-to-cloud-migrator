"""
validate_migration.py

Validates a completed migration by comparing DC object counts and a random
sample of attribute values against Cloud.

Usage:
    python validate_migration.py --schema-name "Example Inventory"
    python validate_migration.py --schema-name "Example Inventory" --sample 10

Output:
    Printed validation report (stdout)
    exports/<schema_name>/validation_report.json
"""

import argparse
import json
import random
import time
from pathlib import Path

import requests

import config
from migration_artifacts import (
    ArtifactError,
    SchemaArtifacts,
    csv_inventory,
    read_json,
    record_phase_outcome,
)
from migration_http import request_json

MAPPINGS_DIR = Path("mappings")
EXPORTS_DIR = Path("exports")

# Shared session for connection pooling
_session = requests.Session()


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def dc_get(path: str, params: dict = None, retries: int = 3):
    return request_json(
        _session,
        "GET",
        f"{config.DC_BASE_URL}{path}",
        headers=config.dc_headers(),
        params=params,
        timeout=30,
        retries=retries,
    )


def cloud_get(path: str, params: dict = None, retries: int = 3):
    return request_json(
        _session,
        "GET",
        f"{config.CLOUD_API_BASE}{path}",
        auth=config.cloud_auth(),
        headers=config.cloud_headers(),
        params=params,
        timeout=30,
        retries=retries,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Count helpers
# ─────────────────────────────────────────────────────────────────────────────

def dc_count(dc_schema_id: int, dc_type_id: int) -> int:
    data = dc_get("/iql/objects", params={
        "iql": f"objectTypeId = {dc_type_id}",
        "objectSchemaId": dc_schema_id,
        "resultsPerPage": 1,
        "page": 1,
    })
    return data.get("totalFilterCount", 0)


def cloud_count(cloud_schema_id: str, cloud_type_id: str) -> int:
    try:
        data = cloud_get("/iql/objects", params={
            "iql": f"objectTypeId = {cloud_type_id}",
            "resultsPerPage": 1,
        })
        return data.get("totalFilterCount", 0)
    except Exception:
        return -1  # API error


# ─────────────────────────────────────────────────────────────────────────────
# Sample helpers
# ─────────────────────────────────────────────────────────────────────────────

def dc_get_sample_objects(dc_schema_id: int, dc_type_id: int, n: int) -> list:
    """Get up to n random objects from DC."""
    data = dc_get("/iql/objects", params={
        "iql": f"objectTypeId = {dc_type_id}",
        "objectSchemaId": dc_schema_id,
        "resultsPerPage": n,
        "page": 1,
        "includeAttributes": "true",
    })
    return data.get("objectEntries", [])


def dc_get_attr_name_map(dc_type_id: int) -> dict:
    data = dc_get(f"/objecttype/{dc_type_id}/attributes")
    attrs = data if isinstance(data, list) else []
    return {str(a["id"]): a["name"] for a in attrs}


def cloud_find_object_by_key(dc_key: str, cloud_type_id: str) -> dict | None:
    """Find a Cloud object that was imported from DC_Key."""
    try:
        data = cloud_get("/iql/objects", params={
            "iql": f'"DC_Key" = "{dc_key}"',
            "objectTypeId": cloud_type_id,
            "resultsPerPage": 1,
            "includeAttributes": "true",
        })
        entries = data.get("objectEntries", [])
        return entries[0] if entries else None
    except Exception:
        return None


def flatten_dc_attr_values(obj: dict, attr_name_map: dict) -> dict:
    """Returns {attr_name: [values]} from a DC object."""
    result = {}
    for attr in obj.get("attributes", []):
        attr_id = str(attr.get("objectTypeAttributeId", ""))
        name = attr_name_map.get(attr_id, attr_id)
        values = attr.get("objectAttributeValues") or attr.get("values") or []
        result[name] = sorted(
            v.get("displayValue") or v.get("value") or "" for v in values
        )
    return result


def flatten_cloud_attr_values(obj: dict) -> dict:
    """Returns {attr_name: [values]} from a Cloud object."""
    result = {}
    for attr in obj.get("attributes", []):
        ota = attr.get("objectTypeAttribute") or {}
        name = ota.get("name") or str(attr.get("objectTypeAttributeId", ""))
        values = attr.get("objectAttributeValues") or []
        result[name] = sorted(
            str(v.get("displayValue") or v.get("value") or "") for v in values
        )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Main validation logic
# ─────────────────────────────────────────────────────────────────────────────

def validate(schema_name: str, exports_dir: Path,
             mappings_dir: Path, sample_size: int = 5):
    config.validate()

    artifacts = SchemaArtifacts(schema_name, exports_dir, mappings_dir)
    mapping_file = artifacts.mapping_file
    schema_dir = artifacts.schema_dir
    struct_file = artifacts.structure_file

    if not mapping_file.exists():
        print(f"ERROR: {mapping_file} not found. Run import-schema first.")
        return
    if not struct_file.exists():
        print(f"ERROR: {struct_file} not found. Run export-schema first.")
        return

    try:
        mapping = read_json(mapping_file)
        struct = read_json(struct_file)
    except ArtifactError as exc:
        print(f"ERROR: {exc}")
        return
    if not isinstance(mapping, dict) or not isinstance(struct, dict):
        print("ERROR: Mapping and schema structure must be JSON objects.")
        return

    dc_schema_id = int(struct["schema"]["id"])
    cloud_schema_id = str(mapping["cloudSchemaId"])
    type_map: dict = mapping.get("objectTypeMapping", {})

    print(f"\n{'='*60}")
    print(f"  VALIDATE  '{schema_name}'")
    print(f"  DC schema: {dc_schema_id}  |  Cloud schema: {cloud_schema_id}")
    print(f"{'='*60}\n")

    type_name_map = {ot["id"]: ot["name"] for ot in struct["objectTypes"]}
    report = {"schema": schema_name, "types": []}
    total_ok = 0
    total_warn = 0

    for dc_type_id_raw, cloud_type_id in type_map.items():
        dc_type_id = int(dc_type_id_raw)
        ot_name = type_name_map.get(dc_type_id, str(dc_type_id))
        print(f"  [{ot_name}]", end=" ")

        # Count check
        dc_c = dc_count(dc_schema_id, dc_type_id)
        cloud_c = cloud_count(cloud_schema_id, str(cloud_type_id))
        count_ok = (dc_c == cloud_c)
        status = "OK" if count_ok else "MISMATCH"
        print(f"DC={dc_c}  Cloud={cloud_c}  → {status}")

        type_report = {
            "name": ot_name,
            "dc_count": dc_c,
            "cloud_count": cloud_c,
            "count_match": count_ok,
            "sample_mismatches": [],
        }

        # Sample attribute check
        if dc_c > 0 and sample_size > 0:
            attr_name_map = dc_get_attr_name_map(dc_type_id)
            sample_objs = dc_get_sample_objects(dc_schema_id, dc_type_id,
                                                min(sample_size * 3, 50))
            sampled = random.sample(sample_objs, min(sample_size, len(sample_objs)))

            for dc_obj in sampled:
                dc_key = dc_obj.get("objectKey", "")
                cloud_obj = cloud_find_object_by_key(dc_key, str(cloud_type_id))
                if not cloud_obj:
                    type_report["sample_mismatches"].append({
                        "dc_key": dc_key,
                        "issue": "Object not found in Cloud",
                    })
                    print(f"    MISSING  {dc_key}")
                    continue

                dc_vals = flatten_dc_attr_values(dc_obj, attr_name_map)
                cloud_vals = flatten_cloud_attr_values(cloud_obj)

                diffs = []
                for attr_name, dv in dc_vals.items():
                    if attr_name in ("Name", "Key", "Created", "Updated"):
                        continue
                    cv = cloud_vals.get(attr_name, [])
                    if dv != cv:
                        diffs.append({"attr": attr_name, "dc": dv, "cloud": cv})

                if diffs:
                    type_report["sample_mismatches"].append({
                        "dc_key": dc_key,
                        "diffs": diffs,
                    })
                    print(f"    DIFF     {dc_key}: {len(diffs)} attr(s) differ")
                else:
                    print(f"    OK       {dc_key}")
                time.sleep(0.1)

        report["types"].append(type_report)
        if count_ok and not type_report["sample_mismatches"]:
            total_ok += 1
        else:
            total_warn += 1

    # Save report
    report_file = schema_dir / "validation_report.json"
    schema_dir.mkdir(parents=True, exist_ok=True)
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    csv_files = [entry.path for entry in csv_inventory(artifacts.csv_dir)]
    record_phase_outcome(
        artifacts,
        "validate",
        "success" if total_warn == 0 else "warning",
        inputs=[mapping_file, struct_file, *csv_files],
        counts={"typesOk": total_ok, "typesWarning": total_warn},
        warnings=[
            {"classification": "source-data", "type": item["name"]}
            for item in report["types"]
            if not item["count_match"] or item["sample_mismatches"]
        ],
    )

    print(f"\n{'='*60}")
    print(f"  Types OK     : {total_ok}")
    print(f"  Types WARN   : {total_warn}")
    print(f"  Report saved : {report_file}")
    print(f"{'='*60}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Validate a Jira Assets migration.")
    parser.add_argument("--schema-name", required=True)
    parser.add_argument("--exports-dir", default="exports")
    parser.add_argument("--mappings-dir", default="mappings")
    parser.add_argument("--sample", type=int, default=5,
                        help="Number of random objects to sample per type (default: 5)")
    args = parser.parse_args()

    validate(
        args.schema_name,
        Path(args.exports_dir),
        Path(args.mappings_dir),
        sample_size=args.sample,
    )


if __name__ == "__main__":
    main()
