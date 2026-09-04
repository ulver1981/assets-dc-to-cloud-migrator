# Jira Assets Data Center to Cloud Migration Toolkit

A community-maintained Python toolkit for migrating Jira Assets schema structures
and objects from Jira Data Center (Insight) to Jira Cloud Assets.

This project is not affiliated with, endorsed by, or supported by Atlassian. It
is provided as a community tool; validate every migration in a non-production
environment before using it with production data.

## Quick start

Create a virtual environment, install dependencies, then run the offline demo:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe demo_offline.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

The demo uses only [synthetic fixtures](fixtures/offline_demo) and does not load
`.env` or make network requests.

For a real migration, copy `.env.template` to `.env`, set your own endpoints and
credentials, and follow [GUIDE.md](GUIDE.md). Never commit `.env`, production
exports, mappings, or logs.

## Release safety

Build a reviewable public baseline from the explicit allowlist:

```powershell
.\.venv\Scripts\python.exe build_public_baseline.py ..\AssetsDataMigration-public
.\.venv\Scripts\python.exe release_readiness.py ..\AssetsDataMigration-public
```

The readiness check reports only offending paths and categories; it does not
print candidate secret values.

## License and support

Distributed under the [MIT License](LICENSE). Community support is limited to
reproducible issues created with synthetic or fully redacted data.
