# Database & Module Update Pipeline

One code path can change this ERP's schema: `app/services/dbupdate`. Application
startup, `tools/dbupdate.py`, the CI gate and the deploy gate all call the same
`run_update()`, so what you see from the CLI is what the server does at boot —
not a best-effort imitation of it.

```
app/services/dbupdate/
  policy.py         development vs production, what may be written and when
  ledger.py         ams_schema_migration / ams_update_run + PRAGMA user_version
  schema_audit.py   expected (models + modules) vs actual (database)
  migrations.py     revision discovery, linting, checksums, planning
  legacy_steps.py   the historical ensure-chain, declared and recorded
  integrity.py      SQLite checks + preflight + the financial/inventory suite
  installs.py       ams_module_install (what is installed, in which version)
  runner.py         the pipeline itself
  reports.py        JSON + markdown reports with bounded history
  docs.py           generates docs/MODULE_REGISTRY.md from live metadata
```

`app/services/schema.py::_bootstrap_database()` is **still there and unchanged**.
It remains the fallback the factory uses when the pipeline is skipped or fails,
and its individual steps are now driven through `legacy_steps.STEPS` so each one
is recorded and re-audited instead of living inside `try/except: pass`.

---

## 1. The gates, in order

`UpdatePipeline.run()` executes these steps in sequence. A hard failure marks the
run `UPDATE REQUIRES ATTENTION` and stops the steps after it; the run is always
reported, never swallowed.

| # | Step | Guarantees |
|---|---|---|
| 1 | `discover_modules` | manifests parsed and validated; a broken module is inactive **with a reason** |
| 2 | `migration_ledger` | ledger tables exist (only when this run is allowed to write) |
| 3 | `detect_requirements` | model metadata + module-declared tables become the expectation |
| 4 | `compare_schema` | full audit: tables, columns, indexes, FKs, unique constraints, drift |
| 5 | `approved_migrations` | every change is either covered by a revision or closable by the additive baseline |
| 6 | `validate_migrations` | lint, checksum vs ledger, destructive policy, dependency edges |
| 7 | `dependencies` | no module is activated with unresolved dependencies; cycles are reported |
| 8 | `backup` | a verified backup exists before anything risky (mandatory in production) |
| 9 | `apply_baseline` | the historical additive chain (`create_all` + ensure-steps), recorded |
| 10 | `apply_migrations` | each revision in its own transaction, then verified |
| 11 | `verify_schema` | re-audit: the schema now matches, and any step the baseline swallowed shows up here |
| 12 | `data_integrity` | `PRAGMA integrity_check`, FK violations, preflight, the consistency suite |
| 13 | `module_health` | each module's own checks (routes, tables, permission wiring) |
| 14 | `navigation` | every sidebar entry (module **and** core layout) resolves |
| 15 | `module_tests` | the affected modules' declared tests (opt-in) |
| 16 | `regression` | row counts never shrink; a smoke pass over core routes and financial totals |

Steps 1–14 are the twelve gates of the standard, with the two extra checks this
codebase needs (navigation and install bookkeeping) because the ERP's historical
failure mode was a *route or sidebar* breaking rather than a table.

`READY` is only ever the last word after 11–16 have passed — the pipeline cannot
claim success it has not verified.

## 2. Modes

| Mode | Writes? | Used by |
|---|---|---|
| `check` | **no** — no DDL, no ledger rows, no backups | `dbupdate.py check` (the default), the admin "run a check" action |
| `plan` | no, but resolves pending revisions and rehearses optionally | `dbupdate.py plan` |
| `apply` | yes, guarded by policy + backup + verification | `dbupdate.py apply`, `full-update --apply`, startup under `auto`/`guarded` |

`preview_on_copy()` ( `plan --rehearse` ) copies the database file and applies the
pending revisions in a **subprocess** against the copy — a second Flask app in
the same process would share `models.db`'s metadata and prove nothing.

## 3. Environment policy (`policy.py`)

`AMS_ENV` is `development` | `test` | `production`; when unset it is inferred
(PythonAnywhere markers ⇒ production). `AMS_UPDATE_POLICY` is `auto` | `guarded`
| `audit` | `manual`; when unset, production defaults to `guarded`, everything
else to `auto`.

| Policy | Baseline `create_all` | Additive ensure-steps | Revisions | Destructive | Notes |
|---|---|---|---|---|---|
| `auto` | yes | yes | applied after verification | only with the flag | development / CI |
| `guarded` | empty DB only | **yes** (failures recorded + re-audited) | non-destructive only | blocked unless `ALLOW_DESTRUCTIVE_MIGRATIONS` **and** `--yes` in prod | production default |
| `audit` | no | no | **not** applied | never | reports only |
| `manual` | no | no | never at boot; `apply` needs `--force` | never | change window owned by a human |

Three things cannot be talked out of, whatever the environment:

* production **always** takes and validates a backup before a schema change
  (`AMS_REQUIRE_BACKUP_BEFORE_UPDATE=0` is ignored, with a note in the report);
* production **never** resets or seeds over real data (`AMS_ALLOW_DB_RESET` is
  ignored);
* a revision that is destructive, unverified (for data), or whose file changed
  after being applied stops the run instead of being "repaired" on the fly.

`config.validate_config()` additionally refuses a configuration that asks for
destructive migrations in production, and `deployment_config()` prints the whole
policy block in the control panel so the effective state is visible at a glance.

Under pytest, boot-time updates drop to `check`: the suite creates an app per
test and paying a backup each time makes the tests too slow to be run. Set
`AMS_UPDATE_UNDER_TESTS=1` (or any explicit `AMS_UPDATE_POLICY`) to exercise the
full path — `tests/test_dbupdate_pipeline.py` does exactly that through
`run_update()` directly.

## 4. Backups and retention

The pipeline uses the instance's existing maintenance engine
(`app/services/maintenance.py`), not a second one:

* `create_backup(reason="pre-update:<run_key>")` → a validated copy plus a
  manifest, recorded in `ams_update_run` and in the ledger row of every revision
  applied in that run (`backup_path`, `backup_sha256`);
* the backup is **validated** (size, `PRAGMA integrity_check` on the copy,
  sha256) before anything is written;
* `tools/maintenance.py` retention pruning applies after success, so repeated
  updates cannot grow the disk without bound — the same guarantee holds for the
  report archive;
* `AMS_REQUIRE_BACKUP_BEFORE_UPDATE` + no usable backup ⇒ the run stops before
  touching the schema, with `next_action` naming the command to run.

Restore is deliberately manual (`app.services.recovery` /
`tools/maintenance.py restore`), because a pipeline that restores on its own
could undo the operator's own repair.

## 5. Reports (and their size)

Written on every run into `UPDATE_REPORT_DIR` (default `instance/logs/`):

| File | Contents |
|---|---|
| `update-health-report.json` | the whole run: policy, modules, revisions, every step, blockers |
| `schema-audit.json` | expected vs actual, issues by severity |
| `module-registry-report.json` | discovery result, statuses, navigation, migration plan |
| `migration-report.json` | per-revision applied/failed/skipped with timings and reports |
| `UPDATE_HEALTH_REPORT.md` | the human page (module counts, migration counts, each verification, final status) |
| `update-history/` | timestamped copies of the above, capped at `UPDATE_REPORT_HISTORY` runs (default 12) |

Secrets never reach them: `reports.redact()` strips any key that looks like a
credential (`password`, `token`, `api_key`, `authorization`, …) and collapses
`user:pass@` inside connection URLs before writing. Stack traces stay in
`instance/logs/errorlog.txt`, and reports link to it by step name instead.

Machine consumers: `/health` carries `update.final_status` and
`update.bootstrap_error`; `/admin/api/modules/updates` returns the latest report
(or runs a fresh read-only check with `?run=1`).

## 6. When a run fails

A blocker always answers the same six questions, in the report and on
`/admin/modules/updates`:

```
step            where in the pipeline it stopped
what            the failure, in one sentence
why             the rule that stopped it
where / module / database_object   what to look at
data_risk       what is at stake for real records
next_action     the exact command or file to change
```

The common ones and what they mean:

| Blocker | Reading | Action |
|---|---|---|
| `MODIFIED` revision | an applied file was edited | add a *new* revision; never rewrite history |
| `approved_migrations` | drift with no revision while the baseline will not close it | write the revision (module pack or `app/migrations/`) |
| `validate_migrations` | unlintable SQL / data revision without `verify()` | fix the revision |
| `dependencies` | module depends on a missing/failed module | fix or disable the dependency |
| `apply_migrations` | the revision raised | it was rolled back or compensated (`undo()`); fix and re-run |
| `verify_schema` | the schema still differs after applying | read `schema-audit.json`; `SILENT_BOOTSTRAP_FAILURE` means an ensure-step threw and was swallowed by legacy code |
| `data_integrity` / `regression` | financial/inventory consistency or row counts | **stop**; compare with the backup; repair with `tools/repair_controlled/` |
| `navigation` | a sidebar endpoint no longer resolves | restore the route or update the manifest/layout |

Nothing continues past a critical failure on a *write*: the run is marked
`UPDATE REQUIRES ATTENTION`, the modules involved keep their status
(`MIGRATION_REQUIRED`, `FAILED_HEALTH`, …), and `/health` reports `degraded`.

## 7. Operator commands

```bash
python tools/dbupdate.py                      # == check: read-only, exit 1 if pending
python tools/dbupdate.py discover --json      # why a module is not active
python tools/dbupdate.py audit-schema         # models vs ledger vs database
python tools/dbupdate.py validate-migrations  # lint + checksums + dependencies
python tools/dbupdate.py plan --rehearse      # pending list, applied to a copy
python tools/dbupdate.py backup               # explicit verified backup
python tools/dbupdate.py apply                # backup → migrate → verify → report
python tools/dbupdate.py integrity            # SQLite + preflight + consistency suite
python tools/dbupdate.py tests --all          # regression suite
python tools/dbupdate.py full-update --apply  # the whole sequence in one go
python tools/dbupdate.py status / history / docs
```

Destructive behaviour is never the default: the default command is `check`,
`apply` writes only what the policy permits, and in production `apply` additionally
requires `--yes`. `full-update` without `--apply` is a preview.

## 8. Post-upgrade checklist (what "done" means)

1. `UPDATE HEALTH REPORT` ends in `FINAL STATUS: READY`;
2. `MIGRATIONS PENDING: 0` and `MIGRATIONS FAILED: 0`;
3. `SCHEMA VALIDATION: OK` (re-audit after the writes, not before);
4. `DATA INTEGRITY STATUS: PASS` and `REGRESSION TESTS: PASS`;
5. module counts: `MODULES FAILED: 0`, and the new module appears under
   `READY`/`REGISTERED` in `/admin/modules`;
6. `tools/consistency_report.py` agrees (it is the same suite the run used);
7. the deploy gate recorded `Update Gate ✔`;
8. a backup from this run is listed in `ams_update_run.backup_path`;
9. `docs/MODULE_REGISTRY.md` regenerated (automatic when revisions were applied);
10. `git status` clean of runtime artefacts (reports are gitignored).

## 8a. Bulk legacy loads are a separate, gated procedure

A new `ALLEXPORT-*.xlsx` snapshot from the legacy system is staged in
`legacy data/` (see `tools/migrate/README.md`). It is **never** touched by the
update pipeline:

* the pipeline migrates *structure* and small, reviewed data corrections — it is
  not an importer, and an importer must never run at startup over a live ledger;
* the legacy load is the gated 5-step procedure in `tools/migrate/`
  (audit → clean export → verify the export → post-import SQL audit → load with
  `--confirm`), because the app's raw importer restores the workbook verbatim
  and the purge rules have to be applied first;
* after such a load, run `python tools/dbupdate.py check` and
  `python tools/dbupdate.py integrity`: the pipeline's row-count guard compares
  the pre/post snapshots, so a load that dropped rows is visible in the report
  rather than discovered weeks later in a reconciliation.

`tools/dbupdate.py audit-schema` is also the command to run after a load: it
tells you whether the imported database matches what the models and modules
expect (extra columns and unmanaged tables are reported as observations, never
"cleaned up").

## 9. What this pipeline will not do

* no blind `create_all()` on a populated production database;
* no automatic `DROP TABLE` / `DROP COLUMN` / `ALTER COLUMN` — such drift is
  reported as `MANUAL` for a human to schedule;
* no database reset, no reseed over real data;
* no importing of code that has not validated (discovery reads manifests as
  data; `MODULE_CONFIG` is read with `ast`, not executed);
* no activation of a module whose dependencies are unresolved;
* no success claimed without the verification steps above.
