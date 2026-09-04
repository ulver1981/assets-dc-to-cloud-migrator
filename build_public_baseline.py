"""Create a clean, allowlisted directory suitable for public-release review."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from release_readiness import inspect_release_tree


ROOT_FILES = (
    ".env.template", ".gitignore", "LICENSE", "README.md", "GUIDE.md",
    "CONTRIBUTING.md", "SECURITY.md", "requirements.txt",
    "build_public_baseline.py", "config.py", "demo_offline.py",
    "release_readiness.py", "migration_artifacts.py", "migration_http.py",
    "migrate.py", "export_schema_structure.py", "export_data_csv.py",
    "import_schema_structure.py", "import_data_csv.py",
    "validate_migration.py", "check_consistency.py", "rebuild_csd_mapping.py",
)
ALLOWED_DIRECTORIES = ("fixtures", "tests")
PUBLIC_GITHUB_PATHS = (
    "copilot-instructions.md", "workflows", "ISSUE_TEMPLATE",
    "PULL_REQUEST_TEMPLATE.md",
)


def build_baseline(source: Path, destination: Path) -> None:
    """Copy only the explicit public source allowlist to an empty destination."""
    if destination.exists():
        if any(destination.iterdir()):
            raise ValueError(f"Destination must be empty: {destination}")
    else:
        destination.mkdir(parents=True)

    for name in ROOT_FILES:
        source_file = source / name
        if source_file.exists():
            shutil.copy2(source_file, destination / name)
    for name in ALLOWED_DIRECTORIES:
        source_dir = source / name
        if source_dir.exists():
            shutil.copytree(
                source_dir, destination / name, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
    for name in PUBLIC_GITHUB_PATHS:
        source_path = source / ".github" / name
        destination_path = destination / ".github" / name
        if source_path.is_file():
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
        elif source_path.is_dir():
            shutil.copytree(source_path, destination_path, dirs_exist_ok=True)

    findings = inspect_release_tree(destination)
    if findings:
        rendered = ", ".join(f.path.as_posix() for f in findings)
        raise ValueError(f"Baseline failed readiness checks: {rendered}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    build_baseline(args.source.resolve(), args.destination.resolve())
    print(f"Created clean public baseline: {args.destination.resolve()}")


if __name__ == "__main__":
    main()
