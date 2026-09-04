"""
import_schema_structure.py

Imports a schema structure (from exports/<schema_name>/schema_structure.json)
into Jira Cloud Assets.

Cross-schema Object-reference attributes are resolved by loading mapping files
from mappings/ (previously imported schemas).  Unresolved refs are logged to
exports/<schema_name>/unresolved_refs.json for a second pass after importing
the prerequisite schema.

Usage:
    python import_schema_structure.py --schema-name "Example Inventory"
    python import_schema_structure.py --schema-name "Example Inventory" --dry-run

Output:
    mappings/<schema_name>_mapping.json     DC typeId → Cloud typeId + schemaId
    exports/<schema_name>/unresolved_refs.json   (if any cross-schema refs unresolvable)
"""

import argparse
import csv
import json
import time
from pathlib import Path

import requests

import config
from migration_artifacts import (
    ArtifactError,
    SchemaArtifacts,
    assert_unique_csv_stems,
    csv_inventory,
    read_json,
    record_phase_outcome,
    safe_schema_name,
    write_json,
)
from migration_http import request_json

RESERVED_ATTRS = {
    "name", "key", "created", "updated", "archived",
    "created by", "updated by",
}

MAPPINGS_DIR = Path("mappings")
EXPORTS_DIR = Path("exports")

# Shared session for connection pooling
_session = requests.Session()



# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def cloud_get(path: str, retries: int = 3):
    return request_json(
        _session,
        "GET",
        f"{config.CLOUD_API_BASE}{path}",
        auth=config.cloud_auth(),
        headers=config.cloud_headers(),
        timeout=30,
        retries=retries,
    )


def cloud_post(path: str, payload: dict, retries: int = 3):
    return request_json(
        _session,
        "POST",
        f"{config.CLOUD_API_BASE}{path}",
        auth=config.cloud_auth(),
        headers=config.cloud_headers(),
        payload=payload,
        timeout=30,
        retries=retries,
        success_statuses={200, 201},
    )


def cloud_delete(path: str, retries: int = 3):
    return request_json(
        _session,
        "DELETE",
        f"{config.CLOUD_API_BASE}{path}",
        auth=config.cloud_auth(),
        headers=config.cloud_headers(),
        timeout=30,
        retries=retries,
        success_statuses={200, 204},
    )


def dc_get(path: str, params: dict = None, retries: int = 3):
    """GET from DC Insight API (used by fix-refs for sample object queries)."""
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
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_all_mappings(mappings_dir: Path, exclude: str | None = None) -> dict:
    """
    Build a unified DC typeId → Cloud typeId dict from ALL mapping files in
    mappings/.  This lets us resolve cross-schema Object-refs automatically if
    the prerequisite schema has already been imported.

    Pass `exclude` (filename only, e.g. 'MySchema_mapping.json') to skip the
    current schema's own stale mapping and avoid using outdated Cloud IDs.
    """
    combined: dict = {}
    for f in mappings_dir.glob("*_mapping.json"):
        if exclude and f.name == exclude:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for dc_id, cloud_id in data.get("objectTypeMapping", {}).items():
                combined[int(dc_id)] = str(cloud_id)
        except Exception as e:
            print(f"    [warn] Could not load mapping {f}: {e}")
    return combined


def get_ref_type_uuid(schema_cloud_id: str) -> str | None:
    """Return the UUID/id of the first available reference type (prefers 'Reference')."""
    try:
        rts = cloud_get(f"/objectschema/{schema_cloud_id}/referencetypes")
    except Exception as e:
        print(f"    [warn] Could not fetch reference types: {e}")
        return None
    if not rts:
        return None
    for rt in rts:
        if rt.get("name", "").lower() == "reference":
            return rt["id"]
    return rts[0]["id"]


def safe_schema_key(name: str) -> str:
    """Derive a valid schema key (3–10 uppercase letters/digits) from schema name."""
    key = "".join(c for c in name.upper() if c.isalnum())[:10]
    return key or "SCH"


def fetch_cloud_icon_map() -> dict:
    """Return {icon_name_lower: cloud_icon_id} from the global Cloud icon catalog."""
    try:
        icons = cloud_get("/icon/global")
        return {ic["name"].lower(): str(ic["id"]) for ic in icons}
    except Exception as e:
        print(f"    [warn] Could not fetch Cloud icon catalog: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Creation helpers (skip in dry-run)
# ─────────────────────────────────────────────────────────────────────────────

def create_schema(name: str, description: str, schema_key: str, dry_run: bool) -> dict:
    if dry_run:
        return {"id": "DRY_RUN_SCHEMA_ID", "name": name}
    payload = {
        "name": name,
        "description": description,
        "objectSchemaKey": schema_key or safe_schema_key(name),
        "crossSchemaReferencingAllowed": True,
    }
    return cloud_post("/objectschema/create", payload)


def create_object_type(schema_cloud_id: str, name: str,
                       parent_cloud_id: str | None,
                       icon_id: str | None,
                       description: str | None,
                       dry_run: bool) -> dict:
    if dry_run:
        import random
        return {"id": f"DRY_{random.randint(1000, 9999)}", "name": name}
    payload = {
        "name": name,
        "objectSchemaId": schema_cloud_id,
        "iconId": icon_id or "1",
    }
    if parent_cloud_id:
        payload["parentObjectTypeId"] = parent_cloud_id
    if description:
        payload["description"] = description
    return cloud_post("/objecttype/create", payload)


def create_attribute(object_type_cloud_id: str, attr: dict,
                     dc_to_cloud: dict, ref_type_uuid: str | None,
                     dry_run: bool) -> dict | None:
    """
    Build and send the Cloud payload for one attribute.
    Returns the created attribute dict, or None if skipped.
    Raises on unexpected API errors.
    """
    attr_type = int(attr.get("type") or 0)
    payload: dict = {"name": attr["name"], "type": attr_type}

    # Preserve attribute description
    if attr.get("description"):
        payload["description"] = attr["description"]

    if attr_type == 0:
        dt = attr.get("defaultType")
        payload["defaultTypeId"] = int(dt) if dt is not None else 0

    elif attr_type == 1:
        tv = attr.get("typeValue")
        if tv is None:
            return None   # logged by caller
        cloud_ref = dc_to_cloud.get(int(tv))
        if not cloud_ref:
            return None   # logged by caller
        try:
            payload["typeValue"] = int(cloud_ref)
        except (ValueError, TypeError):
            payload["typeValue"] = cloud_ref  # dry-run placeholder (e.g. "DRY_1234")
        if ref_type_uuid:
            payload["additionalValue"] = str(ref_type_uuid)

    # Cardinality
    min_c = attr.get("minimumCardinality", 0)
    max_c = attr.get("maximumCardinality", 1)
    if min_c and min_c > 0:
        payload["minimumCardinality"] = min_c
    # DC uses -1 for unlimited; Cloud doesn't accept -1, use 100 instead
    if max_c == -1:
        payload["maximumCardinality"] = 100
    elif max_c and max_c > 1:
        payload["maximumCardinality"] = max_c

    # NOTE: do NOT set uniqueAttribute=True during migration – DC data often
    # has duplicate values that violate uniqueness constraints on Cloud.
    # if attr.get("unique"): payload["uniqueAttribute"] = True

    if dry_run:
        print(f"      [dry-run] Would create: {payload}")
        return {"id": "DRY_ATTR", "name": attr["name"]}

    result = cloud_post(f"/objecttypeattribute/{object_type_cloud_id}", payload)
    time.sleep(0.3)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Main import logic
# ─────────────────────────────────────────────────────────────────────────────

def import_schema(schema_name: str, exports_dir: Path,
                  mappings_dir: Path, dry_run: bool = False):
    if not dry_run:
        config.validate()

    schema_dir = exports_dir / schema_name
    struct_file = schema_dir / "schema_structure.json"
    if not struct_file.exists():
        print(f"ERROR: {struct_file} not found. Run export-schema first.")
        return

    with open(struct_file, encoding="utf-8") as f:
        data = json.load(f)

    print(f"\n{'='*60}")
    print(f"  IMPORT SCHEMA  '{schema_name}'  {'[DRY-RUN]' if dry_run else ''}")
    print(f"{'='*60}\n")

    # Load all existing type mappings (for cross-schema refs),
    # but exclude this schema's own stale mapping so parent types within
    # the current schema are resolved fresh (avoids 404 on re-import).
    if not dry_run:
        mappings_dir.mkdir(parents=True, exist_ok=True)
    safe_name = schema_name.replace(" ", "_").replace("/", "-")
    own_mapping_file = f"{safe_name}_mapping.json"
    all_dc_to_cloud = load_all_mappings(mappings_dir, exclude=own_mapping_file)
    print(f"[pre] Loaded {len(all_dc_to_cloud)} type mappings from {mappings_dir}/ (excluded own stale mapping)")

    # 1. Create schema
    print("\n[1/4] Creating schema …")
    schema_cloud = create_schema(
        data["schema"]["name"],
        data["schema"].get("description", ""),
        data["schema"].get("objectSchemaKey", ""),
        dry_run,
    )
    schema_cloud_id = schema_cloud["id"]
    print(f"      Cloud schema ID: {schema_cloud_id}")

    # 2. Get reference type UUID
    ref_type_uuid = None
    if not dry_run:
        ref_type_uuid = get_ref_type_uuid(schema_cloud_id)
        print(f"      Reference type UUID: {ref_type_uuid or '(none found)'}")
    else:
        ref_type_uuid = "DRY_REF_UUID"

    # 3. Create object types (topological sort – multi-pass)
    print("\n[2/4] Creating object types …")

    # Load Cloud icon catalog for icon resolution
    cloud_icon_map = {} if dry_run else fetch_cloud_icon_map()
    if cloud_icon_map:
        print(f"      Loaded {len(cloud_icon_map)} Cloud icons for resolution")

    def _resolve_icon(ot: dict) -> str | None:
        """Resolve DC icon name to Cloud icon ID, return None for default."""
        icon_data = ot.get("icon")
        if not icon_data:
            return None
        icon_name = icon_data.get("name", "") if isinstance(icon_data, dict) else ""
        if icon_name and cloud_icon_map:
            cloud_id = cloud_icon_map.get(icon_name.lower())
            if cloud_id:
                return str(cloud_id)
        return None

    types = data["objectTypes"]
    dc_to_cloud: dict = dict(all_dc_to_cloud)   # start with all known mappings
    remaining = list(types)
    passes = 0
    while remaining and passes <= len(types):
        passes += 1
        next_remaining = []
        for ot in remaining:
            dc_id = int(ot["id"])  # normalize to int
            parent_dc_id = ot.get("parentTypeId")
            icon_id = _resolve_icon(ot)
            ot_desc = ot.get("description", "")
            if parent_dc_id is None:
                cloud_type = create_object_type(schema_cloud_id, ot["name"], None, icon_id, ot_desc, dry_run)
                dc_to_cloud[dc_id] = str(cloud_type["id"])
                icon_tag = f" icon={icon_id}" if icon_id else ""
                print(f"      [root] '{ot['name']}' → Cloud ID {cloud_type['id']}{icon_tag}")
            elif int(parent_dc_id) in dc_to_cloud:
                parent_cloud_id = dc_to_cloud[int(parent_dc_id)]
                cloud_type = create_object_type(schema_cloud_id, ot["name"], parent_cloud_id, icon_id, ot_desc, dry_run)
                dc_to_cloud[dc_id] = str(cloud_type["id"])
                icon_tag = f" icon={icon_id}" if icon_id else ""
                print(f"      [L{passes}]   '{ot['name']}' → Cloud ID {cloud_type['id']}{icon_tag}")
            else:
                next_remaining.append(ot)
        remaining = next_remaining

    if remaining:
        print(f"\n  WARNING: {len(remaining)} object types could not be created (parent not found):")
        for ot in remaining:
            print(f"    - '{ot['name']}' (parentTypeId DC={ot.get('parentTypeId')})")

    # Build this-schema-only dc → cloud map (excluding external schemas)
    this_schema_dc_ids = {int(ot["id"]) for ot in types}
    this_schema_mapping = {
        k: v for k, v in dc_to_cloud.items()
        if k in this_schema_dc_ids
    }

    # 4. Create attributes
    print("\n[3/4] Creating attributes …")
    unresolved_refs: list = []
    attr_errors: list = []

    for ot in types:
        dc_id = int(ot["id"])
        cloud_type_id = dc_to_cloud.get(dc_id)
        if not cloud_type_id:
            print(f"\n  SKIP attributes for '{ot['name']}': object type not created")
            continue
        print(f"\n  [{dc_id}] '{ot['name']}':")

        # Always create DC_Key attribute first (used for idempotency in data import)
        dc_key_attr = {"name": "DC_Key", "type": 0, "defaultType": 0}
        try:
            result = create_attribute(cloud_type_id, dc_key_attr, dc_to_cloud, ref_type_uuid, dry_run)
            if result:
                print("    OK  'DC_Key' (idempotency key)")
        except Exception as e:
            print(f"    WARN could not create DC_Key attr: {e}")

        for attr in ot["attributes"]:
            if attr["name"].lower() in RESERVED_ATTRS:
                print(f"    skip reserved: '{attr['name']}'")
                continue

            try:
                result = create_attribute(cloud_type_id, attr, dc_to_cloud, ref_type_uuid, dry_run)
                if result is None:
                    tv = attr.get("typeValue")
                    resolved = dc_to_cloud.get(int(tv)) if tv else None
                    issue = (
                        "typeValue is null" if tv is None
                        else f"DC typeId {tv} not in any loaded mapping"
                    )
                    print(f"    SKIP Object-ref '{attr['name']}': {issue}")
                    unresolved_refs.append({
                        "objectTypeDcId": ot["id"],
                        "objectTypeName": ot["name"],
                        "attributeId": attr["id"],
                        "attributeName": attr["name"],
                        "referencedDcTypeId": tv,
                        "resolved": resolved,
                        "issue": issue,
                    })
                else:
                    print(f"    OK  '{attr['name']}'")
            except Exception as e:
                err = str(e)
                print(f"    ERROR '{attr['name']}': {err[:120]}")
                attr_errors.append({
                    "objectTypeName": ot["name"],
                    "attributeName": attr["name"],
                    "error": err,
                })

    # 5. Save mapping file
    print("\n[4/4] Saving mapping …")
    mapping_file = mappings_dir / own_mapping_file
    mapping_doc = {
        "schemaName": schema_name,
        "dcSchemaId": data["schema"]["id"],
        "cloudSchemaId": schema_cloud_id,
        "objectTypeMapping": this_schema_mapping,
    }
    if not dry_run:
        with open(mapping_file, "w", encoding="utf-8") as f:
            json.dump(mapping_doc, f, indent=2, ensure_ascii=False)
        print(f"      Saved: {mapping_file}")
    else:
        print(f"      [dry-run] Would save: {mapping_file}")

    if unresolved_refs:
        unres_file = schema_dir / "unresolved_refs.json"
        if not dry_run:
            with open(unres_file, "w", encoding="utf-8") as f:
                json.dump(unresolved_refs, f, indent=2, ensure_ascii=False)
        print(f"      Unresolved refs ({len(unresolved_refs)}): {unres_file}")
        print("      → Import prerequisite schema(s) first, then re-run this script")

    if attr_errors:
        err_file = schema_dir / "attribute_errors.json"
        if not dry_run:
            with open(err_file, "w", encoding="utf-8") as f:
                json.dump(attr_errors, f, indent=2, ensure_ascii=False)
        print(f"      Attribute errors ({len(attr_errors)}): {err_file}")

    if not dry_run:
        artifacts = SchemaArtifacts(schema_name, exports_dir, mappings_dir)
        record_phase_outcome(
            artifacts,
            "import-schema",
            "success" if not remaining and not unresolved_refs and not attr_errors else "warning",
            inputs=[struct_file, mapping_file],
            counts={
                "objectTypesCreated": len(types) - len(remaining),
                "unresolvedReferences": len(unresolved_refs),
                "attributeErrors": len(attr_errors),
            },
            warnings=[
                {"classification": "unresolved-reference", **item}
                for item in unresolved_refs
            ],
            errors=[
                {"classification": "final-api-failure", **item}
                for item in attr_errors
            ],
        )

    # Summary
    print(f"\n{'='*60}")
    created_types = len(types) - len(remaining)
    print(f"  Object types created : {created_types}/{len(types)}")
    print(f"  Unresolved obj-refs  : {len(unresolved_refs)}")
    print(f"  Attribute errors     : {len(attr_errors)}")
    if dry_run:
        print("  [DRY-RUN]: no changes made to Cloud")
    print(f"{'='*60}\n")

    return mapping_doc


# ─────────────────────────────────────────────────────────────────────────────
# Fix type-value references (DC API bug workaround)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_ref_via_dc_api(dc_type_id: int, attr_id: int):
    """Query DC for a sample object to discover the correct referenced type."""
    try:
        resp = dc_get(
            "/iql/objects",
            params={"iql": f"objectTypeId = {dc_type_id}", "page": 1,
                     "resultPerPage": 25, "includeAttributes": True},
        )
        for obj in resp.get("objectEntries", []):
            for attr in obj.get("attributes", []):
                if attr.get("objectTypeAttributeId") != attr_id:
                    continue
                for val in attr.get("objectAttributeValues", []):
                    ref = val.get("referencedObject")
                    if ref:
                        ot = ref.get("objectType")
                        if isinstance(ot, dict) and ot.get("id"):
                            return int(ot["id"])
    except Exception as e:
        print(f"      [DC-API] error querying type {dc_type_id} attr {attr_id}: {e}")
    return None


def _detect_correct_type_values(data: dict, csv_files: list[Path]):
    """
    Detect the correct typeValue for each obj-ref (type=1) attribute by:
      1. Analysing CSV data (cross-reference keys → types)
      2. Matching attribute name → object type name
      3. Querying DC API for a sample object (definitive fallback)

    Returns list of dicts with wrong_tv, correct_tv, method, etc.
    """
    type_map = {t["id"]: t["name"] for t in data["objectTypes"]}
    name_to_id = {t["name"]: t["id"] for t in data["objectTypes"]}
    # case-insensitive lookup
    name_lower_to_id = {t["name"].lower(): t["id"] for t in data["objectTypes"]}

    # Strategy 1 prep: build DC key → type name from all CSVs
    key_to_type_name: dict[str, str] = {}
    for csv_file in csv_files:
        try:
            with open(csv_file, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if "DC_Key" not in (reader.fieldnames or []):
                    continue
                tname = csv_file.stem
                if "__" in tname:
                    tname = tname.rsplit("__", 1)[0]
                for row in reader:
                    k = row.get("DC_Key", "").strip()
                    if k:
                        key_to_type_name[k] = tname
        except OSError as exc:
            print(f"      [CSV] cannot read {csv_file}: {exc}")

    fixes = []
    unresolved = []

    for t in data["objectTypes"]:
        type_id = t["id"]
        type_name = t["name"]

        # Find CSV file for this type
        candidates = [
            csv_file for csv_file in csv_files
            if csv_file.stem == type_name
            or csv_file.stem.startswith(f"{type_name}__")
        ]
        csv_path = candidates[0] if candidates else None

        csv_rows = None
        if csv_path and csv_path.exists():
            try:
                with open(csv_path, encoding="utf-8") as f:
                    csv_rows = list(csv.DictReader(f))
            except Exception:
                pass

        for a in t["attributes"]:
            if a["type"] != 1:
                continue
            current_tv = a.get("typeValue")
            if current_tv is None:
                continue

            correct_tv = None
            method = None

            # --- Strategy 1: CSV cross-reference ---
            if csv_rows:
                for row in csv_rows[:200]:
                    val = row.get(a["name"], "").strip()
                    if not val:
                        continue
                    for ref_key in val.split(","):
                        ref_key = ref_key.strip()
                        if ref_key in key_to_type_name:
                            ref_tname = key_to_type_name[ref_key]
                            correct_tv = name_to_id.get(ref_tname)
                            if correct_tv:
                                method = "CSV"
                                break
                    if correct_tv:
                        break

            # --- Strategy 2: attribute name → type name ---
            if not correct_tv:
                aname = a["name"]
                # Direct match, +s, -s
                for candidate in (aname, aname + "s", aname.rstrip("s")):
                    if candidate in name_to_id:
                        correct_tv = name_to_id[candidate]
                        method = "name-match"
                        break
                # Case-insensitive
                if not correct_tv:
                    cid = name_lower_to_id.get(aname.lower())
                    if cid:
                        correct_tv = cid
                        method = "name-match"
                # Fuzzy: attr name contained in type name, or vice versa
                if not correct_tv:
                    al = aname.lower()
                    for tname, tid in name_to_id.items():
                        tl = tname.lower()
                        if tid == type_id:
                            continue  # skip self
                        if al in tl or tl in al:
                            correct_tv = tid
                            method = "name-fuzzy"
                            break

            # --- Strategy 3: DC API sample query ---
            if not correct_tv:
                print(f"      [DC-API] querying sample for {type_name}.{a['name']} ...")
                correct_tv = _resolve_ref_via_dc_api(type_id, a["id"])
                if correct_tv:
                    method = "DC-API"

            if correct_tv and correct_tv != current_tv:
                fixes.append({
                    "dc_type_id": type_id,
                    "type_name": type_name,
                    "attr_id": a["id"],
                    "attr_name": a["name"],
                    "attr_def": a,
                    "wrong_tv": current_tv,
                    "correct_tv": correct_tv,
                    "correct_type_name": type_map.get(correct_tv, f"external({correct_tv})"),
                    "method": method,
                })
            elif not correct_tv or correct_tv == current_tv:
                if current_tv == type_id:
                    # Still self-referencing and we couldn't resolve it
                    unresolved.append(f"{type_name}.{a['name']} (attr {a['id']})")

    return fixes, unresolved


def fix_type_value_refs(schema_name: str, exports_dir: Path, mappings_dir: Path,
                        dry_run: bool = False):
    """
    Fix obj-ref attributes whose typeValue was incorrectly set by the DC API bug.

    The DC API returns typeValue = parent objectType ID for ALL obj-ref attributes
    of a type, instead of the actual referenced type ID. This causes all obj-ref
    attributes on Cloud to point to the wrong type after import-schema.

    For each wrong attribute: DELETE the Cloud attribute + POST a new one with
    the correct typeValue. After fix-refs, re-run import-data to link obj-refs.
    """
    artifacts = SchemaArtifacts(schema_name, exports_dir, mappings_dir)
    schema_dir = artifacts.schema_dir
    struct_file = artifacts.structure_file
    csv_dir = artifacts.csv_dir
    meta_file = artifacts.attr_meta_file

    if not struct_file.exists():
        print(f"ERROR: {struct_file} not found. Run export-schema first.")
        return

    mapping_file = artifacts.mapping_file
    if not mapping_file.exists():
        print(f"ERROR: {mapping_file} not found. Run import-schema first.")
        return
    if not meta_file.exists():
        print(f"ERROR: {meta_file} not found. Run export-data before fix-refs.")
        return
    csv_artifacts = csv_inventory(csv_dir)
    if not csv_artifacts:
        print(f"ERROR: No CSV files found in {csv_dir}. Run export-data before fix-refs.")
        return
    try:
        assert_unique_csv_stems(csv_artifacts)
        data = read_json(struct_file)
        mapping = read_json(mapping_file)
        read_json(meta_file)
    except ArtifactError as exc:
        print(f"ERROR: {exc}")
        return
    if not isinstance(data, dict) or not isinstance(mapping, dict):
        print("ERROR: Schema structure and mapping must be JSON objects.")
        return

    config.validate()

    dc_to_cloud = {int(k): str(v) for k, v in mapping["objectTypeMapping"].items()}
    # For target typeValue resolution we also need cross-schema mappings.
    # Current schema mappings override any stale/duplicate IDs.
    all_dc_to_cloud = load_all_mappings(mappings_dir, exclude=f"{safe_name}_mapping.json")
    all_dc_to_cloud.update(dc_to_cloud)
    cloud_schema_id = str(mapping["cloudSchemaId"])
    type_map = {t["id"]: t["name"] for t in data["objectTypes"]}

    print(f"\n{'='*60}")
    print(f"  FIX OBJ-REF TYPE VALUES  '{schema_name}'  {'[DRY-RUN]' if dry_run else ''}")
    print(f"{'='*60}\n")

    # --- Phase 1: detect correct typeValues ---
    print("[1/2] Detecting correct typeValues ...\n")
    fixes, unresolved = _detect_correct_type_values(
        data, [artifact.path for artifact in csv_artifacts]
    )

    if not fixes:
        print("\n  No fixes needed — all obj-ref typeValues are correct.")
        if unresolved:
            print(f"\n  ⚠  {len(unresolved)} attributes could not be resolved:")
            for u in unresolved:
                print(f"    - {u}")
        return

    print(f"\n  Found {len(fixes)} attributes to fix:")
    for fix in fixes:
        wrong_name = type_map.get(fix["wrong_tv"], "?")
        print(f"    {fix['type_name']}.{fix['attr_name']}: "
              f"{wrong_name} -> {fix['correct_type_name']} ({fix['method']})")

    if unresolved:
        print(f"\n  WARNING: {len(unresolved)} attributes could NOT be resolved (no data/match):")
        for u in unresolved:
            print(f"    - {u}")

    # --- Phase 2: apply fixes on Cloud ---
    print(f"\n[2/2] Applying fixes on Cloud ...\n")

    ref_type_uuid = None
    if not dry_run:
        ref_type_uuid = get_ref_type_uuid(cloud_schema_id)

    ok_count = 0
    err_count = 0

    for fix in fixes:
        cloud_type_id = dc_to_cloud.get(fix["dc_type_id"])
        if not cloud_type_id:
            print(f"  SKIP {fix['type_name']}.{fix['attr_name']}: DC type not in mapping")
            err_count += 1
            continue

        correct_cloud_tv = all_dc_to_cloud.get(fix["correct_tv"])
        if not correct_cloud_tv:
            print(f"  SKIP {fix['type_name']}.{fix['attr_name']}: "
                  f"target DC type {fix['correct_tv']} not in any mapping")
            err_count += 1
            continue

        # Find Cloud attribute ID
        try:
            cloud_attrs = cloud_get(f"/objecttype/{cloud_type_id}/attributes")
        except Exception as e:
            print(f"  ERROR listing attributes for Cloud type {cloud_type_id}: {e}")
            err_count += 1
            continue

        cloud_attr_id = None
        for ca in cloud_attrs:
            ca_name = ca.get("name", "")
            if ca_name == fix["attr_name"]:
                cloud_attr_id = ca.get("id")
                break

        create_only = cloud_attr_id is None  # attribute missing on Cloud → CREATE instead of DELETE+POST

        if not create_only:
            # DELETE old attribute (or dry-run log)
            if dry_run:
                print(f"  [dry-run] Would fix {fix['type_name']}.{fix['attr_name']}: "
                      f"DELETE attr {cloud_attr_id}, POST with typeValue={correct_cloud_tv}")
                ok_count += 1
                continue
            try:
                cloud_delete(f"/objecttypeattribute/{cloud_attr_id}")
            except Exception as e:
                print(f"  ERROR deleting attr {cloud_attr_id} ({fix['type_name']}.{fix['attr_name']}): {e}")
                err_count += 1
                continue
        else:
            if dry_run:
                print(f"  [dry-run] Would CREATE {fix['type_name']}.{fix['attr_name']}: "
                      f"POST with typeValue={correct_cloud_tv} (attribute missing on Cloud)")
                ok_count += 1
                continue

        # POST new attribute with correct typeValue
        attr_def = fix["attr_def"]
        payload = {
            "name": fix["attr_name"],
            "type": 1,
            "typeValue": int(correct_cloud_tv),
        }
        if ref_type_uuid:
            payload["additionalValue"] = str(ref_type_uuid)
        if attr_def.get("description"):
            payload["description"] = attr_def["description"]
        max_c = attr_def.get("maximumCardinality", 1)
        if max_c == -1:
            payload["maximumCardinality"] = 100
        elif max_c and max_c > 1:
            payload["maximumCardinality"] = max_c
        min_c = attr_def.get("minimumCardinality", 0)
        if min_c and min_c > 0:
            payload["minimumCardinality"] = min_c

        try:
            cloud_post(f"/objecttypeattribute/{cloud_type_id}", payload)
            wrong_name = type_map.get(fix["wrong_tv"], "?")
            if create_only:
                print(f"  CREATED {fix['type_name']}.{fix['attr_name']}: "
                      f"-> {fix['correct_type_name']} ({fix['method']})")
            else:
                print(f"  FIXED {fix['type_name']}.{fix['attr_name']}: "
                      f"{wrong_name} -> {fix['correct_type_name']} ({fix['method']})")
            ok_count += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"  ERROR creating attr {fix['type_name']}.{fix['attr_name']}: {e}")
            err_count += 1

    # --- Summary ---
    print(f"\n{'='*60}")
    print(f"  Fixed   : {ok_count}/{len(fixes)}")
    print(f"  Errors  : {err_count}")
    if unresolved:
        print(f"  Unresolved : {len(unresolved)} (need manual fix)")
    if not dry_run and ok_count > 0:
        print(f"\n  Next step: re-run import-data to link obj-ref values:")
        print(f"    python migrate.py import-data --schema-name \"{schema_name}\"")
    if not dry_run:
        record_phase_outcome(
            artifacts,
            "fix-refs",
            "success" if not err_count and not unresolved else "warning",
            inputs=[
                struct_file,
                mapping_file,
                meta_file,
                *(item.path for item in csv_artifacts),
            ],
            counts={
                "attributesFixed": ok_count,
                "attributeErrors": err_count,
                "unresolvedAttributes": len(unresolved),
            },
            warnings=[
                {"classification": "unresolved-reference", "attribute": item}
                for item in unresolved
            ],
            errors=[
                {"classification": "final-api-failure", "count": err_count}
            ] if err_count else [],
        )
    print(f"{'='*60}\n")


def reconcile_mapping(schema_name: str, cloud_schema_id: str, exports_dir: Path,
                      mappings_dir: Path, write: bool = False) -> dict:
    """Preview or explicitly write a type mapping for an existing Cloud schema."""
    artifacts = SchemaArtifacts(schema_name, exports_dir, mappings_dir)
    try:
        structure = read_json(artifacts.structure_file)
        existing = read_json(artifacts.mapping_file)
    except ArtifactError as exc:
        print(f"ERROR: {exc}")
        return {"error": str(exc)}
    if not isinstance(structure, dict) or not isinstance(existing, dict):
        message = "Schema structure and mapping must be JSON objects."
        print(f"ERROR: {message}")
        return {"error": message}

    config.validate()
    cloud_types = cloud_get(f"/objectschema/{cloud_schema_id}/objecttypes")
    cloud_by_name = {item["name"]: str(item["id"]) for item in cloud_types}
    reconciled = {}
    unmatched = []
    for object_type in structure.get("objectTypes", []):
        cloud_id = cloud_by_name.get(object_type["name"])
        if cloud_id is None:
            unmatched.append(
                {"dcTypeId": object_type["id"], "name": object_type["name"]}
            )
        else:
            reconciled[str(object_type["id"])] = cloud_id

    result = {
        "schema": schema_name,
        "cloudSchemaId": str(cloud_schema_id),
        "matched": len(reconciled),
        "unmatched": unmatched,
        "mapping": reconciled,
        "written": False,
    }
    print(f"Mapping reconciliation for '{schema_name}': "
          f"{len(reconciled)} matched, {len(unmatched)} unmatched")
    for item in unmatched:
        print(f"  UNMATCHED DC type {item['dcTypeId']}: {item['name']}")

    if write:
        updated = dict(existing)
        updated["cloudSchemaId"] = str(cloud_schema_id)
        updated["objectTypeMapping"] = reconciled
        write_json(artifacts.mapping_file, updated)
        result["written"] = True
        print(f"Updated mapping: {artifacts.mapping_file}")
    else:
        print("Preview only. Re-run with --write to persist this mapping.")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Import a Jira Assets schema structure into Jira Cloud."
    )
    parser.add_argument("--schema-name", required=True,
                        help="Schema name (must match the folder under exports/)")
    parser.add_argument("--exports-dir", default="exports",
                        help="Base exports directory (default: ./exports)")
    parser.add_argument("--mappings-dir", default="mappings",
                        help="Mappings directory (default: ./mappings)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate without making any Cloud API calls")
    args = parser.parse_args()

    import_schema(
        args.schema_name,
        Path(args.exports_dir),
        Path(args.mappings_dir),
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
