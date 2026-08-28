# Data-layer audit (database, backup/restore, legacy import)

Stack: Flask + SQLAlchemy 2 + SQLite. Live file: `instance/ahmed_cement_v44_fresh.db` (`APP_DB_PATH` / `config.RUNTIME_DB_NAME`). Schema is created by `db.create_all()` plus hand-rolled `_ensure_model_columns` / `app.services.dbupdate` revisions — **not Alembic**.

| Sev | Area | Finding | Where |
|-----|------|---------|--------|
| HIGH | Schema versioning | Core `app/migrations/` is empty. Additive columns are applied at boot by `_ensure_model_columns` / `create_all`. Restoring an older *file copy* of the DB into a newer app works only because changes have been additive so far. Renames have no declared steps in the old path. | `app/migrations/README.md`; `app/services/schema.py`; `app/services/dbupdate/migrations.py` |
| HIGH | Legacy import vs XLSX | Full-raw import reads Excel (`pandas.read_excel`) and **silently ignores unknown columns** (warning only). Spreadsheet round-trips already produced `'7761.0'` account numbers. XLSX is a display format, not a source of truth. | `blueprints/import_export/engine.py` ~725–735 (`unknown_columns` → warning, continue) |
| HIGH | Pre-import backup no-op | `backup_database()` always returns success and writes nothing (“single-store mode”). A failed master import has no snapshot to roll back to. | `blueprints/import_export/hash_io.py` 48–51, 147–150 |
| HIGH | Restore not upsert | Full-raw import **skips** duplicate primary keys instead of `INSERT OR REPLACE`. Re-running an append import does not double rows (good) but also does not refresh them. Overwrite mode DELETEs then INSERT — not idempotent if the delete fails mid-way. | `engine.py` ~770–790 |
| MED | SQLite FK pragma | **Fixed on the SQLAlchemy engine**: every connect runs `PRAGMA foreign_keys=ON` and asserts it. Raw `sqlite3.connect` elsewhere must set it itself. | `app/__init__.py` 271–286 |
| MED | WAL | Journal mode is WAL locally, DELETE on PythonAnywhere/NFS. | `app/__init__.py` `_resolve_sqlite_journal_mode` |
| MED | Backup mechanism | Authoritative backups (`app/services/maintenance.py`) use the **SQLite online backup API** (good: not a running-file copy). They do **not** use `VACUUM INTO`. Manifest `format_version` is integer `1`, unrelated to workbook `2026-04`. Retention default is **3** folders (`BACKUP_RETENTION`). Checksums exist. | `maintenance.py` 197–235, 247–270 |
| MED | File-copy restore | `_restore_sqlite_snapshot` uses `shutil.copy2` of db + wal/shm — unsafe if the live connection is open. | `hash_io.py` 266–278 |
| MED | Positional INSERT | No `INSERT INTO t VALUES (...)` without column names in application code. Named columns used in cash-flow helpers. | `cash_flow_reconciliation_helpers.py` 157, 217 |
| MED | FKs declared | SQLAlchemy `ForeignKey` on sales/stock/parties/etc. SQLite enforces them only with the pragma above. Text `client_code` / `client_name` are **not** FKs — this is the orphan source. | `models/sales.py`, `models/parties.py` |
| LOW | Hourly backup worker | `_start_hourly_backup_worker` still exists; embedded scheduler is off by default (`BACKUP_EMBEDDED_SCHEDULER`). | `app/services/backup.py`; `app/__init__.py` 261 |
| LOW | Import transaction | Full-raw import commits valid rows even when some fail (partial). Not one all-or-nothing transaction. | `engine.py` ~830 `db.session.commit()` |
| INFO | Voided rows | Soft delete `is_void` exists; full-raw import **does not** refuse voided rows — they are restored as-is. | models + engine |
| INFO | Master migration ETL | `app/services/legacy_migration.py` maps by **column name**, reports orphans, does not auto-merge. Transaction templates cannot import. | `legacy_migration.py` |
| INFO | Workbook meta | `__AMS_META__` `format_version` currently `2026-04`. | `blueprints/import_export/misc_helpers.py` 36 |

## What this pass adds (does not replace the UI importers)

A standalone `data_ops/` package + `tools/data_ops.py`:

- Embeds string `format_version` (`2026-04`) in JSON and backup manifests.
- Per-version step registry (`rename` / `retype` / `split` / `derive` / `default` / `drop`).
- JSON loader that matches **by name**, aborts on unknown columns without a step, coerces types with a report, refuses `is_void`, cascades children, synthesizes `OrphanN` clients, upserts on `id` in one transaction.
- Timestamped `VACUUM INTO` (fallback: online backup) + JSON + sha256 manifest + prune (daily 30 / weekly 52).
- Post-load `PRAGMA foreign_key_check` and money totals.

Existing Excel import UI is unchanged (do not rewrite features). Treat XLSX as display-only; use JSON from `tools/data_ops.py export-json` as the migrate transport.
