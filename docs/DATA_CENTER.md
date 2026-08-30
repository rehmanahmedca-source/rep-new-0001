# Data Center — design & policy (import/export, restore, legacy data)

**Status:** implemented (engine `data_ops/`, UI `blueprints/import_export`, CLI `tools/data_ops.py`).
This document is the contract. Every rule below is enforced by code or by a loud failure — nothing is best-effort.

---

## 1. The stack decision

| Role | Format | Why |
|------|--------|-----|
| **Main database** | SQLite (single file, `APP_DB_PATH`) | Zero administration, file-level backup, Flask+SQLAlchemy fit, single yard PC / small LAN scale. |
| **Export — faithful snapshot** | `.db` (`VACUUM INTO`) | Only form that can rebuild schema *byte-for-byte* (DDL, indexes, FK pragmas, triggers). |
| **Export — portable archive** | `.json` | Machine transport: versioned, diffable, mergeable, idempotent. This is the **restore/merge** format. |
| **Export — human view** | `.xlsx` | Read-only display. **Never** a source of truth (Excel mangles types — `'7761.0'` account numbers — and can silently drop unknown columns). |
| **Restore** | `.json` (safe merge/upgrade) or `.db` (disaster replace) | JSON cannot recreate DDL, so `.db` stays the disaster path; JSON is the daily upgrade/merge path. |
| **Legacy data** | `.json` merge | Same engine as restore, with strict one-way merge rules (see §6). |

**XLSX import and CSV import are no longer part of the data path.** The old Excel import/export pages are left registered for backward compatibility only (flagged *Advanced / legacy* in the UI and reachable by URL); the sidebar points at the new Data Center.

---

## 2. The portable JSON envelope

Every export written by this system (backup folder `export.json`, UI/CLI `export-json`) is a **schema-aware archive**:

```json
{
  "kind": "ams.data-archive",
  "format_version": "2026-08",
  "app_version": "v44",
  "exported_at": "2026-08-30T12:00:00+00:00",
  "database": { "name": "…db", "sha256": "…", "journal_mode": "WAL" },
  "schema": {
    "payment": {
      "columns": [
        { "name": "id", "type": "INTEGER", "notnull": 0, "dflt": null, "pk": 1 },
        { "name": "client_id", "type": "INTEGER", "notnull": 0, "dflt": null, "pk": 0 }
      ],
      "primary_key": ["id"],
      "foreign_keys": [ { "from": "client_id", "table": "client", "to": "id" } ]
    }
  },
  "tables": { "payment": [ {"id": 1, "client_id": 7, …}, … ] },
  "stats": { "payment": { "rows": 120, "columns": 14 } }
}
```

- `format_version` is the single version string (`data_ops/constants.py`). Bump it only for a **non-additive** wire change (renames, retypes, removed fields). A new table or column is **additive** and needs **no version bump** — the planner diff handles it (§3).
- The `schema` block is what makes restore possible after the app has moved on: the planner compares *file schema* vs *target schema* directly instead of hoping server-side DDL matches a string.
- Legacy flat JSON (`{format_version, tables}`) written by earlier versions of this repo is still accepted — the planner infers the file schema from the rows.

---

## 3. Restore planning (the heart of versioning)

Restore is never a blind `INSERT`. Every restore is:

```
upload → plan (diff) → review (dry-run preview) → apply (one transaction) → verify
```

The plan is computed against the **live target schema**, which is the source of truth for "now":

| Situation | Policy |
|-----------|--------|
| Table in file, **missing in target** | Abort unless a declared `drop` step covers it. A file from a *newer* app must not silently lose data. |
| Table in file + in target | Planned; rows upserted on primary key `id` (`INSERT … ON CONFLICT(id) DO UPDATE`). |
| Table in target, **missing in file** | **Untouched** — never truncated, never cleared. This is the "new tables in new versions" guarantee: a 6-month-old export restores its data into today's DB, and every table added since then simply stays as it is (empty or live). Nothing is lost. |
| Column in file, missing in target | Covered by a `rename`/`drop`/`split` step → transformed in memory. Otherwise **abort before any write** (never drop data silently). |
| Column in target, missing in file | Filled by the column's own DEFAULT; else by a type-neutral value (`0`, `''`, …) and **reported** under `filled_missing` — never silently invented, never destructive. |
| Row with `is_void` truthy | **Refused** (soft-delete rows are not restored). Children of a voided parent are cascaded out of the load. |
| `client_id` / client text unknown | Synthetic `OrphanN` client created (merge policy), original identity kept in `notes`, reported. |
| Optional FK that doesn't resolve | Blanked (NULL) and reported, so a partial legacy file can still load. Required FK that doesn't resolve | Transaction fails → rollback → nothing changes. |
| Value that can't be coerced | Abort before any write. An unloadable row must never load half-corrupt. |
| File `format_version` newer than app | Abort with "upgrade the app first". |

### What "no data loss" means here
- Nothing in the *target* is deleted, truncated or replaced by a JSON restore (JSON restore is merge-only). The only destructive path is the explicit **DB snapshot restore** (§5), which replaces the file and requires typed confirmation plus an automatic pre-replace snapshot.
- Nothing in the *file* is silently discarded: unknown table/column, void row, dirty FK, or bad value is either handled through a declared rule **and reported**, or the whole operation aborts.

---

## 4. The apply transaction

1. **Safety snapshot first**: `VACUUM INTO` (fallback: online backup) of the live DB to `instance/storage/data_backups/backup_<stamp>/` — always, before the first write. Overridable only by explicit `skip_safety_backup=True` for test harnesses.
2. One `BEGIN`; `PRAGMA foreign_keys=OFF` during load (parents/children may arrive in any order), `ON` afterwards.
3. Parents first (`client`, then alphabetical), upsert by PK, named columns only (never positional `INSERT`).
4. `PRAGMA foreign_key_check` → any violation = `ROLLBACK` + abort.
5. `COMMIT` → `verify_database()` (FKs, dangling `client_id`, money totals, row counts).
6. Run recorded in `data_transfer_run` (actor, file, format versions, table stats, verdict).

Idempotent: applying the same file twice yields identical row counts (`restore-twice` proof in tests).

---

## 5. DB snapshot restore (disaster path)

Only path that can rebuild a *lost/empty/corrupt* database with exact schema. UI requires:

- an explicit checkbox + typed confirmation (`RESTORE`),
- an automatic pre-restore snapshot of the current file,
- a mandatory `PRAGMA integrity_check` + `foreign_key_check` on the uploaded file before touching anything,
- a post-restore verify; on failure the pre-restore snapshot is pointed to in the result screen.

---

## 6. Legacy data merge

Same engine with merge-oriented posting rules (they are the default for every restore):

- ids are kept as-is when free; conflicts upsert,
- unknown client identities become `OrphanN` clients (never silently skip a row),
- void rows refused, cascaded children skipped, unknown columns abort,
- every run is dry-run first and the diff report is shown before commit,
- each run stores the source filename + checksum + actor so merges are traceable,
- legacy `.db` files can be **converted to the JSON envelope** in one step (`convert-db → export-json`), then merged with the same rules — one code path for everything.

---

## 7. Retention & storage

- Backups: timestamped folders, never overwritten; `manifest.json` (sha256 of db + json, row counts, verify result); prune keeps last 30 daily + up to 52 weekly.
- Uploaded restore files live in `instance/.tmp/import_uploads/` (gitignored), referenced by a session token, deleted after apply or after 24 h.
- Export downloads are generated on demand (JSON/XLSX) or streamed from the snapshot folder (DB).

---

## 8. Permission model

All Data Center routes require `login_required` + the same gate as the old module: `can_import_export` or `role in ('admin','root')`. Apply/restore additionally logs the actor into `data_transfer_run`.

---

## 9. What the UI exposes (sidebar: **Data**)

| Page | Route | Purpose |
|------|-------|---------|
| Data Center | `GET /import_export/` | Overview: the three flows + latest runs + policy summary. |
| Export | `GET/POST /import_export/export` | Choose JSON (default), XLSX (display), or DB snapshot; full DB or one module group; per-file manifest. |
| Restore | `GET /import_export/restore` | Upload JSON → plan screen → apply. |
| Disaster snapshot | `GET/POST /import_export/restore/db` | Upload `.db` → integrity check → typed confirm → replace. |
| Legacy data | `GET/POST /import_export/legacy` | Convert legacy DB → JSON, upload legacy JSON, merge with diff preview. |
| History | `GET /import_export/history` | Every export/restore/merge run with actor, file, verdict. |
| *(Advanced)* old Excel wizard | `/legacy-migration/…`, `/import_export/…` (old routes) | Kept registered + linked as deprecated; not in the main sidebar flow. |

---

## 10. Test contract

`tests/test_data_ops.py` (pre-existing) + `tests/test_data_center.py`:

- backup → restore → restore again → identical row counts;
- old export into *newer* schema (new columns + new table) → data enters, new table stays empty/untouched, filled columns reported;
- unknown column → abort, no writes;
- void/orphan/coercion → refused / synthesized / coerced with report;
- rename step across versions;
- schema-aware round-trip: export with envelope → restore → same counts + `format_version` in manifest;
- new UI smoke: export page renders, restore plan + apply flow works end to end (CSRF-aware client).
