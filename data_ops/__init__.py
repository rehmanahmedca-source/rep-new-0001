"""Versioned, schema-aware backup / restore / legacy JSON engine.

Public API:

* :class:`data_ops.planner.SchemaAbort` — restore aborted before any write
* :func:`data_ops.backup_ops.create_data_backup` — VACUUM INTO + JSON + manifest
* :func:`data_ops.backup_ops.restore_data_backup` — idempotent restore
* :func:`data_ops.backup_ops.export_json` — portable envelope export
* :func:`data_ops.engine.execute_restore` — plan + apply (caller opts dry-run)
* :func:`data_ops.portable.export_json` — schema-aware archive writer
* :func:`data_ops.xlsx_export.export_xlsx` — display-only workbook
* :func:`data_ops.verify.verify_database` — post-load verification
"""

from data_ops.constants import FORMAT_VERSION, ARCHIVE_KIND
from data_ops.backup_ops import (
    create_data_backup,
    restore_data_backup,
    prune_backups,
    export_json,
)
from data_ops.loader import load_legacy_json, SchemaAbort
from data_ops.engine import execute_restore
from data_ops.verify import verify_database, row_counts

__all__ = [
    "FORMAT_VERSION",
    "ARCHIVE_KIND",
    "create_data_backup",
    "restore_data_backup",
    "prune_backups",
    "export_json",
    "load_legacy_json",
    "SchemaAbort",
    "execute_restore",
    "verify_database",
    "row_counts",
]
