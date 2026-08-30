# Data operations (backup, restore, upgrade, legacy JSON)

**Design contract: `docs/DATA_CENTER.md`** — read that first. This is the quick-operations guide.

Live DB: `instance/ahmed_cement_v44_fresh.db` (override with `APP_DB_PATH`).

| Role | Format | Notes |
|------|--------|-------|
| Main database | SQLite single file | Flask + SQLAlchemy 2, file-level backup |
| Export | JSON (schema-aware archive) | **The restore/merge transport.** Contains `schema` + `tables` + `format_version` |
| Export | XLSX | **Display only** — never restored (Excel corrupts types / drops columns) |
| Export | SQLite snapshot (`VACUUM INTO`) | Exact disaster copy (DDL, indexes, pragmas) |
| Restore | JSON | Plan-first upsert merge; versioned; idempotent; new tables stay untouched |
| Restore (disaster) | SQLite snapshot | Integrity-checked, typed confirmation, auto safety backup, post-verify |
| Legacy data | JSON merge | Same engine; legacy `.db` converts to JSON in one step |

All commands run from the repo root.

## Web UI (sidebar → **Data**)

| Page | URL | Purpose |
|------|-----|---------|
| Data Center | `/import_export/` | Overview + recent runs + server backup folders |
| Export | `/import_export/data-export` | JSON / XLSX / SQLite; all tables or selected |
| Restore | `/import_export/restore` | Upload JSON or pick a server backup → plan → APPLY |
| Disaster snapshot | `/import_export/restore/db` | Full replace from `.sqlite3` (destructive, guarded) |
| Legacy data | `/import_export/legacy` | Convert legacy DB → JSON, then merge with diff preview |
| History | `/import_export/history` | Every run with actor / file / verdict |

## CLI

```bash
# Backup (VACUUM INTO + schema-aware JSON + manifest)
python tools/data_ops.py backup

# Export
python tools/data_ops.py export-json   /tmp/ams.json
python tools/data_ops.py export-db     /tmp/ams.sqlite3
python tools/data_ops.py export-xlsx   /tmp/ams.xlsx

# Restore — always plan first, then apply (safety backup automatic)
python tools/data_ops.py plan          /tmp/old.json
python tools/data_ops.py restore-json  /tmp/old.json

# Legacy
python tools/data_ops.py import-json   /tmp/legacy.json --dry-run
python tools/data_ops.py convert-db    legacy.db /tmp/legacy.json

# Disaster replace (offline, explicit)
python tools/data_ops.py restore-db    backup_x/database.sqlite3 --yes

# Proofs
python tools/data_ops.py verify
python tools/data_ops.py restore-twice instance/storage/data_backups/backup_YYYYMMDD_HHMMSS
```

## Versioning guarantees

* `data_ops/constants.py` `FORMAT_VERSION` — the single version string. Bump only on a **non-additive** wire change.
* `data_ops/steps.py` `REGISTRY` — rename / drop / split / derive / retype steps keyed by file version.
* The restore planner diffs the **archive's embedded schema** against the **live target schema**:
  * table in file but not in target → abort (unless a declared `drop` step);
  * table in target but not in file → **untouched** (no data loss, new tables stay as they are);
  * column in file but not in target → abort (unless a declared step renames/drops it);
  * column in target but not in file → database DEFAULT, else type-neutral fill, **reported**;
  * `is_void` rows refused; children of voided parents cascaded; unknown clients → `OrphanN`;
  * optional FKs that don't resolve → blanked + reported; required FKs that don't resolve → abort.
* Restore is one transaction with `PRAGMA foreign_key_check` + `verify_database()` after commit; any failure rolls back to "nothing changed".

## What the old Excel path is now

Still registered for backward compatibility (`/import_export/…`, `/legacy-migration/…`) but **no longer linked from the sidebar** — XLSX is a display format, not a data path. New imports/restores use JSON only.
