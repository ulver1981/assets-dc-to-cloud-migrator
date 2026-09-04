# Copilot Instructions

## Commands

- Install runtime dependencies: `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
- Run the offline test suite: `.\.venv\Scripts\python.exe -m unittest tests.test_preflight_offline`
- Run one test: `.\.venv\Scripts\python.exe -m unittest tests.test_preflight_offline.PreflightOfflineTests.test_preflight_counts_root_and_nested_csvs`
- No build system, linter, formatter, or test runner configuration beyond the standard-library `unittest` tests is currently configured.
- Inspect the public CLI without touching Jira: `.\.venv\Scripts\python.exe migrate.py --help`
- Run a local, read-only artifact audit: `.\.venv\Scripts\python.exe migrate.py preflight --schema-name "<Schema Name>"`

## Architecture

- `migrate.py` is the public CLI and dispatches to the domain modules. Preserve its command names, options, and default `exports`/`mappings` paths.
- `config.py` is the single configuration boundary: it loads the repository-root `.env`, exposes DC and Cloud authentication helpers, and validates required credentials. Do not read credentials directly in domain modules.
- The migration pipeline is:
  1. `export_schema_structure.py` exports the DC schema definition to `exports/<Schema Name>/schema_structure.json`.
  2. `import_schema_structure.py` creates the Cloud schema, object-type hierarchy, and attributes; it writes `mappings/<Schema_Name>_mapping.json`.
  3. `export_data_csv.py` exports one CSV per DC object type plus `_attr_meta.json`.
  4. `import_data_csv.py` creates objects first and updates object references in a second pass; it writes `mappings/<Schema_Name>_objects.json`.
  5. `validate_migration.py` compares counts and samples values; `check_consistency.py` exhaustively compares object-reference relationships.
- Mapping files are the cross-schema contract. Type mappings associate DC type IDs with Cloud type IDs; object mappings associate `DC_Key` values with Cloud object IDs. Dependent schemas require prerequisite mappings to be present.
- `exports/`, `mappings/`, and `logs/` are generated, environment-specific data and are intentionally ignored. Treat them as operational state, not source-controlled fixtures.
- The active OpenSpec change is in `openspec/changes/consolidate-migration-reliability-artifacts/`. Its proposal, design, specs, and tasks define the approved consolidation and English-standardization work; keep application changes aligned with those artifacts.

## Repository-Specific Conventions

- The CLI is compatibility-sensitive. Keep all commands documented in `BASELINE.md`, including their current option names and default paths. Do not remove top-level script entry points.
- Schema directory names preserve the original schema name. Mapping filenames use a normalized schema stem: spaces become `_` and `/` becomes `-`. CSV stems replace `/` and `\` with `_`; duplicate type names append `__<DC type ID>`.
- `DC_Key` is both the import idempotency key and the value used to resolve object references. Do not replace it with display names or Cloud IDs in exported CSV data.
- Object references are intentionally imported in two phases: create non-reference attributes for every object, then update references once object mappings are available. A second `import-data` run is a supported recovery path for intra-schema cycles.
- DC object-reference metadata can be wrong. `fix-refs` reconciles reference target types using CSV evidence, names, and DC API fallback; preserve its `--dry-run` behavior and its relationship to mapping artifacts.
- DC cardinality `-1` is translated to Cloud cardinality `100`. Do not reintroduce `-1` in Cloud payloads. Attributes reserved by Cloud and DC uniqueness constraints require the existing handling in schema import.
- API calls use `requests.Session`, 429 retry delays, and bounded retries for 5xx responses. Keep retry behavior and progress logging consistent when changing API access.
- `preflight` and `status` are local inspection commands. `preflight` must make no DC/Cloud requests and must not write files; `status` must remain read-only and safe on Windows CP1252 consoles.
- Existing tests construct temporary exports and mappings and import `migrate.py` directly. Keep offline checks independently testable without credentials or network access.
- Keep human-facing maintained text in English, but do not translate CLI names, options, environment-variable names, JSON keys, API payload keys, filenames, schema names, or persisted mapping values.
