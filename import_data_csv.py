"""
import_data_csv.py

Imports CSV object data into Jira Cloud Assets.

Usage:
    python import_data_csv.py --schema-name "Example Inventory"

Inputs:
    exports/<schema_name>/csv/<TypeName>.csv
    exports/<schema_name>/_attr_meta.json    (from export_data_csv.py)
    mappings/<schema_name>_mapping.json      (from import_schema_structure.py)

Outputs:
    mappings/<schema_name>_objects.json      DC_Key → Cloud object ID

Key improvements vs old version:
  - Uses attribute NAMES in CSV (fixed header bug)
  - Resolves Object-reference values via DC key → Cloud object ID
    (intra-schema: resolved from objects already imported in this run;
     cross-schema: resolved from mappings/<other>_objects.json)
  - Idempotent: checks if object already exists before creating
  - Detailed per-row error reporting with unresolved ref tracking
"""

import argparse
import csv
import json
import re
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
)
from migration_http import request_json


MAPPINGS_DIR = Path("mappings")
EXPORTS_DIR = Path("exports")

# Shared session for connection pooling (reuse TCP+TLS across requests)
_session = requests.Session()


def _incremental_save(objects_file: Path, new_data: dict):
    """Merge new_data into objects_file (creating or updating it)."""
    existing = {}
    if objects_file.exists():
        try:
            existing = json.loads(objects_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError(
                f"Cannot update malformed object mapping {objects_file}: {exc}"
            ) from exc
        if not isinstance(existing, dict):
            raise ArtifactError(
                f"Cannot update object mapping {objects_file}: expected a JSON object."
            )
    existing.update(new_data)
    objects_file.parent.mkdir(parents=True, exist_ok=True)
    objects_file.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")


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


def cloud_put(path: str, payload: dict, retries: int = 3):
    return request_json(
        _session,
        "PUT",
        f"{config.CLOUD_API_BASE}{path}",
        auth=config.cloud_auth(),
        headers=config.cloud_headers(),
        payload=payload,
        timeout=30,
        retries=retries,
        success_statuses={200, 201},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_objects_mapping(schema_name: str, mappings_dir: Path) -> dict:
    """
    Load DC_Key → Cloud object ID from ALL *_objects.json files in mappings/.
    Returns combined dict.
    """
    combined: dict = {}
    for f in mappings_dir.glob("*_objects.json"):
        try:
            combined.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"    [warn] Could not load objects map {f}: {e}")
    return combined


def get_cloud_attrs_map(cloud_type_id: str) -> dict:
    """Returns {attr_name: attr_cloud_id} for a Cloud object type."""
    data = cloud_get(f"/objecttype/{cloud_type_id}/attributes")
    attrs = data if isinstance(data, list) else data.get("objectTypeAttributes", [])
    return {a["name"]: a["id"] for a in attrs}


def fetch_cloud_projects() -> dict:
    """
    Returns {project_name_lower: project_key} from the Cloud Jira project list.
    Used to resolve type=6 (Jira Project) attribute values.
    """
    import os
    site = os.getenv("CLOUD_SITE_URL", "").rstrip("/")
    if not site:
        return {}
    try:
        data = request_json(
            _session,
            "GET",
            f"{site}/rest/api/3/project/search",
            auth=config.cloud_auth(),
            headers={"Accept": "application/json"},
            params={"maxResults": 500},
            timeout=30,
        )
        return {p["name"].lower(): p["key"] for p in data.get("values", [])}
    except Exception as e:
        print(f"  [warn] Could not fetch Cloud projects: {e}")
        return {}


def preload_existing_cloud_objects(cloud_type_id: str) -> dict:
    """
    Batch-load ALL existing objects for a Cloud type that have a DC_Key attribute.
    Returns {dc_key: cloud_object_id}.

    NOTE: The Cloud IQL endpoint does not reliably include custom attributes
    (like DC_Key) in its response, so we first collect object IDs via IQL
    then read each one via the direct /object/{id} endpoint which does
    return all attribute values.
    """
    # Step 1: collect all object IDs via IQL (paginate until empty page)
    obj_ids: list = []
    page = 1
    try:
        while True:
            data = cloud_get("/iql/objects", params={
                "iql": f"objectTypeId = {cloud_type_id}",
                "resultsPerPage": 25,  # Cloud API caps pages at 25
                "page": page,
            })
            entries = data.get("objectEntries", [])
            if not entries:
                break
            for obj in entries:
                obj_ids.append(str(obj["id"]))
            page += 1
    except requests.exceptions.RequestException as e:
        print(f"    [warn] Could not list existing objects: {e}")
        return {}

    if not obj_ids:
        return {}

    # Step 2: read DC_Key from each object via direct endpoint
    result: dict = {}
    for obj_id in obj_ids:
        try:
            obj = cloud_get(f"/object/{obj_id}", params={"includeAttributes": "true"})
            for attr in obj.get("attributes", []):
                ota = attr.get("objectTypeAttribute") or {}
                if ota.get("name") == "DC_Key":
                    vals = attr.get("objectAttributeValues") or []
                    if vals:
                        dc_key = str(vals[0].get("displayValue") or vals[0].get("value") or "")
                        if dc_key:
                            result[dc_key] = obj_id
                    break
        except requests.exceptions.RequestException:
            pass  # skip unreadable objects
    return result


def find_existing_cloud_object(cloud_type_id: str, dc_key: str) -> str | None:
    """
    Fallback: check if a single Cloud object exists for a given DC key.
    Prefer preload_existing_cloud_objects() for batched lookups.
    """
    try:
        iql = f'"DC_Key" = "{dc_key}"'
        data = cloud_get("/iql/objects", params={
            "iql": iql,
            "objectTypeId": cloud_type_id,
            "resultsPerPage": 2,
        })
        entries = data.get("objectEntries", [])
        if entries:
            return str(entries[0]["id"])
    except requests.exceptions.RequestException:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Object creation
# ─────────────────────────────────────────────────────────────────────────────

def _create_with_retry(cloud_type_id: str, attributes: list) -> tuple[dict, list]:
    """
    POST /object/create with automatic error recovery:
      - "invalid due to restrictions" → strip offending attr(s), retry
      - "value provided is invalid"  → strip offending attr(s), retry
      - "must be less than 255 characters" → truncate value, retry
    Returns (result_dict, stripped_attr_ids).
    """
    payload = {"objectTypeId": cloud_type_id, "attributes": attributes}
    try:
        return cloud_post("/object/create", payload), []
    except Exception as e:
        err_str = str(e)
        # Parse JSON from error message: "400 Bad Request – {json}"
        err_json = None
        brace_idx = err_str.find("{")
        if brace_idx != -1:
            try:
                err_json = json.loads(err_str[brace_idx:])
            except Exception:
                pass

        if not err_json or not err_json.get("errors"):
            raise  # unrecoverable

        errors = err_json["errors"]
        strip_ids = []
        truncate_ids = []

        for attr_key, msg in errors.items():
            msg_lower = str(msg).lower()
            attr_id = attr_key.rsplit("-", 1)[-1]  # "rlabs-insight-attribute-3052" → "3052"

            if ("invalid due to restrictions" in msg_lower
                    or "value provided is invalid" in msg_lower
                    or "not found in jira" in msg_lower
                    or msg_lower.startswith("invalid values")
                    or "invalid group" in msg_lower
                    or msg_lower.startswith("user(s)")
                    or "has duplicated value" in msg_lower
                    or "refers to the schema" in msg_lower
                    or "can only contain max" in msg_lower):
                strip_ids.append(attr_id)
            elif "less than 255 characters" in msg_lower:
                truncate_ids.append(attr_id)

        # Truncate long text values
        if truncate_ids:
            for a in attributes:
                if str(a["objectTypeAttributeId"]) in truncate_ids:
                    for v in a.get("objectAttributeValues", []):
                        if v.get("value") and len(v["value"]) > 254:
                            v["value"] = v["value"][:254]

        # Strip entirely invalid attrs
        if strip_ids:
            attributes = [a for a in attributes
                          if str(a["objectTypeAttributeId"]) not in strip_ids]
            print(f"      [retry] dropping invalid attr(s) {strip_ids}")

        if truncate_ids:
            print(f"      [retry] truncating attr(s) {truncate_ids} to 254 chars")

        if not strip_ids and not truncate_ids:
            raise  # nothing we can fix

        payload = {"objectTypeId": cloud_type_id, "attributes": attributes}
        return cloud_post("/object/create", payload), strip_ids

def build_attributes_payload(row: dict, cloud_attr_map: dict,
                             type_attr_meta: dict,
                             dc_key_to_cloud_id: dict,
                             jira_projects_map: dict,
                             ref_filter: str = "all") -> tuple[list, list]:
    """
    Build the attributes array for a Cloud object creation payload.

    ref_filter controls which attributes are included:
      "all"       – every attribute (original behaviour)
      "no_refs"   – only non-object-ref attributes (Phase 1: create)
      "only_refs" – only object-ref (type=1) attributes (Phase 2: link)

    Returns (attributes_list, skipped_list) where each skipped item is
    {col, value, reason}.
    """
    attributes = []
    skipped = []

    for col, raw_value in row.items():
        # DC_Key is written as an attribute for idempotency tracking
        # (skip only if there's no DC_Key attribute in this Cloud type)
        if not raw_value or not raw_value.strip():
            continue

        cloud_attr_id = cloud_attr_map.get(col)
        if not cloud_attr_id:
            skipped.append({"col": col, "value": raw_value,
                            "reason": "attribute not found in Cloud type"})
            continue

        meta = type_attr_meta.get(col, {})
        attr_type = int(meta.get("type", 0))
        is_obj_ref = attr_type == 1
        is_jira_project = attr_type == 6
        max_card = int(meta.get("maximumCardinality", 1))
        is_multi_value = max_card != 1  # -1 (unlimited) or >1

        # Phase filtering: skip attributes based on ref_filter
        if ref_filter == "no_refs" and is_obj_ref:
            continue
        if ref_filter == "only_refs" and not is_obj_ref:
            continue

        # Split on " | " for multi-value types: obj-refs, Jira projects,
        # and any attribute with maximumCardinality != 1.
        if is_obj_ref or is_jira_project or is_multi_value:
            raw_parts = [p.strip() for p in raw_value.split("|") if p.strip()]
        else:
            raw_parts = [raw_value.strip()]
        object_attribute_values = []

        for part in raw_parts:
            if is_obj_ref:
                # part is a DC object key (e.g. IDS-12345) – resolve to Cloud ID
                cloud_obj_id = dc_key_to_cloud_id.get(part)
                if not cloud_obj_id:
                    skipped.append({"col": col, "value": part,
                                   "reason": f"Object-ref DC key '{part}' not yet imported"})
                    continue
                object_attribute_values.append({"value": str(cloud_obj_id)})
            elif is_jira_project:
                # part is a Jira project name – resolve to Cloud project key
                project_key = jira_projects_map.get(part.lower())
                if not project_key:
                    skipped.append({"col": col, "value": part,
                                   "reason": f"Jira project '{part}' not found in Cloud"})
                    continue
                object_attribute_values.append({"value": project_key})
            else:
                object_attribute_values.append({"value": part})

        if object_attribute_values:
            attributes.append({
                "objectTypeAttributeId": cloud_attr_id,
                "objectAttributeValues": object_attribute_values,
            })

    return attributes, skipped


# ─────────────────────────────────────────────────────────────────────────────
# Per-type import
# ─────────────────────────────────────────────────────────────────────────────

def import_type(ot_name: str, csv_file: Path,
                cloud_type_id: str,
                type_attr_meta: dict,
                dc_key_to_cloud_id: dict,
                schema_objects_out: dict,
                jira_projects_map: dict,
                objects_file: Path = None) -> tuple[int, int, list]:
    """
    Import all objects from one CSV file into Cloud.
    Updates dc_key_to_cloud_id and schema_objects_out in-place.
    Saves objects_file incrementally after each successful creation.
    Returns (count_ok, count_err, failed_rows) where failed_rows is a list
    of {"dc_key": ..., "error": ...} for objects that could not be created.
    """
    cloud_attr_map = get_cloud_attrs_map(cloud_type_id)
    has_dc_key_attr = "DC_Key" in cloud_attr_map
    # Attribute IDs blacklisted due to type restrictions (grows at runtime)
    restricted_attr_ids: set = set()

    with open(csv_file, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"  [{ot_name}] {len(rows)} rows, {len(cloud_attr_map)} Cloud attrs")

    # ── Batch-preload existing Cloud objects (idempotency) ──
    # Instead of N individual IQL queries, load all DC_Key→CloudID in one pass.
    if has_dc_key_attr:
        print(f"    preloading existing Cloud objects …", end=" ", flush=True)
        existing_map = preload_existing_cloud_objects(cloud_type_id)
        print(f"{len(existing_map)} found")
        for dk, cid in existing_map.items():
            dc_key_to_cloud_id[dk] = cid
            schema_objects_out[dk] = cid
    else:
        existing_map = {}

    count_ok = count_exists = count_err = 0
    all_skipped: list = []
    failed_rows: list = []
    total_rows = len(rows)

    for row_idx, row in enumerate(rows):
        dc_key = row.get("DC_Key", "").strip()

        # Idempotency: only skip if the object was confirmed on Cloud via preload.
        # Do NOT trust dc_key_to_cloud_id alone — it may contain stale IDs from
        # a previous import that was deleted and re-created.
        if dc_key and dc_key in existing_map:
            count_exists += 1
            continue

        # Cloud API requires a non-empty Name; fall back to DC_Key if blank
        if not row.get("Name", "").strip() and dc_key:
            row = {**row, "Name": dc_key}

        attributes, skipped = build_attributes_payload(
            row, cloud_attr_map, type_attr_meta, dc_key_to_cloud_id, jira_projects_map,
            ref_filter="no_refs",
        )
        # Drop already-known restricted attributes to avoid repeated retries
        if restricted_attr_ids:
            attributes = [a for a in attributes
                          if str(a["objectTypeAttributeId"]) not in restricted_attr_ids]
        all_skipped.extend(skipped)

        try:
            result, stripped = _create_with_retry(cloud_type_id, attributes)
            if stripped:
                restricted_attr_ids.update(stripped)  # blacklist for remaining rows
                for aid in stripped:
                    all_skipped.append({"col": f"attr_id:{aid}", "value": "",
                                        "reason": "type restriction mismatch, dropped on retry"})
            cloud_obj_id = str(result.get("id", result.get("globalId", "")))
            if dc_key:
                dc_key_to_cloud_id[dc_key] = cloud_obj_id
                schema_objects_out[dc_key] = cloud_obj_id
            count_ok += 1
            # Incremental save so a crash doesn't lose progress
            if objects_file and dc_key and count_ok % 50 == 0:
                _incremental_save(objects_file, schema_objects_out)
            # Progress every 200 rows
            if (row_idx + 1) % 200 == 0:
                pct = (row_idx + 1) / total_rows * 100
                print(f"    … {row_idx + 1}/{total_rows} ({pct:.0f}%) – "
                      f"{count_ok} ok, {count_exists} exist, {count_err} err")
            time.sleep(0.15)
        except Exception as e:
            err_msg = str(e)[:300]
            print(f"    ERROR row DC_Key={dc_key}: {err_msg}")
            if count_err == 0:  # print payload only for the first failure
                payload_debug = {"objectTypeId": cloud_type_id, "attributes": attributes}
                print(f"      payload: {json.dumps(payload_debug, ensure_ascii=False)[:600]}")
            failed_rows.append({"dc_key": dc_key, "type": ot_name, "error": err_msg})
            count_err += 1

    status = "OK" if count_err == 0 else "WARN"
    print(f"  [{ot_name}] {status}: {count_ok} created, "
          f"{count_exists} already existed, {count_err} errors")
    if all_skipped:
        uniq = {}
        for s in all_skipped:
            k = (s["col"], s["reason"])
            uniq[k] = uniq.get(k, 0) + 1
        for (col, reason), cnt in uniq.items():
            print(f"    skip [{cnt}x] '{col}': {reason}")

    return count_ok, count_err, failed_rows


def _load_existing_refs(cloud_type_id: str, ref_attr_names: set) -> dict:
    """
    Batch-load all objects of a type and return
    {cloud_obj_id: {attr_name: set_of_ref_cloud_ids}}.
    Used to skip Phase 2 updates for objects that already have refs set.

    NOTE: Cloud IQL caps pages at 25 entries and totalFilterCount is unreliable,
    so we paginate until we get an empty page.
    """
    result: dict = {}
    page = 1
    try:
        while True:
            data = cloud_get("/iql/objects", params={
                "iql": f"objectTypeId = {cloud_type_id}",
                "resultsPerPage": 25,  # Cloud API caps at 25
                "page": page,
                "includeAttributes": "true",
            })
            entries = data.get("objectEntries", [])
            if not entries:
                break
            for obj in entries:
                obj_id = str(obj["id"])
                obj_refs: dict = {}
                for attr in obj.get("attributes", []):
                    ota = attr.get("objectTypeAttribute") or {}
                    attr_name = ota.get("name", "")
                    if attr_name in ref_attr_names and int(ota.get("type", 0)) == 1:
                        ref_ids = set()
                        for v in (attr.get("objectAttributeValues") or []):
                            ref_obj = v.get("referencedObject")
                            if ref_obj:
                                ref_ids.add(str(ref_obj.get("id", "")))
                            elif v.get("value"):
                                ref_ids.add(str(v["value"]))
                        if ref_ids:
                            obj_refs[attr_name] = ref_ids
                result[obj_id] = obj_refs
            page += 1
    except Exception as e:
        print(f"    [warn] Could not preload existing refs: {e}")
    return result


def update_type_refs(ot_name: str, csv_file: Path,
                     cloud_type_id: str,
                     type_attr_meta: dict,
                     dc_key_to_cloud_id: dict,
                     jira_projects_map: dict):
    """
    Phase 2: update existing Cloud objects to set their obj-ref attributes.
    Called after ALL objects have been created (Phase 1) so that every DC key
    can be resolved to a Cloud object ID.
    """
    # Check whether this type actually has any obj-ref attributes
    has_refs = any(v.get("type") == 1 for v in type_attr_meta.values())
    if not has_refs:
        return 0, 0

    cloud_attr_map = get_cloud_attrs_map(cloud_type_id)

    # Identify obj-ref attribute names for idempotency check
    ref_attr_names = {name for name, info in type_attr_meta.items() if info.get("type") == 1}

    # Preload existing refs so we skip objects that already have refs set
    print(f"  [{ot_name}] preloading existing refs …", end=" ", flush=True)
    existing_refs = _load_existing_refs(cloud_type_id, ref_attr_names)
    already_linked = sum(1 for v in existing_refs.values() if v)
    print(f"{len(existing_refs)} objects, {already_linked} with refs")

    with open(csv_file, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    count_ok = count_err = count_skip = 0
    all_skipped: list = []
    total_rows = len(rows)
    # Attribute IDs known to be invalid (e.g. typeValue restriction mismatch);
    # once one row fails for a given attr, skip it for all remaining rows.
    restricted_attr_ids: set = set()

    for row_idx, row in enumerate(rows):
        dc_key = row.get("DC_Key", "").strip()
        if not dc_key:
            continue

        cloud_obj_id = dc_key_to_cloud_id.get(dc_key)
        if not cloud_obj_id:
            count_skip += 1
            continue

        # Idempotency: skip if this object already has refs set on Cloud
        obj_existing_refs = existing_refs.get(cloud_obj_id, {})
        if obj_existing_refs:
            count_skip += 1
            continue

        ref_attrs, skipped = build_attributes_payload(
            row, cloud_attr_map, type_attr_meta,
            dc_key_to_cloud_id, jira_projects_map,
            ref_filter="only_refs",
        )
        all_skipped.extend(skipped)

        # Drop already-known restricted attributes
        if restricted_attr_ids:
            ref_attrs = [a for a in ref_attrs
                         if str(a["objectTypeAttributeId"]) not in restricted_attr_ids]

        if not ref_attrs:
            continue   # no ref values to set for this row

        payload = {
            "objectTypeId": cloud_type_id,
            "attributes": ref_attrs,
        }
        try:
            cloud_put(f"/object/{cloud_obj_id}", payload)
            count_ok += 1
            if (row_idx + 1) % 200 == 0:
                pct = (row_idx + 1) / total_rows * 100
                print(f"    … {row_idx + 1}/{total_rows} ({pct:.0f}%) – "
                      f"{count_ok} linked")
            time.sleep(0.15)
        except Exception as e:
            err = str(e)
            # Try to recover: parse error JSON, strip invalid attrs, retry
            err_json = None
            brace_idx = err.find("{")
            if brace_idx != -1:
                try:
                    err_json = json.loads(err[brace_idx:])
                except Exception:
                    pass

            recovered = False
            if err_json and err_json.get("errors"):
                strip_ids = []
                for attr_key, msg in err_json["errors"].items():
                    msg_lower = str(msg).lower()
                    if ("invalid due to restrictions" in msg_lower
                            or "value provided is invalid" in msg_lower
                            or "not found in jira" in msg_lower
                            or msg_lower.startswith("invalid values")
                            or "invalid group" in msg_lower
                            or msg_lower.startswith("user(s)")
                            or "has duplicated value" in msg_lower
                            or "refers to the schema" in msg_lower
                            or "can only contain max" in msg_lower):
                        attr_id = attr_key.rsplit("-", 1)[-1]
                        strip_ids.append(attr_id)

                if strip_ids:
                    restricted_attr_ids.update(strip_ids)
                    ref_attrs = [a for a in ref_attrs
                                 if str(a["objectTypeAttributeId"]) not in restricted_attr_ids]
                    if count_err == 0:
                        print(f"      [Phase 2] blacklisting attr(s) {strip_ids} (restriction mismatch)")
                    if ref_attrs:
                        try:
                            payload["attributes"] = ref_attrs
                            cloud_put(f"/object/{cloud_obj_id}", payload)
                            count_ok += 1
                            recovered = True
                        except Exception:
                            pass
                    else:
                        recovered = True  # nothing left to link, not a real error
                        count_skip += 1

            if not recovered:
                if count_err == 0:
                    print(f"    ERROR linking DC_Key={dc_key}: {err[:200]}")
                count_err += 1

    if count_ok or count_err:
        status = "OK" if count_err == 0 else "WARN"
        print(f"  [{ot_name}] refs {status}: {count_ok} linked, "
              f"{count_skip} skipped, {count_err} errors")
    if all_skipped:
        uniq = {}
        for s in all_skipped:
            k = (s["col"], s["reason"])
            uniq[k] = uniq.get(k, 0) + 1
        for (col, reason), cnt in uniq.items():
            print(f"    skip [{cnt}x] '{col}': {reason}")

    return count_ok, count_err


# ─────────────────────────────────────────────────────────────────────────────
# Main import logic
# ─────────────────────────────────────────────────────────────────────────────

def import_data(schema_name: str, exports_dir: Path, mappings_dir: Path):
    config.validate()

    artifacts = SchemaArtifacts(schema_name, exports_dir, mappings_dir)
    schema_dir = artifacts.schema_dir
    csv_dir = artifacts.csv_dir
    meta_file = artifacts.attr_meta_file
    mapping_file = artifacts.mapping_file

    for p, label in [(csv_dir, "CSV dir"), (meta_file, "_attr_meta.json"),
                     (mapping_file, "mapping file")]:
        if not p.exists():
            print(f"ERROR: {p} not found ({label}). Check export/import-schema steps.")
            return

    try:
        attr_meta = read_json(meta_file)
        mapping = read_json(mapping_file)
    except ArtifactError as exc:
        print(f"ERROR: {exc}")
        return
    if not isinstance(attr_meta, dict) or not isinstance(mapping, dict):
        print("ERROR: Attribute metadata and mapping must be JSON objects.")
        return

    cloud_schema_id = str(mapping["cloudSchemaId"])

    print(f"\n{'='*60}")
    print(f"  IMPORT DATA  '{schema_name}'  (Cloud schema {cloud_schema_id})")
    print(f"{'='*60}\n")

    # Get Cloud type name → ID  (and DC→Cloud type mapping for __dcID suffixed CSVs)
    print("[1/4] Fetching Cloud object types …")
    raw_types = cloud_get(f"/objectschema/{cloud_schema_id}/objecttypes")
    name_to_cloud_id: dict = {t["name"]: str(t["id"]) for t in raw_types}
    dc_type_to_cloud_type: dict = {
        str(k): str(v) for k, v in mapping.get("objectTypeMapping", {}).items()
    }
    print(f"      Found {len(name_to_cloud_id)} Cloud types, {len(dc_type_to_cloud_type)} DC→Cloud mappings")

    # Build Jira project name → key map (for type=6 attributes)
    jira_projects_map = fetch_cloud_projects()
    if jira_projects_map:
        print(f"      Loaded {len(jira_projects_map)} Jira Cloud projects for type=6 resolution")

    # Load all known DC_Key → Cloud object ID mappings (cross-schema objects)
    print("[2/4] Loading existing object mappings …")
    dc_key_to_cloud_id = load_objects_mapping(schema_name, mappings_dir)
    print(f"      Pre-loaded {len(dc_key_to_cloud_id)} object key mappings")

    # Sort CSV files: import types with no obj-refs first to build the key map
    inventory = csv_inventory(csv_dir)
    try:
        assert_unique_csv_stems(inventory)
    except ArtifactError as exc:
        print(f"ERROR: {exc}")
        return
    csv_files = [entry.path for entry in inventory]
    if not csv_files:
        print(f"ERROR: No CSV files found in {csv_dir}")
        return

    # Sort: types with no obj-ref attributes come first, then the rest.
    # This maximises intra-schema resolution without multiple passes.
    def sort_key(fp: Path) -> int:
        ot_name = fp.stem.replace("_", "/")
        if ot_name not in attr_meta:
            ot_name = fp.stem
        meta = attr_meta.get(ot_name, {})
        has_refs = any(v.get("type") == 1 for v in meta.values())
        return 1 if has_refs else 0

    csv_files.sort(key=sort_key)

    print(f"\n[3/4] Phase 1 – Creating objects (no refs) for {len(csv_files)} type(s) …")
    schema_objects_out: dict = {}
    total_ok = total_err = 0
    all_failed_rows: list = []
    objects_file = artifacts.objects_file

    # Collect resolved (csv_file, cloud_type_id, type_attr_meta) for Phase 2
    resolved_types: list = []

    for csv_file in csv_files:
        ot_safe_name = csv_file.stem
        cloud_type_id = None

        # Handle __dcTypeId suffix for duplicate-name types
        # e.g. "Terraform IAC__221.csv" → DC type 221 → Cloud type via mapping
        dc_suffix_match = re.search(r"__(\d+)$", ot_safe_name)
        if dc_suffix_match:
            dc_type_id_str = dc_suffix_match.group(1)
            cloud_type_id = dc_type_to_cloud_type.get(dc_type_id_str)
            if not cloud_type_id:
                print(f"  SKIP '{ot_safe_name}': DC type {dc_type_id_str} not in mapping")
                continue
        else:
            # Standard name-based matching (handle "/" ↔ "_" substitution)
            cloud_type_id = (
                name_to_cloud_id.get(ot_safe_name)
                or name_to_cloud_id.get(ot_safe_name.replace("_", " "))
                or name_to_cloud_id.get(ot_safe_name.replace("_", "/"))
            )

        if not cloud_type_id:
            print(f"  SKIP '{ot_safe_name}': no matching Cloud type (available: {list(name_to_cloud_id)[:5]}…)")
            continue

        # Find attr_meta for this type
        type_attr_meta = (
            attr_meta.get(ot_safe_name)
            or attr_meta.get(ot_safe_name.replace("_", " "))
            or attr_meta.get(ot_safe_name.replace("_", "/"))
            or {}
        )

        resolved_types.append((ot_safe_name, csv_file, cloud_type_id, type_attr_meta))

        ok, err, failed = import_type(
            ot_safe_name, csv_file,
            cloud_type_id, type_attr_meta,
            dc_key_to_cloud_id, schema_objects_out,
            jira_projects_map,
            objects_file,
        )
        total_ok += ok
        total_err += err
        all_failed_rows.extend(failed)
        # Save after each type completes
        _incremental_save(objects_file, schema_objects_out)

    # Final save (already done incrementally, this ensures everything is flushed)
    _incremental_save(objects_file, schema_objects_out)

    # Save failed objects report (if any)
    if all_failed_rows:
        failed_file = schema_dir / "failed_objects.json"
        failed_file.write_text(
            json.dumps(all_failed_rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"  Failed objects report: {failed_file}")

    # ── Phase 2: set obj-ref attributes now that all objects exist ──
    print(f"\n[4/4] Phase 2 – Linking object references …")
    total_linked = total_link_err = 0
    for ot_safe_name, csv_file, cloud_type_id, type_attr_meta in resolved_types:
        linked, lerr = update_type_refs(
            ot_safe_name, csv_file,
            cloud_type_id, type_attr_meta,
            dc_key_to_cloud_id, jira_projects_map,
        )
        total_linked += linked
        total_link_err += lerr

    print(f"\n{'='*60}")
    print(f"  Phase 1 – objects created : {total_ok}")
    print(f"  Phase 1 – create errors   : {total_err}")
    if all_failed_rows:
        print(f"  Phase 1 – failed report   : {schema_dir / 'failed_objects.json'}")
    print(f"  Phase 2 – refs linked     : {total_linked}")
    print(f"  Phase 2 – link errors     : {total_link_err}")
    print(f"  Object map saved: {objects_file}")
    print(f"{'='*60}\n")
    record_phase_outcome(
        artifacts,
        "import-data",
        "success" if total_err == 0 and total_link_err == 0 else "warning",
        inputs=[meta_file, mapping_file, *csv_files],
        counts={
            "csvFiles": len(csv_files),
            "objectsCreated": total_ok,
            "createErrors": total_err,
            "referencesLinked": total_linked,
            "referenceErrors": total_link_err,
        },
        errors=[
            {"classification": "source-data", "count": total_err},
            {"classification": "unresolved-reference", "count": total_link_err},
        ] if total_err or total_link_err else [],
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Import Jira Assets object data from CSV to Cloud.")
    parser.add_argument("--schema-name", required=True,
                        help="Schema name (must match folder under exports/)")
    parser.add_argument("--exports-dir", default="exports")
    parser.add_argument("--mappings-dir", default="mappings")
    args = parser.parse_args()
    import_data(args.schema_name, Path(args.exports_dir), Path(args.mappings_dir))


if __name__ == "__main__":
    main()
