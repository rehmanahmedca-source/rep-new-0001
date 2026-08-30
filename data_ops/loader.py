"""Compatibility loader — the old public API, powered by the new engine.

Kept so ``tools/data_ops.py import-json``, ``restore_data_backup`` and the
original tests keep working unchanged::

    report = load_legacy_json(conn, path, dry_run=False)

The real work now lives in :mod:`data_ops.planner` /
:mod:`data_ops.engine` (schema-aware, versioned, transactional).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from data_ops.engine import execute_restore
from data_ops.planner import SchemaAbort  # noqa: F401  (re-exported API)

__all__ = ["load_legacy_json", "SchemaAbort"]


def load_legacy_json(
    conn: sqlite3.Connection,
    source: str | Path | dict,
    *,
    dry_run: bool = False,
) -> dict:
    """Parse, validate, plan, load, report. Single transaction. Upsert on id."""
    report = execute_restore(conn, source, dry_run=dry_run)
    # Legacy consumers read these keys; keep them present.
    for tname, rec in report.get("tables", {}).items():
        inn = rec.get("in", 0)
        voided = rec.get("voided", 0)
        casc = rec.get("cascaded", 0)
        rec["arithmetic"] = f"{inn} - {voided} - {casc} = {rec.get('out', 0)}"
    return report
