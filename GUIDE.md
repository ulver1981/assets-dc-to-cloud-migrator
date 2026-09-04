# Operator guide

## Configuration

Copy `.env.template` to `.env` and set values for your Jira environments.
`.env` contains credentials and must remain local. The example URLs and values in
the template are placeholders, not usable endpoints.

## Migration workflow

For each source schema, use the following order:

1. `python migrate.py export-schema --schema-id <DC_SCHEMA_ID>` exports the
   schema structure from Data Center to local JSON.
2. `python migrate.py import-schema --schema-name "Example Inventory"` creates
   the schema and types in Cloud. Add `--dry-run` to validate local structure
   without Cloud calls or mapping writes.
3. `python migrate.py export-data --schema-id <DC_SCHEMA_ID>` exports source
   objects to local CSV and metadata.
4. `python migrate.py fix-refs --schema-name "Example Inventory"` repairs
   object-reference type metadata. `--dry-run` previews changes.
5. `python migrate.py import-data --schema-name "Example Inventory"` imports
   objects in two phases so references can resolve through `DC_Key`.
6. Run `validate` and `check` to compare migration results.

Import prerequisite schemas before schemas that reference their object types.

## Command side effects

| Command | Local read | Local write | DC read | Cloud read | Cloud write |
| --- | --- | --- | --- | --- | --- |
| `preflight`, `status` | Yes | No | No | No | No |
| `export-schema`, `export-data` | Yes | Yes | Yes | No | No |
| `import-schema --dry-run` | Yes | Log only | No | No | No |
| `import-schema`, `fix-refs`, `reconcile-mapping`, `import-data` | Yes | Yes | As needed | Yes | Yes |
| `validate`, `check` | Yes | Yes | Yes | Yes | No |
| `delete-schema` | Yes | Yes | No | Yes | **Yes, irreversible** |

`delete-schema` asks for an interactive `yes` confirmation. Automation must
explicitly pass `--confirm-delete`; use it only after independently verifying
the Cloud schema ID.

## Offline demo and validation

Run the synthetic no-network demo:

```powershell
.\.venv\Scripts\python.exe demo_offline.py
```

Run all offline checks:

```powershell
.\.venv\Scripts\python.exe release_readiness.py .
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Run the readiness check only against a clean public baseline. It must fail for
an operational working directory containing `.env`, exports, mappings, or logs.
