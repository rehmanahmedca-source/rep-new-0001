# Phase 3 evidence

## Tests

```
$ python -m pytest tests/test_data_ops.py -q
......                                                                   [100%]
6 passed in 0.11s
```

| Test | Proves |
|------|--------|
| `test_backup_restore_twice_identical` | Snapshot + JSON + sha256; restore twice → identical counts |
| `test_additive_schema_restore` | Pre-change backup loads into schema with extra column + extra table |
| `test_rename_step` | Declared rename `acct_no` → `account_no` |
| `test_unknown_column_aborts` | Unknown column aborts |
| `test_void_orphan_coerce` | Voids refused, children cascaded, OrphanN clients |
| `test_bad_type_aborts` | Bad integer id aborts |

## CLI on a seeded copy (not the live DB)

```
seeded 24576 bytes  (client id=1, payment id=10 amount=50)

$ python tools/data_ops.py backup
{
  "backup": "/tmp/ams_data_backups/backup_20260828_162145",
  "manifest": {
    "format_version": "2026-04",
    "database_sha256": "c599c72e367bc59f73098a0ddf8ae4ac115eb011f481df596369b6920a1c6e9e",
    "json_sha256": "ed2394e29feaab35fc477dcd860e3fc3f1567ead9c17580eff43db3d693c31e9",
    "json": { "tables": 5, "rows": 2 },
    "verify": { "ok": true, "failures": [] }
  }
}

$ python tools/data_ops.py --db /tmp/ams_empty.db restore .../backup_20260828_162145
client arithmetic: 1 - 0 - 0 + 0 = 1
payment arithmetic: 1 - 0 - 0 + 0 = 1
money: payment.amount = 50.0
verify.ok true

$ python tools/data_ops.py --db /tmp/ams_empty.db restore-twice ...
{
  "first":  { "client": 1, "payment": 1, ... },
  "second": { "client": 1, "payment": 1, ... },
  "identical": true
}

Unknown column:
REFUSED: Unknown columns/tables not covered by a declared migration step: payment.ghost_col.
```

Live production data was not modified.
