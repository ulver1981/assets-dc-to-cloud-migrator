"""
export_data_csv.py

Exports all objects of a Jira DC Assets schema to CSV files.

Usage:
    python export_data_csv.py --schema-id 7

Output:
    exports/<schema_name>/csv/<TypeName>.csv
    exports/<schema_name>/_attr_meta.json    (attribute metadata for import)

Key improvements vs old version:
  - CSV headers are ATTRIBUTE NAMES (not numeric IDs)  [bug fix]
  - Object-reference values export the DC object KEY (e.g. IDS-12345),
    not the display name  [bug fix – enables reliable Cloud import]
  - Saves _attr_meta.json so import knows which columns are object-refs
  - Robust pagination with retry on 429 / 5xx
"""

import argparse
import csv
import json
import time
from pathlib import Path

import requests

import config
from migration_artifacts import (
    SchemaArtifacts,
    csv_inventory,
    record_phase_outcome,
)
from migration_http import request_json

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


# ─────────────────────────────────────────────────────────────────────────────
# Attribute metadata helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_attr_meta_for_type(type_id: int) -> dict:
    """
    Returns {attr_id_str: {"name": ..., "type": ..., "typeValue": ...}}
    for all attributes of an object type.
    """
    data = dc_get(f"/objecttype/{type_id}/attributes")
    attrs = data if isinstance(data, list) else data.get("objectTypeAttributes", [])
    result = {}
    for a in attrs:
        attr_type = a.get("type", 0)
        if isinstance(attr_type, dict):
            attr_type = attr_type.get("id", 0)
        tv = a.get("typeValue")
        if isinstance(tv, dict):
            tv = tv.get("id")
        result[str(a["id"])] = {
            "name": a["name"],
            "type": int(attr_type),
            "typeValue": int(tv) if tv is not None else None,
            "maximumCardinality": int(a.get("maximumCardinality", 1)),
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Object paging
# ─────────────────────────────────────────────────────────────────────────────

def get_all_objects(schema_id: int, type_id: int) -> list:
    """Paginate through all objects of a type using IQL."""
    all_objects = []
    page = 1
    page_size = 500
    while True:
        data = dc_get("/iql/objects", params={
            "iql": f"objectTypeId = {type_id}",
            "objectSchemaId": schema_id,
            "page": page,
            "resultsPerPage": page_size,
            "includeAttributes": "true",
        })
        entries = data.get("objectEntries", [])
        all_objects.extend(entries)
        total = data.get("totalFilterCount", 0)
        if not entries or len(all_objects) >= total:
            break
        page += 1
    return all_objects


# ─────────────────────────────────────────────────────────────────────────────
# Per-type export
# ─────────────────────────────────────────────────────────────────────────────

def export_type(schema_id: int, ot: dict, out_csv_dir: Path,
                all_attr_meta: dict, safe_name: str = "") -> dict:
    """
    Export one object type to a CSV file.
    all_attr_meta accumulates {safe_name: {attr_name: meta}} across all types.

    Returns the per-type attr_meta dict.
    """
    ot_id = int(ot["id"])
    ot_name = ot["name"]
    if not safe_name:
        safe_name = ot_name.replace("/", "_")

    # 1. Get attribute metadata (ID → name + type)
    print(f"    [{ot_id}] '{ot_name}' – fetching attributes …", end=" ", flush=True)
    id_to_meta = get_attr_meta_for_type(ot_id)  # {attr_id_str: {name, type, typeValue}}
    id_to_name = {k: v["name"] for k, v in id_to_meta.items()}
    print(f"{len(id_to_meta)} attributes")

    # Store meta by name for the _attr_meta.json file
    type_attr_meta = {
        v["name"]: {
            "type": v["type"],
            "typeValue": v["typeValue"],
            "maximumCardinality": v.get("maximumCardinality", 1),
        }
        for v in id_to_meta.values()
    }
    all_attr_meta[safe_name] = type_attr_meta

    # 2. Fetch all objects
    print(f"    [{ot_id}] '{ot_name}' – fetching objects …", end=" ", flush=True)
    objects = get_all_objects(schema_id, ot_id)
    print(f"{len(objects)} objects")
    if not objects:
        return type_attr_meta

    # 3. Collect all attribute names in order of first appearance
    # Always include "Name" first (from top-level obj["name"], not from attributes)
    ordered_attr_names: list = ["Name"]
    seen_names: set = {"Name"}
    for obj in objects:
        for attr in obj.get("attributes", []):
            attr_id = str(attr.get("objectTypeAttributeId", ""))
            name = id_to_name.get(attr_id)
            if not name:
                # fallback: embedded objectTypeAttribute
                ota = attr.get("objectTypeAttribute")
                name = ota["name"] if isinstance(ota, dict) else None
            if name and name not in seen_names:
                ordered_attr_names.append(name)
                seen_names.add(name)

    # 4. Write CSV
    csv_file = out_csv_dir / f"{safe_name}.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["DC_Key"] + ordered_attr_names,
                                extrasaction="ignore")
        writer.writeheader()
        for obj in objects:
            # Always capture the top-level display name (not available in attributes)
            obj_name = obj.get("name") or obj.get("label") or ""
            row: dict = {"DC_Key": obj.get("objectKey", ""), "Name": obj_name}
            for attr in obj.get("attributes", []):
                attr_id = str(attr.get("objectTypeAttributeId", ""))
                name = id_to_name.get(attr_id)
                if not name:
                    ota = attr.get("objectTypeAttribute")
                    name = ota["name"] if isinstance(ota, dict) else None
                if not name:
                    continue

                values = attr.get("objectAttributeValues") or attr.get("values") or []
                attr_meta = id_to_meta.get(attr_id, {})
                is_obj_ref = int(attr_meta.get("type", 0)) == 1

                parts = []
                for v in values:
                    if is_obj_ref:
                        # Export the DC object KEY (e.g. IDS-12345), not display name.
                        # This is stable and allows reliable resolution on import.
                        ref_key = (
                            v.get("referencedObject", {}).get("objectKey")
                            or v.get("objectKey")
                            or v.get("value")
                            or ""
                        )
                        parts.append(ref_key)
                    else:
                        parts.append(
                            v.get("displayValue") or v.get("value") or ""
                        )
                row[name] = " | ".join(p for p in parts if p)
            writer.writerow(row)

    print(f"    -> {csv_file}")
    return type_attr_meta


# ─────────────────────────────────────────────────────────────────────────────
# Main export logic
# ─────────────────────────────────────────────────────────────────────────────

def export_data(schema_id: int, output_dir: Path):
    config.validate()

    print(f"\n{'='*60}")
    print(f"  EXPORT DATA  schema_id={schema_id}")
    print(f"{'='*60}\n")

    # 1. Get schema name
    print("[1/3] Fetching schema metadata …")
    schema_meta = dc_get(f"/objectschema/{schema_id}")
    schema_name = schema_meta.get("name", f"schema_{schema_id}")
    print(f"      Name: {schema_name}")

    out_csv_dir = output_dir / schema_name / "csv"
    out_csv_dir.mkdir(parents=True, exist_ok=True)

    # 2. List object types
    print("\n[2/3] Fetching object types …")
    raw_types = dc_get(f"/objectschema/{schema_id}/objecttypes")
    if not isinstance(raw_types, list):
        raw_types = raw_types.get("objectTypes", [])
    print(f"      Found {len(raw_types)} object type(s)")

    # 3. Detect duplicate type names and build safe_name map
    from collections import Counter
    name_counts = Counter(t["name"] for t in raw_types)
    id_to_safe: dict = {}
    for ot in raw_types:
        base = ot["name"].replace("/", "_")
        if name_counts[ot["name"]] > 1:
            id_to_safe[int(ot["id"])] = f"{base}__{ot['id']}"
        else:
            id_to_safe[int(ot["id"])] = base
    dupes = {n for n, c in name_counts.items() if c > 1}
    if dupes:
        print(f"      Duplicate type names detected: {dupes}")
        print(f"      Using __dcTypeId suffix for disambiguation")

    # 4. Export each type
    print("\n[3/3] Exporting objects …")
    all_attr_meta: dict = {}
    total_objects = 0
    for ot in raw_types:
        safe_name = id_to_safe[int(ot["id"])]
        type_meta = export_type(schema_id, ot, out_csv_dir, all_attr_meta, safe_name)
        csv_path = out_csv_dir / f"{safe_name}.csv"
        if csv_path.exists():
            with open(csv_path, encoding="utf-8") as cf:
                total_objects += sum(1 for _ in cf) - 1  # subtract header
        time.sleep(0.1)

    # 4. Save attribute metadata
    meta_file = output_dir / schema_name / "_attr_meta.json"
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(all_attr_meta, f, indent=2, ensure_ascii=False)
    artifacts = SchemaArtifacts(schema_name, output_dir, Path("mappings"))
    csv_files = [entry.path for entry in csv_inventory(out_csv_dir)]
    record_phase_outcome(
        artifacts,
        "export-data",
        "success",
        inputs=[meta_file, *csv_files],
        counts={"csvFiles": len(csv_files), "objectsExported": total_objects},
    )
    print(f"\nAttribute metadata saved: {meta_file}")
    print(f"CSV files saved in: {out_csv_dir}")
    print(f"Total objects exported: {total_objects}")
    print(f"Done.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Export Jira DC Assets object data to CSV.")
    parser.add_argument("--schema-id", type=int, required=True, help="Jira DC schema numeric ID")
    parser.add_argument("--output-dir", default="exports",
                        help="Base output directory (default: ./exports)")
    args = parser.parse_args()
    export_data(args.schema_id, Path(args.output_dir))


if __name__ == "__main__":
    main()
