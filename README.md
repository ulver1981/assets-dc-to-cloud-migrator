# Jira Assets Data Center to Cloud Migrator

> Move Jira Assets schema definitions and object data from Data Center (Insight)
> to Jira Cloud Assets with a repeatable, reviewable migration workflow.

`assets-dc-to-cloud-migrator` is a community-maintained Python toolkit for Jira
administrators who need more than a one-off data copy. It exports migration
artifacts locally, creates the target schema, imports objects with stable source
keys, and provides validation and relationship-consistency checks.

> [!WARNING]
> This project is not affiliated with, endorsed by, or supported by Atlassian.
> Test every migration in a non-production Cloud workspace before operating on
> production data.

## Why this toolkit?

Assets migrations become difficult when object types have duplicate names,
objects reference one another, schemas depend on other schemas, or an import
needs to be resumed safely. This toolkit is designed around those operational
realities:

- **Artifact-first workflow** - schema exports, CSV data, attribute metadata,
  mappings, reports, and phase manifests leave an auditable local trail.
- **Idempotent object import** - the source `DC_Key` is retained as the stable
  identity used to avoid duplicate object creation on a re-run.
- **Two-pass reference handling** - import objects first, then resolve
  relationships once referenced objects have Cloud IDs.
- **Cross-schema support** - imports can reuse mappings produced by prerequisite
  schemas.
- **Safe recovery tools** - `preflight`, `status`, `--dry-run`, and
  `reconcile-mapping` expose migration state before writing to Cloud.
- **Resilient API behavior** - bounded retries handle rate limits and server
  errors; surfaced request errors redact embedded credentials and query values.

## Start safely: the offline demo

The fastest way to explore the project needs neither Jira credentials nor
network access. The demo runs against fictional schemas, objects, mappings, and
reports in [`fixtures/offline_demo`](fixtures/offline_demo).

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe demo_offline.py
```

### macOS and Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python demo_offline.py
```

Expected result:

```text
Offline demo passed.
Object types: 3
CSV files: 3
```

## Migration at a glance

```text
Jira Data Center                 Local migration artifacts              Jira Cloud Assets
----------------                 -------------------------              -----------------
schema structure  ----------->   schema_structure.json   ----------->   schema and types
object data       ----------->   CSV + attribute metadata ---------->   objects + DC_Key
                                  mappings + manifests     <---------->  Cloud IDs
                                  validation reports       <---------->  counts and references
```

For each schema, run the following workflow in order:

```bash
python migrate.py export-schema --schema-id <DC_SCHEMA_ID>
python migrate.py import-schema --schema-name "Example Inventory"
python migrate.py export-data --schema-id <DC_SCHEMA_ID>
python migrate.py fix-refs --schema-name "Example Inventory"
python migrate.py import-data --schema-name "Example Inventory"
python migrate.py validate --schema-name "Example Inventory" --sample 10
python migrate.py check --schema-name "Example Inventory"
```

When a schema refers to types in another schema, migrate the prerequisite schema
and its objects first. The full workflow, including command side effects and
destructive-operation safeguards, is documented in the [operator
guide](GUIDE.md).

## Configure a real migration

Copy `.env.template` to `.env`, then replace its placeholder values with
credentials and endpoints for environments you control:

```text
DC_BASE_URL
DC_TOKEN
CLOUD_BASE_URL
CLOUD_WORKSPACE_ID
CLOUD_EMAIL
CLOUD_API_TOKEN
```

Keep `.env`, exports, mappings, and logs local. They can contain credentials,
environment identifiers, or production migration state and are intentionally
excluded from the public project.

## Commands for confident operation

| Command | Purpose |
| --- | --- |
| `preflight` | Inspect local artifacts only; no API calls and no writes. |
| `status` | Summarize exported, mapped, imported, validated, and checked state. |
| `import-schema --dry-run` | Validate an exported schema without Cloud calls or mapping writes. |
| `fix-refs --dry-run` | Preview object-reference metadata repairs. |
| `reconcile-mapping` | Compare exported types with an existing Cloud schema; use `--write` only after reviewing its preview. |
| `validate` | Compare type counts and sample attributes across source and target. |
| `check` | Exhaustively verify imported object-reference relationships. |
| `delete-schema` | Permanently delete a Cloud schema after an explicit confirmation. |

See [GUIDE.md](GUIDE.md) for the full local-read, local-write, DC-read,
Cloud-read, and Cloud-write matrix.

## Validate your checkout

Run these commands before contributing or creating a release baseline:

```bash
python release_readiness.py .
python -m compileall -q .
python -m unittest discover -s tests -p "test_*.py"
python demo_offline.py
```

`release_readiness.py` is deliberately path-oriented: it identifies prohibited
files and likely unsafe configuration without echoing suspected secret values.
Run it against a clean checkout or a baseline created by:

```bash
python build_public_baseline.py ../assets-dc-to-cloud-migrator-public
python release_readiness.py ../assets-dc-to-cloud-migrator-public
```

## Repository layout

```text
migrate.py                    Unified migration CLI
migration_artifacts.py        Artifact naming, inventory, manifests, and staleness checks
migration_http.py             Retrying HTTP requests and redacted error messages
export_*.py / import_*.py     Data Center export and Cloud import operations
validate_migration.py         Count and sampled-attribute validation
check_consistency.py          Exhaustive object-reference validation
fixtures/offline_demo/        Fictional data for no-network validation
tests/                        Offline unit and safety tests
```

## Contributing and security

Bug reports and pull requests are welcome when they include a synthetic or
fully redacted reproduction. Never submit credentials, tokens, internal URLs,
production exports, mappings, logs, workspace IDs, or customer data.

- Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.
- Use the [security policy](SECURITY.md) for private vulnerability reporting.
- The project is released under the [MIT License](LICENSE).

## Current release

The initial public pre-release is
[v0.1.0-rc.2](https://github.com/ulver1981/assets-dc-to-cloud-migrator/releases/tag/v0.1.0-rc.2).
Feedback from Jira Assets administrators is especially valuable for additional
Data Center version coverage and safe, synthetic reproductions of edge cases.
