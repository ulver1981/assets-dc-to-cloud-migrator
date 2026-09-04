"""Shared local-artifact conventions for Jira Assets migrations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MANIFEST_FILENAME = "migration_manifest.json"
MANIFEST_VERSION = 1


class ArtifactError(ValueError):
    """Raised when a required local migration artifact is missing or invalid."""


def safe_schema_name(schema_name: str) -> str:
    """Return the legacy-compatible schema name used in mapping filenames."""
    return schema_name.replace(" ", "_").replace("/", "-")


def legacy_csv_stem(type_name: str) -> str:
    """Return the CSV stem used by the existing data exporter."""
    return type_name.replace("/", "_").replace("\\", "_")


def read_json(path: Path, *, required: bool = True) -> Any | None:
    """Read JSON, raising an actionable error for required artifacts."""
    if not path.exists():
        if required:
            raise ArtifactError(f"Required artifact not found: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"Invalid JSON artifact {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    """Write a local JSON artifact using the repository's established encoding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


@dataclass(frozen=True)
class SchemaArtifacts:
    """Paths for one schema without changing the established on-disk layout."""

    schema_name: str
    exports_dir: Path
    mappings_dir: Path

    @property
    def schema_dir(self) -> Path:
        return self.exports_dir / self.schema_name

    @property
    def csv_dir(self) -> Path:
        return self.schema_dir / "csv"

    @property
    def mapping_file(self) -> Path:
        return self.mappings_dir / f"{safe_schema_name(self.schema_name)}_mapping.json"

    @property
    def objects_file(self) -> Path:
        return self.mappings_dir / f"{safe_schema_name(self.schema_name)}_objects.json"

    @property
    def structure_file(self) -> Path:
        return self.schema_dir / "schema_structure.json"

    @property
    def attr_meta_file(self) -> Path:
        return self.schema_dir / "_attr_meta.json"

    @property
    def manifest_file(self) -> Path:
        return self.schema_dir / MANIFEST_FILENAME


@dataclass(frozen=True)
class CsvArtifact:
    """One exported CSV and its relative location in the schema CSV directory."""

    path: Path
    relative_path: Path

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def is_nested(self) -> bool:
        return self.relative_path.parent != Path(".")


def csv_inventory(csv_dir: Path) -> list[CsvArtifact]:
    """Return the complete recursive CSV inventory in deterministic order."""
    if not csv_dir.is_dir():
        return []
    return [
        CsvArtifact(path=path, relative_path=path.relative_to(csv_dir))
        for path in sorted(csv_dir.rglob("*.csv"))
    ]


def assert_unique_csv_stems(csv_files: list[CsvArtifact]) -> None:
    """Reject CSV layouts where two files would resolve to the same type identity."""
    duplicates: dict[str, list[str]] = {}
    for csv_file in csv_files:
        duplicates.setdefault(csv_file.stem, []).append(str(csv_file.relative_path))
    conflicts = {stem: paths for stem, paths in duplicates.items() if len(paths) > 1}
    if conflicts:
        details = "; ".join(
            f"{stem}: {', '.join(paths)}" for stem, paths in sorted(conflicts.items())
        )
        raise ArtifactError(f"Ambiguous CSV artifact identities: {details}")


def csv_data_rows(csv_file: Path) -> int:
    """Count data rows in a CSV without interpreting values."""
    try:
        with csv_file.open(encoding="utf-8", newline="") as handle:
            return max(0, sum(1 for _ in handle) - 1)
    except OSError as exc:
        raise ArtifactError(f"Cannot read CSV artifact {csv_file}: {exc}") from exc


def fingerprint(path: Path) -> dict[str, int | str]:
    """Return a compact content fingerprint for stale-outcome detection."""
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        stat = path.stat()
    except OSError as exc:
        raise ArtifactError(f"Cannot fingerprint artifact {path}: {exc}") from exc
    return {"path": str(path), "size": stat.st_size, "sha256": digest}


def record_phase_outcome(
    artifacts: SchemaArtifacts,
    phase: str,
    status: str,
    *,
    inputs: list[Path],
    counts: dict[str, int] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
    transformations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Merge an explicit, versioned outcome into a schema manifest."""
    manifest = read_json(artifacts.manifest_file, required=False)
    if manifest is None:
        manifest = {
            "version": MANIFEST_VERSION,
            "schema": artifacts.schema_name,
            "phases": {},
        }
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise ArtifactError(f"Unsupported migration manifest: {artifacts.manifest_file}")

    manifest["phases"][phase] = {
        "status": status,
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "inputs": [fingerprint(path) for path in inputs if path.exists()],
        "counts": counts or {},
        "warnings": warnings or [],
        "errors": errors or [],
        "transformations": transformations or [],
    }
    write_json(artifacts.manifest_file, manifest)
    return manifest


def stale_phases(artifacts: SchemaArtifacts) -> list[str]:
    """Return phase names whose recorded inputs no longer match local artifacts."""
    manifest = read_json(artifacts.manifest_file, required=False)
    if manifest is None:
        return []
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise ArtifactError(f"Unsupported migration manifest: {artifacts.manifest_file}")

    stale = []
    for phase, outcome in manifest.get("phases", {}).items():
        for saved in outcome.get("inputs", []):
            path = Path(saved["path"])
            if not path.exists() or fingerprint(path) != saved:
                stale.append(phase)
                break
    return stale
