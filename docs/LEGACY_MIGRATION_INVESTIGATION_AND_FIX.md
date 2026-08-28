# Legacy Data Migration — Deep Investigation & Repair Report

Date: 2026-08-28 · Incident: `ALLEXPORT-28-08-2026_12-35PM.xlsx` validated as **“TOTAL ROWS: 0”** · Branch: `arena/01a047b5-rep-new-0001`

---

## 1. CURRENT MIGRATION ARCHITECTURE (as actually implemented)

There are **two separate import systems** in this ERP; the Legacy Migration screen is the one that was used.

### 1.1 Legacy Data Migration (the feature in the incident)

Execution path (traced, reproduced, fixed):

```
USER selects template "Clients" + .xlsx in dropdown
  └─ templates/legacy_migration.html  → form POST /legacy-migration/upload
       └─ app/blueprints/migration.py :: upload()            (route)
            ├─ guard: admin/root or can_import_export
            ├─ reads request.files['file'] into memory (file.read(); NOT stored)
            └─ app/services/legacy_migration.py :: validate_upload(kind, raw, filename, actor)
                 ├─ load_workbook(...)                ← openpyxl (library), data_only=True
                 ├─ spec = TEMPLATES[kind]            ← template = hard dict of sheet-name → exact header list
                 ├─ for each spec sheet:
                 │    if sheet NOT in wb.sheetnames → append issue {"Required sheet is missing."} → continue
                 │    if row-1 headers != template headers → append issue → continue
                 │    else parse rows from row 2 (EXAMPLE- rows skipped)
                 ├─ per-row: required-field, date (YYYY-MM-DD), numeric checks
                 ├─ Legacy Reference uniqueness: in-workbook + vs migration_mapping table (EXACT_DUPLICATE)
                 ├─ creates MigrationRun (status VALIDATED) + MigrationRow records, summary counts
                 └─ returns (run, issues)   ← ⚠ the route does `run, _ = validate_upload(...)`
     ↓
/legacy-migration/run/<id> preview  → legacy_migration_preview.html renders summary_json cards + rows
/legacy-migration/run/<id>/dry-run  → flashes summary_json, changes nothing
/legacy-migration/run/<id>/import   → import_run() — ONLY CLIENTS/SUPPLIERS/MATERIALS/ACCOUNTS
     (masters via SQLAlchemy model adapters); every other kind raises
     “…import is locked pending its domain-service adapter” — this is the
     “transaction templates stay locked” sentence quoted on screen.
/legacy-migration/run/<id>/errors.xlsx → per-row error export
```

Key files: `app/blueprints/migration.py` (58-line route module), `app/services/legacy_migration.py`
(`TEMPLATES`, `validate_upload`, `import_run`, `template_workbook`), `models/migration.py`
(`migration_run`, `migration_row`, `migration_mapping` tables — created by the app’s
`db.create_all()` bootstrap, see `app/services/dbupdate/legacy_steps.py`).

### 1.2 Import & Export “Full Raw” system (different tool, correctly handles this file)

`blueprints/import_export/` (`_pages_full_raw_export.py`, `_pages_full_raw_import.py`,
`engine.py::_run_full_raw_import_bytes`) exports/imports **the whole database as one XLSX per
table**, using pandas + SQLAlchemy Core, FK-ordered, with a `__AMS_META__` sheet. The incident
file contains `export_kind = literal_all` in `__AMS_META__` — **this file was produced by
`Full Raw Export`** of the old deployment of this very app, and its designed counterpart is
`/import_export/full_raw_import`, not the migration wizard. That tool was left untouched
(used only as the reference for what the file actually is).

---

## 2. CURRENT SUPPORTED ENTITIES

Official templates (`TEMPLATES` in `legacy_migration.py`), each keyed to current models:

| Template | Sheets required (exact names + exact headers) | Imports into |
|---|---|---|
| CLIENTS / SUPPLIERS / MATERIALS / ACCOUNTS | `DATA_ENTRY` | `Client` / `Supplier` / `Material` / `Account` via controlled model adapters |
| GRN, BOOKINGS, SALES, DIRECT_SALES, DELIVERIES, PAYMENTS, EXPENSES, OPENING_BALANCES | named header/item sheets (e.g. `GRN_HEADERS`+`GRN_ITEMS`) | **validation only** |

## 3. CURRENT BLOCKED ENTITIES

Everything except `{'CLIENTS','SUPPLIERS','MATERIALS','ACCOUNTS'}` is hard-blocked in
`import_run()` by design (`# Only low-risk master data is presently enabled…`), deliberately
preventing raw INSERTs that would bypass stock, ledger, account, balance, and duplicate logic.
This gate is **kept** after the fix; transaction rows are now fully *counted, mapped, validated
and previewed*, but importing them requires wiring each kind to its domain service (documented
next step, not silently bypassed).

## 4. ZERO ROW ROOT CAUSE (exact, code-level)

The workbook has **63 sheets / 26,705 data rows**, but **no sheet named `DATA_ENTRY`** — because
it is a raw per-table database export (`client`, `booking`, `payment`, `invoice`, …).

In `validate_upload` (pre-fix):

```python
for sheet, headers in spec['sheets'].items():
    if sheet not in wb.sheetnames:
        issues.append(_problem(sheet, 0, 'Sheet', 'Required sheet is missing.')); continue
```

→ zero sheets parsed → `parsed=[]` → every counter 0 → `summary = {"READY":0,…,"Total Rows":0}`,
run stored `VALIDATED`. Meanwhile the route line

```python
run, _ = validate_upload(kind, file.read(), file.filename, current_user.username)
```

**discards `issues`** — the only diagnostic the system ever produced. It was not persisted on the
run, not flashed, not rendered on the preview, not present in errors.xlsx (rows-only). So the user
saw a green “Validation complete” + all-zero counters, which is indistinguishable from “file read,
nothing inside”.

Classification vs the checklist: **B + C + D + R** (sheet-name mismatch / exact-template-only
design / exact header equality / “requires an exact template rather than intelligently mapping”)
**plus a genuine defect in the API/UI contract (M/L-ish)**: the backend *did* detect the problem
and threw the detection away. It is *not* an openpyxl parsing problem, not merged cells, not
hidden-sheet or empty-row detection — those never got the chance to run.

Secondary contributors found while tracing:
* `validate_upload` skipped only `Legacy Reference`-prefixed `EXAMPLE-` rows on any sheet, and `header` equality compared *all* row-1 cells (any extra column fails it).
* `dry-run` just re-flashed the same zero summary; nothing could distinguish “locked transaction” from “empty workbook”.
* `docs/legacy_migration_mapping.md` states the design (“does not accept a legacy database dump”) — the behavior was *intended* to reject; only the **silent** rejection was the bug. But the requirement now is: reject *loudly*, or adapt. Both were implemented.

## 5. XLSX STRUCTURE ANALYSIS (actual file inspection)

63 sheets, all DB-table shaped (row 1 = DB column names, ISO datetimes, `_minor` money columns).
Excerpt of the real profile the fixed importer now produces:

| Sheet | Rows | Cols | Detected entity | Conf. | Handling |
|---|---:|---:|---|---|---|
| client | 315 | 21 | CLIENTS | HIGH | adapted: name→Client Name, phone→Phone, address→Address, category→Category, opening_balance→Legacy Expected Due, code→Legacy Reference |
| supplier | 6 | 8 | SUPPLIERS | HIGH | adaptable |
| material | 66 | 9 | MATERIALS | HIGH | name/unit/unit_price/total; `category_id` resolved via `material_category` sheet |
| account | 12 | 21 | ACCOUNTS | HIGH | mappable (note: balances are *computed* values — reconciliation only) |
| booking / booking_item | 411 / 948 | 14 / 5 | BOOKINGS | HIGH | validated, import locked, references resolved against masters |
| direct_sale / direct_sale_item | 2,551 / 4,693 | 24 / 7 | DIRECT_SALES | HIGH | validated, locked |
| payment / supplier_payment | 793 / 78 | 29 / 23 | PAYMENTS | HIGH | validated, locked |
| grn / grn_item | 48 / 48 | 25 / 7 | GRN | HIGH | validated, locked |
| invoice / pending_bill | 2,308 / 1,612 | — | SALES / derived | — | pending_bill & entry are ledger-derived (`source_module/source_table` cols) → IGNORED *with reason* |
| entry | 4,887 | 26 | (derived ledger) | — | IGNORED explicitly — “recomputed, not importable” |
| audit_log, accounting_audit_log, user, settings, bill_counter, __AMS_META__, … | 1,188+ | — | internal | — | IGNORED with reason; empty sheets flagged separately |

No merged cells anywhere; no hidden sheets; empty sheets (`delivery`, `fbm_client`, `cash_flow_party`, …) detected as empty.

## 6. LEGACY VS CURRENT ERP GAP

* Legacy export keys entities by **numeric ids** and **names** (`client_name`, `material_name`,
  `booking_id`); the current schema wants `Client`/`Material` records, codes (`FBMCL-…`), and FK ids.
* Money: current model stores `balance`/`balance_minor`, `paid_amount`, `amount_minor`; the old
  export carries both — reconciliation values, never authoritative imports.
* `invoice`/`pending_bill`/`entry` are **derived ledgers** in the current app (rebuilt from
  sales/payments/bookings) — importing them directly would double-count. The old app’s own
  raw-export tooling already treats them as computed; the migration layer now refuses them
  explicitly instead of silently.
* `material_return`/`material_return_item`, `waive_off`, `delivery_rent`, `sale_delivery_persons`,
  `booking_allocation`, `grn_allocation` have **no official migration template at all** in the
  old code (they were invisible). They are now discovered and reported; returns/waivers remain
  future adapters.
* Dates mix ISO strings and Excel datetimes; numeric values arrive as floats ('7761.0'); both
  are normalized in the adapter.

## 7. DEPENDENCY IMPORT ORDER (from the real schema)

`CLIENTS, SUPPLIERS, MATERIALS(+categories), ACCOUNTS(+banks)` →
`OPENING_BALANCES` → `GRN(+items)` → `BOOKINGS(+items)` → `SALES`/`DIRECT_SALES(+items)` →
`DELIVERIES` → `PAYMENTS` → `EXPENSES` → reconciliation. Enforced at **validation** time now: a
transaction run whose master deps are not COMPLETED gets `PENDING DEPENDENCIES` and its
unresolvable-reference rows are `BLOCKED` (not invalid), with the expected order printed; after
masters import, revalidation flips them to READY/real-orphan.

## 8. WHAT WAS CHANGED (the fix)

**Zero-row bug — fixed at the contract level**
* `validate_upload` now *always* profiles every sheet first (`migration_discovery.profile_workbook`) and the run summary carries:
  `SOURCE ROWS FOUND / RECOGNIZED / MAPPED / UNMAPPED`, per-sheet report (rows, columns, header row, merged/hidden/empty flags, entity, confidence, evidence, status, **reason not imported**), and the previously-discarded workbook issues (`ISSUES`) — persisted, rendered on the preview page, and included in `errors.xlsx`.
* The route no longer throws away diagnostics; “Validation complete” is only flashed when rows were mapped; otherwise it warns: *“No rows could be mapped … (N source rows found but not mapped). X issue(s) listed in preview.”*
* Official-template exact path is preserved byte-compatible (`mode=EXACT_TEMPLATE`), including `EXAMPLE-` skipping; trailing empty header cells tolerated.

**Legacy adaptation layer (new `app/services/migration_discovery.py`)**
* Sheet discovery + header-row detection (text-density scoring, handles title rows above headers), column profiling (filled/numeric/date/distinct + samples), merged/hidden/empty/duplicate-name detection, amount/qty totals for reconciliation.
* Entity detection = sheet-name tokens **and** header signatures **and** data shape → confidence HIGH/MEDIUM/LOW/UNKNOWN, with anti-fool guards (auxiliary `*_category`/counter tables, audit/derived-ledger tables, `*_item` children can’t masquerade as masters; movement registers penalized on master entities).
* Column mapping = priority-ordered alias lists (Client/Customer/Party, MOBILE NO/Phone, Qty/Bags, Rate/Price/Price at time, Bill/Invoice/Auto bill no, …) mapping legacy → normalized → template field; configurable dict, extendable per future legacy file.
* Adaptation to template-shaped rows: FK-number resolution from in-workbook lookup sheets (`category_id→category name`, `payment_account_id→account name`), missing-reference derivation as **class-B controlled default** (`AUTO-<sheet>-<row>-<sha1>` — stable, recorded, still keeps duplicate protection), MEDIUM-confidence rows → `WARNING` (never auto-imported), never-guess rules kept for identities.
* Duplicate levels: L1 source row content & reference uniqueness in-workbook; L2 prior `migration_mapping` reference → `EXACT_DUPLICATE`; possible business duplicate (case-insensitive name match vs live tables) → `WARNING` for review — no auto-merge.
* Orphans: unresolved Client/Supplier/Party/Material/Account references → `ORPHAN` with fuzzy candidate suggestions; item rows whose parent key is absent → `ORPHAN`; import-order-unresolvable → `BLOCKED` with the ordered remedy. Revalidation after master import is a first-class flow.

**Wizard (UI)**
* `/legacy-migration/` upload accepts **AUTO** (analyze) as default choice; source file is preserved under `instance/migration/uploads/<run-key>.xlsx` (never modified) so analysis runs can *prepare* per-entity validation runs (`POST /run/<id>/prepare/<ENTITY>`) without re-uploading.
* Preview page = steps 2–9: count cards incl. **SOURCE ROWS FOUND**, sheet discovery table, applied column mapping, detected-entities + one-click “Validate as <ENTITY>”, required import order, row-level preview with statuses/problems, error-XLSX incl. sheet-level issues, post-import **reconciliation panel** (`RECONCILE`: target counts before/after, created delta, balanced?, not-imported counts).
* Dry-run now flashes the real per-status counts instead of the raw JSON blob.

**What was deliberately NOT changed**
* `import_run` master-only gate and its model adapters (no stock/ledger/balance bypass; transactions remain locked pending domain-service adapters — per your rules and the app’s own safety statement).
* Database schema: zero new columns/tables — all analysis lives in `migration_run.summary_json` and preserved upload files, so nothing outside the feature is touched and no DB migration is needed.
* The `import_export` full-raw restore tool (correct home for whole-DB restores; the migration dashboard now cross-links to it).

## 9. VERIFICATION

* `tests/test_legacy_migration_discovery.py` — 8 tests: real-file profiling (63 sheets / 26,705 rows / client=315 HIGH), CLIENTS end-to-end (315 READY → 315 imported → re-upload = 315 EXACT_DUPLICATE), BOOKINGS blocked→revalidated with real statuses & locked import, ANALYSIS + prepare flow, synthetic legacy sheet (`CUSTOMER MASTER` with `Customer Name`/`MOBILE NO`/`Balance` → adapted, invalid-name row INVALID, junk sheet NOT_MAPPED with reason), official-template regression, no-match workbook = loud BLOCKED with counts, full UI render + errors.xlsx + upload POST via HTTP.
* Full suite: **222 passed, 0 failed** (512 s).
* Live app check (this sandbox): uploading the exact incident file to `/legacy-migration/` now shows
  `SOURCE ROWS FOUND 26705 · RECOGNIZED 18470 · MAPPED 315 · UNMAPPED 26390 · READY 315`, the missing-sheet explanation, the applied mapping panel, and the discovery table for all 63 sheets.

## 10. RECOMMENDED NEXT STEPS (each independently safe)

1. Wire transaction adapters to the existing services (`sales_core`, `grn_svc`, `finance_clients`, ledger/accounting services) one kind at a time and unlock their import buttons — validation/preview data for them already exists today.
2. Material returns / waive-offs: add TEMPLATES entries + aliases (data exists in the workbook; detection already labels them).
3. Post-import business verification beyond master-count reconciliation (stock equation, client outstanding) inside the same run summary once (1) lands — hooks `RECONCILE` block already present.
