# Contributing

Contributions must use synthetic or fully redacted data. Do not submit Jira
credentials, access tokens, internal URLs, production exports, mappings, logs,
workspace IDs, or customer data.

Before opening a pull request, run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.\.venv\Scripts\python.exe release_readiness.py .
```

The project accepts focused fixes, tests, documentation improvements, and
synthetic reproductions. Compatibility changes should identify the tested Jira
Data Center and Cloud API behavior without disclosing environment data.
