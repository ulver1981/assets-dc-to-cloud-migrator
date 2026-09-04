"""Deprecated compatibility wrapper for generic schema mapping reconciliation.

Use ``python migrate.py reconcile-mapping --schema-name ... --cloud-schema-id ...``
for new recovery work.
"""

import argparse
from pathlib import Path

from import_schema_structure import reconcile_mapping


def main() -> None:
    """Run a preview-first reconciliation for a specified schema."""
    parser = argparse.ArgumentParser(
        description="Reconcile a local schema mapping with an existing Cloud schema."
    )
    parser.add_argument("--schema-name", default="Example Inventory")
    parser.add_argument("--cloud-schema-id", default="65")
    parser.add_argument("--exports-dir", default="exports")
    parser.add_argument("--mappings-dir", default="mappings")
    parser.add_argument("--write", action="store_true",
                        help="Persist the reconciled mapping after previewing")
    args = parser.parse_args()
    reconcile_mapping(
        args.schema_name,
        args.cloud_schema_id,
        Path(args.exports_dir),
        Path(args.mappings_dir),
        write=args.write,
    )


if __name__ == "__main__":
    main()
