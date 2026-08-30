# Data operations (backup, restore, upgrade, legacy JSON)

Live DB: `instance/ahmed_cement_v44_fresh.db` (override with `APP_DB_PATH`).

Transport of record: **JSON** with `format_version`. SQLite snapshot rebuilds a lost file. XLSX/CSV are display formats only.

All commands run from the repo root. They never write over the live file unless you pass that path.

## Backup (timestamped folder, never overwrite)

```bash
python tools/data_ops.py backup
```

Writes `instance/storage/data_backups/backup_YYYYMMDD_HHMMSS/` containing:

| File | Role |
|------|------|
| `database.sqlite3` | Consistent snapshot (`VACUUM INTO`, else online backup) |
| `export.json` | Name-keyed rows + `format_version` |
| `manifest.json` | sha256 of both + row counts |
| `REPORT.txt` | Short human summary |

Retention: last 30 dailies + up to 52 weeklies (`prune_backups`).

Existing UI backups (`python tools/maintenance.py backup`) still work; they are a different folder (`instance/storage/backups`) with integer manifest version `1`.

## Restore (idempotent; safe to run twice)

Into a **copy** (recommended):

```bash
export APP_DB_PATH=/tmp/ams_restore.db
python tools/data_ops.py restore instance/storage/data_backups/backup_YYYYMMDD_HHMMSS
python tools/data_ops.py restore-twice instance/storage/data_backups/backup_YYYYMMDD_HHMMSS
```

Empty target: the SQLite snapshot is copied first (JSON cannot recreate DDL). Then JSON **upserts on `id`** so a second run does not duplicate rows.

## Export JSON from the live DB

```bash
python tools/data_ops.py export-json /tmp/ams.json
```

## Import one legacy JSON file

```bash
python tools/data_ops.py import-json /tmp/ams.json --dry-run
python tools/data_ops.py import-json /tmp/ams.json --db /tmp/copy.db
```

Rules (in order): refuse `is_void`; cascade owned children; blank optional missing FKs; create `Orphan1…` clients for unknown identities; keep original ids.

Unknown column without a declared step → **abort** (no writes).

## Upgrade / schema steps

Edit `data_ops/steps.py` `REGISTRY`. Additive columns/tables need **no** step. For a rename:

```python
VersionSpec(
    version="2026-05",
    steps=[Step(kind="rename", table="payment", source="acct_no", target="account_no")],
)
```

Bump `FORMAT_VERSION` in `data_ops/constants.py` when you ship a non-additive change. Also keep using `app/migrations/` for DDL on the live app.

## Verify

```bash
python tools/data_ops.py verify
```

Fails on unresolved FKs / dangling `client_id`. Money totals are printed for reconciliation.

## What I did not change

- Live `instance/` data
- Excel full-raw import UI (`blueprints/import_export`)
- `app/services/maintenance.py` scheduler

If you need the Excel path to abort on unknown columns the same way, say so — that is a behaviour change to an existing feature.
