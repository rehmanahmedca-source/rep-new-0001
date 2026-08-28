# Core revisions — `app/migrations/`

Versioned, reviewed changes to the **shared** schema: the tables that belong to
the ERP core (`account`, `client`, `ledger_entry`, `sale`, `purchase`, …) rather
than to one module. A module's own revisions live next to it, in
`blueprints/<module>/migrations/`, and are declared in its `module.toml`
(see `docs/MODULE_CONTRACT.md`).

This folder was empty before the update pipeline existed, and the mechanism it
promotes is already in the codebase:
`blueprints/import_export/upgrade.py` (sorted `.sql` files, `migration_history`,
a destructive blocklist, transactional apply). `app/services/dbupdate/migrations.py`
is that idea generalised — same naming, same safety rules, plus a checksum, a
ledger, verification and reporting. Nothing was replaced: the legacy
`migration_history` rows are adopted into `ams_schema_migration` on the first run.

## What goes here — and what does not

| Situation | Where it belongs |
|---|---|
| A model gains a column that `create_all`/`_ensure_model_columns` cannot add (index, NOT NULL, type change) | a revision here |
| Backfill or repair of existing rows in core tables | a revision here with `KIND = "data"` |
| A new table owned by a feature pack | the module's own `migrations/` |
| Anything that drops a table/column | **not** automatic — see the rules below |

## File naming

```
app/migrations/2026_003_add_supplier_tax_no.py     # preferred (can verify + compensate)
app/migrations/2026_003_add_supplier_tax_no.sql     # pure DDL, no python
```

`<version>_<slug>.py|sql`, `version` sorting lexicographically-then-numerically
(`YYYY_NNN`). The version is unique across the whole folder; the ledger records
`core:<version>`.

## Revision contract (python)

```python
"""One paragraph: what changes and why."""
REVISION = "2026_003"
TITLE = "add supplier tax number"
KIND = "schema"          # "schema" | "data" | "index"
MODULE = "core"           # "core" here; the module id in a module pack
DESTRUCTIVE = False       # DROP / ALTER COLUMN / table rebuild → True, always
DEPENDS_ON = ()            # other revisions that must be applied first


def upgrade(connection):
    connection.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS ams_probe (id INTEGER PRIMARY KEY)"
    )
    return {"rows_updated": 0, "created": "ams_probe"}   # optional report


def verify(connection):
    """Mandatory for KIND = "data": raise if the change did not land."""
    row = connection.exec_driver_sql("SELECT COUNT(*) FROM ams_probe").first()
    if not row or row[0] < 0:
        raise RuntimeError("ams_probe missing")


def undo(connection):
    """Optional. SQLite commits DDL implicitly, so a failure after a CREATE
    cannot always be rolled back; undo() is how the pipeline compensates."""
    connection.exec_driver_sql("DROP TABLE IF EXISTS ams_probe")
```

`.sql` revisions are linted instead of imported: `DROP`, `TRUNCATE`,
`DELETE`/`UPDATE` without `WHERE`, and unbounded rewrites are **blocked** unless
`AMS_ALLOW_DESTRUCTIVE_MIGRATIONS=1` is set explicitly (and, in production,
unless the operator also passes `--yes`). A `WHERE`-less `UPDATE` is always
rejected in a `.sql` revision: batch it in `upgrade()` with row-count
assertions instead.

## Rules the pipeline enforces (do not work around them)

1. **Never edit an applied revision.** Its SHA-256 is stored in
   `ams_schema_migration`; a mismatch makes the revision `MODIFIED` and stops the
   run. Add a new revision that repairs the change.
2. **Nothing is dropped automatically.** A column present in the database but not
   in the models is reported as an observation and left alone — old data is
   cheaper to keep than to explain.
3. **`create_all()` is not a migration.** It is the additive baseline for
   development/first-boot; production never relies on it (see
   `docs/DATABASE_UPDATE_PIPELINE.md` for the policy table).
4. **A data revision must be verifiable.** `KIND = "data"` without `verify()` is
   refused, and no revision may leave fewer rows than it found: the pipeline
   compares per-table counts before and after and fails the run otherwise.
5. **Originals survive.** Normalise into a new column or record the exception;
   never overwrite and never discard.

## Adding one, end to end

```bash
# 1. write the revision, then check what the pipeline would do
python tools/dbupdate.py validate-migrations      # lint + checksum + deps
python tools/dbupdate.py plan --rehearse          # applies it to a copy

# 2. apply for real (backup, transaction, verify, integrity, regression)
python tools/dbupdate.py apply

# 3. read the verdict
instance/logs/UPDATE_HEALTH_REPORT.md
```

On a development database whose schema was created by `create_all`, it is
normal for `plan` to show nothing pending — the models already describe the
state. A revision is for the parts SQLModel's `create_all` cannot express.
