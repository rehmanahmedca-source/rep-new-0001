# AMS ERP — STEP B: DEEP HUMAN-LIKE QA TEST REPORT

**Generated:** 2026-08-28 07:07  
**Duration:** 17.5s  
**Harness:** `tools/qa_stepb/` (drives the real Flask routes, real ORM, throw-away SQLite)  
**Predecessor:** STEP A discovery report — `docs/SKILLS_BOOK.md` (complete)  

> Every figure below comes from an actual HTTP request against the real
> application and an **independent recomputation** from the ORM rows. No
> application helper is trusted to grade its own output.

---

## APPLICATION COVERAGE

| Metric | Value |
|---|---|
| Total routes discovered (GET, deduplicated) | 161 |
| Pages opened and tested | 140 |
| Pages passed | 140 |
| Pages failed | 0 |
| Pages blocked (no sample record to instantiate) | 17 |
| Pages skipped (destructive by policy) | 4 |
| Total assertions executed | 1022 |
| Assertions passed | 966 |
| Assertions failed | 0 |
| Assertions blocked | 48 |
| Transaction workflows executed | 125 |
| Repeat cycles completed | 25 |
| QA clients used | 5 |

## BUG SUMMARY

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |
| **Total** | 0 |

No defects were reproduced by this audit.

## REPEAT TEST RESULTS (the mandatory five-times rule)

| QA Client | Cycle 1 | Cycle 2 | Cycle 3 | Cycle 4 | Cycle 5 |
|---|---|---|---|---|---|
| QA TEST CLIENT 01 | PASS | PASS | PASS | PASS | PASS |
| QA TEST CLIENT 02 | PASS | PASS | PASS | PASS | PASS |
| QA TEST CLIENT 03 | PASS | PASS | PASS | PASS | PASS |
| QA TEST CLIENT 04 | PASS | PASS | PASS | PASS | PASS |
| QA TEST CLIENT 05 | PASS | PASS | PASS | PASS | PASS |

Each cycle = GRN (stock in) → Booking → Dispatch (stock out) → Payment → Direct Sale, followed by an exact ledger and stock delta assertion. 5 clients × 5 cycles.

## DATA INTEGRITY

| Area | Result | Evidence |
|---|---|---|
| Inventory Consistency | ✅ CONSISTENT | 183 assertions passed |
| Client Ledger Consistency | ✅ CONSISTENT | 144 assertions passed |
| Account Balance Consistency | ✅ CONSISTENT | 66 assertions passed |
| Payment Consistency | ✅ CONSISTENT | 123 assertions passed |
| Booking Consistency | ✅ CONSISTENT | 168 assertions passed |
| Sales Consistency | ✅ CONSISTENT | 123 assertions passed |
| Dashboard Consistency | ✅ CONSISTENT | 12 assertions passed |
| Report Consistency | ✅ CONSISTENT | 26 assertions passed |
| Database Consistency | ✅ CONSISTENT | 122 assertions passed |

## DETAILED BUG REPORTS

_None._

## TEST COVERAGE TRACKER

### Phase1-Discovery-Empty — **PASSED**

`126 passed · 0 failed · 31 blocked · 4 skipped`

_All items in this area passed._

### Phase1-Discovery-Populated — **PASSED**

`140 passed · 0 failed · 17 blocked · 4 skipped`

_All items in this area passed._

### Phase1-Login — **PASSED**

`2 passed · 0 failed · 0 blocked · 0 skipped`

_All items in this area passed._

### Phase10-Repeat — **PASSED**

`2 passed · 0 failed · 0 blocked · 0 skipped`

_All items in this area passed._

### Phase11-Reversal — **PASSED**

`11 passed · 0 failed · 0 blocked · 0 skipped`

_All items in this area passed._

### Phase12-Filters — **PASSED**

`15 passed · 0 failed · 0 blocked · 0 skipped`

_All items in this area passed._

### Phase13-Reconciliation — **PASSED**

`19 passed · 0 failed · 0 blocked · 0 skipped`

_All items in this area passed._

### Phase15-EdgeCases — **PASSED**

`9 passed · 0 failed · 0 blocked · 0 skipped`

_All items in this area passed._

### Phase2-Clients — **PASSED**

`15 passed · 0 failed · 0 blocked · 0 skipped`

_All items in this area passed._

### Phase2-Masters — **PASSED**

`6 passed · 0 failed · 0 blocked · 0 skipped`

_All items in this area passed._

### Phase3-Cumulative — **PASSED**

`50 passed · 0 failed · 0 blocked · 0 skipped`

_All items in this area passed._

### Phase3-Cycles — **PASSED**

`525 passed · 0 failed · 0 blocked · 0 skipped`

_All items in this area passed._

### Phase5-Forms — **PASSED**

`13 passed · 0 failed · 0 blocked · 0 skipped`

_All items in this area passed._

### Phase9-Persistence — **PASSED**

`33 passed · 0 failed · 0 blocked · 0 skipped`

_All items in this area passed._

## FAILED ROUTES

Every reachable page returned a non-error status.

## UNTESTED AREAS

| Area | Item | Why it was not tested |
|---|---|---|
| Route | /accounts/payments/suppliers/<int:id>/data | 404 - no sample record |
| Route | /cash_flow/entry/<int:entry_id>.json | 404 - no sample record |
| Route | /cash_flow_differences/<int:rec_id> | 404 - no sample record |
| Route | /debug/db | destructive |
| Route | /download_supplier_payment/<int:payment_id> | 404 - no sample record |
| Route | /fix_system_issues | destructive |
| Route | /generate_dummy_data | destructive |
| Route | /import_export/app_upgrade/status/<job_id> | no sample id |
| Route | /import_export/full_raw_import_report/<report_name> | no sample id |
| Route | /import_export/jobs/<int:job_id>/history | 404 - no sample record |
| Route | /import_export/jobs/<int:job_id>/progress | 404 - no sample record |
| Route | /import_export/master/import/status/<job_id> | no sample id |
| Route | /import_export/template/<dataset> | no sample id |
| Route | /import_export/uploads/<upload_id> | no sample id |
| Route | /legacy-migration/run/<int:run_id> | 404 - no sample record |
| Route | /legacy-migration/run/<int:run_id>/errors.xlsx | 404 - no sample record |
| Route | /legacy-migration/template/<kind> | no sample id |
| Route | /logout | destructive |
| Route | /root/backup-settings/history/download/<int:history_id> | 404 - no sample record |
| Route | /tenants/<tenant_id>/backup_history | 404 - no sample record |
| Route | /tenants/backup_history/download/<int:history_id> | 404 - no sample record |

Additionally **not** covered by this harness, and therefore not claimed as working:

- Real-browser JavaScript behaviour (modals, client-side validation, double-click
  guards implemented purely in the front end). The harness drives HTTP, not a DOM.
- True concurrency / race conditions. Requests are issued sequentially, so
  simultaneous-writer races are out of scope.
- Destructive administrative routes (data wipe, dummy-data generation, tenant
  deletion, auto-deploy webhook) — excluded deliberately.
- Outbound integrations (e-mail backup delivery, GitHub deploy) — no network.
- The root-role / multi-tenant surface (`/tenants`, `/root/*`,
  `/import_export/tenant_db_export`). `require_root()` hard-disables these in
  single-store mode, so the audit only proves they stay closed — the features
  behind them are untested.
- PDF *visual* fidelity. Generation is exercised; pixel layout is not graded.

---

**Verdict:** 0 failing assertions and 0 reproduced defects across 1022 checks and 25 full transaction cycles.

