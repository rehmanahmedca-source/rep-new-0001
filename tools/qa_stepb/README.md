# STEP B — Deep human-like QA harness

Drives the **real** AMS ERP the way an employee would: logs in through `/login`,
walks every page, fills real forms, and then proves the consequences in the
database, the ledgers, the stock and the dashboard.

Nothing is stubbed. Every expected figure is **recomputed independently** from
the ORM rows, so a bug cannot hide by having a helper agree with itself.

## Run it

```bash
python -m tools.qa_stepb.run_audit
```

Outputs:

| File | What it is |
|---|---|
| `docs/STEP_B_QA_TEST_REPORT.md` | The human-readable audit + bug report |
| `docs/step_b_qa_results.json` | Every assertion, machine-readable |

It builds a throw-away SQLite database under `/tmp/qa_stepb_run` and never
touches a real instance. Takes ~20s.

## What it does

| Phase | Coverage |
|---|---|
| 1 | Login, then crawl every GET route **twice** — once with empty tables, once fully populated |
| 2 | Master data + 5 dedicated `QA TEST CLIENT nn` records |
| 3/6/7/8 | **5 clients × 5 cycles.** Each cycle: GRN → Booking → Dispatch → Payment → Direct Sale, with an exact stock and ledger delta assertion after every step |
| 5 | Field validation: empty, duplicate, zero, negative, non-numeric, decimal, 1000-char, special characters, XSS, unknown FK, CSRF |
| 9 | Cold **application restart** against the same file, then re-verify every total |
| 10 | Repeated/duplicate submission — double-click, duplicate reference numbers |
| 11 | Edit → verify delta, Void → verify reversal, Unvoid → verify restoration |
| 12 | Search semantics, date boundaries (single-day, reversed, invalid, empty), pagination |
| 13/14 | Dashboard vs reports vs ledgers vs DB; orphan and duplicate sweeps |
| 15 | Invalid routes, missing records, dependency deletion, anonymous access, bad password |

### The five-times rule

Per-cycle figures are deliberately closed-form so the cumulative result is
provable rather than merely "looks right":

```
per cycle:  balance += (15000 - 5000) + 6400 - 2000 = 14,400
            stock   += 100 - 10 - 4                 = +86
after 5:    balance  = 72,000     stock = +430 per client
```

## Structure

```
harness.py            app bootstrap, CSRF browser, bug/coverage recorder
phase1_discovery.py   route crawler with entity-aware id resolution
phase3_cycles.py      QA clients + the 5-cycle workflow engine
phases_deep.py        phases 5, 9, 10, 11, 12, 13/14, 15
report.py             renders the mandated report format
run_audit.py          orchestrator
```

A phase that crashes is recorded as a blocker and the audit continues, so one
broken module never hides the rest.

## Regression locks

`tests/test_stepb_qa_invariants.py` re-asserts the core arithmetic in CI. The
two reproduced defects are pinned with `xfail(strict=True)` — when they are
fixed those tests go **XPASS** and must be converted to plain asserts.
