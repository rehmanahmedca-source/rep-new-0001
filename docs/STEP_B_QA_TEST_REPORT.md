# AMS ERP — STEP B: DEEP HUMAN-LIKE QA TEST REPORT

**Generated:** 2026-08-28 06:46  
**Duration:** 17.6s  
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
| Total assertions executed | 1023 |
| Assertions passed | 964 |
| Assertions failed | 3 |
| Assertions blocked | 48 |
| Transaction workflows executed | 125 |
| Repeat cycles completed | 25 |
| QA clients used | 5 |

## BUG SUMMARY

| Severity | Count |
|---|---|
| Critical | 1 |
| High | 1 |
| Medium | 0 |
| Low | 0 |
| **Total** | 2 |

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
| Client Ledger Consistency | ❌ FAILED | 1/145 assertions failed |
| Account Balance Consistency | ✅ CONSISTENT | 66 assertions passed |
| Payment Consistency | ❌ FAILED | 1/124 assertions failed |
| Booking Consistency | ❌ FAILED | 2/169 assertions failed |
| Sales Consistency | ✅ CONSISTENT | 123 assertions passed |
| Dashboard Consistency | ✅ CONSISTENT | 12 assertions passed |
| Report Consistency | ✅ CONSISTENT | 26 assertions passed |
| Database Consistency | ✅ CONSISTENT | 122 assertions passed |

## DETAILED BUG REPORTS

### BUG-001 — [High] Payments

| Field | Detail |
|---|---|
| Module | Payments |
| Page | /add_payment |
| Severity | High |
| Test Client | QA TEST CLIENT 01 |
| Transaction | - |
| Route / API | POST /add_payment |
| Reproduction Steps | Submit a payment with a negative amount (-500) and check the stored row |
| Expected Result | Rejected with a validation error |
| Actual Result | Payment saved, and the amount was silently changed from -500 to 500.0 |
| Database Impact | - |
| Financial Impact | A user entering a negative figure (intending a refund or a correction) has it silently converted into a positive receipt, so cash and the client credit are both overstated. The correct route is payment_type='Refund', but nothing tells the user that. |
| Inventory Impact | - |
| Ledger Impact | client credited instead of debited |
| Data Loss Risk | No |
| Duplication Risk | No |
| Consistency Risk | Yes |
| Root Cause Suspected | app/services/payments_crud.py:334 - `submitted_minor = abs(to_minor(amount, field='Amount'))` discards the sign, and the direction is taken only from payment_type. A negative Receipt is therefore silently normalised to a positive one instead of being rejected. |
| Status | Reproduced |

### BUG-002 — [Critical] Sales/Bookings

| Field | Detail |
|---|---|
| Module | Sales/Bookings |
| Page | /void_transaction |
| Severity | Critical |
| Test Client | QA TEST CLIENT 02 |
| Transaction | booking 10 |
| Route / API | POST /void_transaction/Booking/10 |
| Reproduction Steps | Create booking 10, then POST /void_transaction/Booking/10 (the 'Void' action), then look for the booking again |
| Expected Result | The booking is marked is_void=True and stays visible in the void audit so it can be reviewed and restored |
| Actual Result | The booking row is permanently deleted. /unvoid_transaction silently does nothing and /void_audit never lists it. |
| Database Impact | Transaction row and its items are erased; only an AuditLog line remains |
| Financial Impact | A voided sale cannot be reviewed, re-checked or reinstated |
| Inventory Impact | - |
| Ledger Impact | History is rewritten - past ledger prints can no longer be reproduced |
| Data Loss Risk | Yes |
| Duplication Risk | No |
| Consistency Risk | Yes |
| Root Cause Suspected | app/blueprints/sales/_bills_void_transaction.py aliases void_transaction() to delete_transaction() -> hard_delete_transaction(); the paired unvoid_transaction() and the void_audit restore UI still assume a soft void. |
| Status | Reproduced |

<details><summary>Evidence</summary>

```
void_transaction(): 'Legacy URL kept so old forms still work; always hard-deletes.'
```

</details>

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

### Phase11-Reversal — **FAILED**

`10 passed · 2 failed · 0 blocked · 0 skipped`

| Status | Item | Detail |
|---|---|---|
| FAILED | voiding a booking soft-voids it rather than destroying the row | the row was hard-deleted from the database |
| FAILED | un-voiding a booking restores the record and the balance | row_restored=False; balance expected 75000.0, got 62000.0 |

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

### Phase5-Forms — **FAILED**

`12 passed · 1 failed · 0 blocked · 0 skipped`

| Status | Item | Detail |
|---|---|---|
| FAILED | add_payment handles negative amount | saved=True stored=500.0 expected_saved=False; flash: Payment received successfully. — by Admin |

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

**Verdict:** 3 failing assertions and 2 reproduced defects across 1023 checks and 25 full transaction cycles.

