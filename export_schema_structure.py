"""
export_schema_structure.py

Exports a Jira DC Assets schema structure (object types + attributes) to JSON.

Usage:
    python export_schema_structure.py --schema-id 7

Output:
    exports/<schema_name>/schema_structure.json
    exports/<schema_name>/export_warnings.json   (cross-schema / unresolved refs)
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests

import config
from migration_artifacts import SchemaArtifacts, record_phase_outcome
from migration_http import request_json

# Shared session for connection pooling
_session = requests.Session()


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def dc_get(path: str, params: dict = None, retries: int = 3):
    """GET from DC Insight API with retry on 429 / 5xx."""
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
# Schema export helpers
# ─────────────────────────────────────────────────────────────────────────────

def fetch_schema(schema_id: int) -> dict:
    return dc_get(f"/objectschema/{schema_id}")


def fetch_object_types(schema_id: int) -> list:
    """Returns flat list of all object types in the schema."""
    data = dc_get(f"/objectschema/{schema_id}/objecttypes")
    return data if isinstance(data, list) else data.get("objectTypes", [])


def fetch_attributes_for_type(type_id: int) -> list:
    """Fetch attribute definitions for a single object type."""
    data = dc_get(f"/objecttype/{type_id}/attributes")
    return data if isinstance(data, list) else data.get("objectTypeAttributes", [])


def fetch_single_attribute(attr_id: int):
    """Fetch a single attribute definition by ID (used as fallback)."""
    try:
        return dc_get(f"/objecttypeattribute/{attr_id}")
    except Exception:
        return None


def _extract_id(val):
    """Extract numeric ID from int, dict {id: X}, or digit string."""
    if isinstance(val, dict):
        return val.get("id")
    if isinstance(val, str):
        return int(val) if val.isdigit() else None
    return val


def resolve_type_value(attr: dict, attr_id: int):
    """
    For type=1 (Object-reference) attributes, resolve the referenced DC type ID.
    Tries 4 levels: objectType.id → typeValue → individual fetch typeValue
    → individual fetch additionalValue/referenceObjectTypeId.
    """
    # Level 1: inline objectType.id
    obj_type = attr.get("objectType")
    if isinstance(obj_type, dict) and obj_type.get("id"):
        return int(obj_type["id"])

    # Level 2: typeValue directly
    tv = _extract_id(attr.get("typeValue"))
    if tv is not None:
        return int(tv)

    # Level 3 & 4: fetch individually
    single = fetch_single_attribute(attr_id)
    if single:
        for field in ("typeValue", "additionalValue", "referenceObjectTypeId"):
            val = _extract_id(single.get(field))
            if val is not None:
                return int(val)
        ot = single.get("objectType")
        if isinstance(ot, dict) and ot.get("id"):
            return int(ot["id"])

    return None


def build_attribute_record(attr: dict) -> dict:
    """Normalise a DC attribute dict into canonical storage format."""
    attr_type = attr.get("type", 0)
    if isinstance(attr_type, dict):
        attr_type = attr_type.get("id", 0)

    default_type = attr.get("defaultType")
    if isinstance(default_type, dict):
        default_type = default_type.get("id")

    options = attr.get("options", "")
    if options and isinstance(options, str):
        options_list = [o.strip() for o in options.split(",") if o.strip()]
    elif isinstance(options, list):
        options_list = [o for o in options if isinstance(o, str)]
    else:
        options_list = []

    return {
        "id": attr["id"],
        "name": attr["name"],
        "type": int(attr_type),
        "defaultType": default_type,
        "typeValue": None,
        "minimumCardinality": attr.get("minimumCardinality", 0),
        "maximumCardinality": attr.get("maximumCardinality", 1),
        "unique": attr.get("unique", False) or attr.get("uniqueAttribute", False),
        "options": options_list,
        "description": attr.get("description", ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main export logic
# ─────────────────────────────────────────────────────────────────────────────

def export_schema(schema_id: int, output_dir: Path) -> dict:
    config.validate()

    print(f"\n{'='*60}")
    print(f"  EXPORT SCHEMA  id={schema_id}")
    print(f"{'='*60}\n")

    # 1. Fetch schema metadata
    print("[1/4] Fetching schema metadata …")
    schema_meta = fetch_schema(schema_id)
    schema_name = schema_meta.get("name", f"schema_{schema_id}")
    print(f"      Name: {schema_name}")
    print(f"      Object count (approx): {schema_meta.get('objectCount', '?')}")

    out_path = output_dir / schema_name
    out_path.mkdir(parents=True, exist_ok=True)

    # 2. Fetch all object types
    print("\n[2/4] Fetching object types …")
    raw_types = fetch_object_types(schema_id)
    print(f"      Found {len(raw_types)} object type(s)")

    type_id_to_name = {int(t["id"]): t["name"] for t in raw_types}

    # 3. Fetch attributes for each type
    print("\n[3/4] Fetching attributes for each object type …")
    object_types = []
    warnings = []

    for ot in raw_types:
        ot_id = int(ot["id"])
        ot_name = ot["name"]
        parent_id = None
        if ot.get("parentObjectTypeId"):
            parent_id = int(ot["parentObjectTypeId"])
        elif ot.get("parentTypeId"):
            parent_id = int(ot["parentTypeId"])

        print(f"      [{ot_id}] {ot_name} …", end=" ", flush=True)
        raw_attrs = fetch_attributes_for_type(ot_id)
        print(f"{len(raw_attrs)} attributes")

        attributes = []
        for attr in raw_attrs:
            rec = build_attribute_record(attr)

            if rec["type"] == 1:
                tv = resolve_type_value(attr, rec["id"])
                rec["typeValue"] = tv
                if tv is None:
                    warnings.append({
                        "objectTypeId": ot_id,
                        "objectTypeName": ot_name,
                        "attributeId": rec["id"],
                        "attributeName": rec["name"],
                        "issue": "Cannot resolve typeValue",
                    })
                    print(f"        ! WARNING: attr '{rec['name']}' (id={rec['id']}) – typeValue unresolvable")
                elif tv not in type_id_to_name:
                    warnings.append({
                        "objectTypeId": ot_id,
                        "objectTypeName": ot_name,
                        "attributeId": rec["id"],
                        "attributeName": rec["name"],
                        "referencedDcTypeId": tv,
                        "issue": "Cross-schema reference: target type belongs to a different schema",
                    })
                    print(f"        ! WARNING: attr '{rec['name']}' references external DC type {tv}")

            attributes.append(rec)
            time.sleep(0.05)

        # Extract icon info (DC returns {id, name, url16, url48})
        dc_icon = ot.get("icon") or {}
        icon_record = {
            "id": dc_icon.get("id"),
            "name": dc_icon.get("name", ""),
        } if dc_icon.get("id") else None

        object_types.append({
            "id": ot_id,
            "name": ot_name,
            "parentTypeId": parent_id,
            "icon": icon_record,
            "description": ot.get("description", ""),
            "attributes": attributes,
        })

    # 4. Save output
    print("\n[4/4] Saving output …")
    schema_doc = {
        "schema": {
            "id": schema_id,
            "name": schema_name,
            "objectSchemaKey": schema_meta.get("objectSchemaKey", ""),
            "description": schema_meta.get("description", ""),
            "objectCount": schema_meta.get("objectCount", 0),
        },
        "objectTypes": object_types,
    }

    struct_file = out_path / "schema_structure.json"
    with open(struct_file, "w", encoding="utf-8") as f:
        json.dump(schema_doc, f, indent=2, ensure_ascii=False)
    print(f"      Saved: {struct_file}")

    if warnings:
        warn_file = out_path / "export_warnings.json"
        with open(warn_file, "w", encoding="utf-8") as f:
            json.dump(warnings, f, indent=2, ensure_ascii=False)
        print(f"      Warnings ({len(warnings)}): {warn_file}")
        cross = sum(1 for w in warnings if "Cross-schema" in w.get("issue", ""))
        if cross:
            print(f"      {cross} cross-schema refs: will need another schema imported first")
    else:
        print("      No warnings – all references resolved within this schema.")

    artifacts = SchemaArtifacts(schema_name, output_dir, Path("mappings"))
    record_phase_outcome(
        artifacts,
        "export-schema",
        "warning" if warnings else "success",
        inputs=[struct_file],
        counts={"objectTypes": len(object_types), "warnings": len(warnings)},
        warnings=[
            {"classification": "unresolved-reference", **warning}
            for warning in warnings
        ],
    )
    print(f"\nDone. Exported {len(object_types)} object types.")
    return schema_doc


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Export a Jira DC Assets schema structure to JSON.")
    parser.add_argument("--schema-id", type=int, required=True, help="Jira DC schema numeric ID")
    parser.add_argument("--output-dir", default="exports",
                        help="Base output directory (default: ./exports)")
    args = parser.parse_args()
    export_schema(args.schema_id, Path(args.output_dir))


if __name__ == "__main__":
    main()
