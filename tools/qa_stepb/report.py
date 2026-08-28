"""Renders the STEP B audit results into the mandated report format."""
from __future__ import annotations

import datetime as _dt
from collections import OrderedDict, defaultdict

from .phase3_cycles import CLIENTS, CYCLES


def _table(rows, headers):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out)


def render(rec, elapsed, cycle_results=None, blocked_login=False) -> str:
    cycle_results = cycle_results or {}
    sev = rec.severity_counts()
    passed, failed = rec.passed, rec.failed
    skipped = [c for c in rec.checks if c.status == "SKIPPED"]
    blocked = [c for c in rec.checks if c.status == "BLOCKED"]

    # Page keys are "<crawl pass>|<route>"; collapse to the best result seen
    # for each distinct route so a route is not double counted.
    rank = {"PASSED": 3, "FAILED": 2, "BLOCKED": 1, "SKIPPED": 0}
    pages = {}
    for key, val in rec.pages.items():
        route = key.split("|", 1)[-1]
        cur = pages.get(route)
        if cur is None or rank[val["status"]] > rank[cur["status"]]:
            pages[route] = val
    p_pass = sum(1 for v in pages.values() if v["status"] == "PASSED")
    p_fail = sum(1 for v in pages.values() if v["status"] == "FAILED")
    p_block = sum(1 for v in pages.values() if v["status"] == "BLOCKED")
    p_skip = sum(1 for v in pages.values() if v["status"] == "SKIPPED")

    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    L = []
    A = L.append

    A("# AMS ERP — STEP B: DEEP HUMAN-LIKE QA TEST REPORT")
    A("")
    A(f"**Generated:** {now}  ")
    A(f"**Duration:** {elapsed:.1f}s  ")
    A("**Harness:** `tools/qa_stepb/` (drives the real Flask routes, real ORM, throw-away SQLite)  ")
    A("**Predecessor:** STEP A discovery report — `docs/SKILLS_BOOK.md` (complete)  ")
    A("")
    A("> Every figure below comes from an actual HTTP request against the real")
    A("> application and an **independent recomputation** from the ORM rows. No")
    A("> application helper is trusted to grade its own output.")
    A("")

    if blocked_login:
        A("## ❌ AUDIT BLOCKED AT LOGIN")
        A("")
        A("Authentication failed, so no further testing could run.")
        A("")

    # ------------------------------------------------------------------
    A("---")
    A("")
    A("## APPLICATION COVERAGE")
    A("")
    A(_table([
        ("Total routes discovered (GET, deduplicated)", len(pages)),
        ("Pages opened and tested", p_pass + p_fail),
        ("Pages passed", p_pass),
        ("Pages failed", p_fail),
        ("Pages blocked (no sample record to instantiate)", p_block),
        ("Pages skipped (destructive by policy)", p_skip),
        ("Total assertions executed", len(rec.checks)),
        ("Assertions passed", len(passed)),
        ("Assertions failed", len(failed)),
        ("Assertions blocked", len(blocked)),
        ("Transaction workflows executed", rec.counters.get("transactions", 0)),
        ("Repeat cycles completed", rec.counters.get("cycles", 0)),
        ("QA clients used", rec.counters.get("qa_clients", 0)),
    ], ["Metric", "Value"]))
    A("")

    # ------------------------------------------------------------------
    A("## BUG SUMMARY")
    A("")
    A(_table([
        ("Critical", sev.get("Critical", 0)),
        ("High", sev.get("High", 0)),
        ("Medium", sev.get("Medium", 0)),
        ("Low", sev.get("Low", 0)),
        ("**Total**", len(rec.bugs)),
    ], ["Severity", "Count"]))
    A("")
    if not rec.bugs:
        A("No defects were reproduced by this audit.")
        A("")

    # ------------------------------------------------------------------
    A("## REPEAT TEST RESULTS (the mandatory five-times rule)")
    A("")
    if cycle_results:
        rows = []
        for name, results in cycle_results.items():
            row = [name] + ["PASS" if r else "FAIL" for r in results]
            row += ["—"] * (CYCLES - len(results))
            rows.append(row)
        A(_table(rows, ["QA Client"] + [f"Cycle {i}" for i in range(1, CYCLES + 1)]))
        A("")
        A(f"Each cycle = GRN (stock in) → Booking → Dispatch (stock out) → Payment → "
          f"Direct Sale, followed by an exact ledger and stock delta assertion. "
          f"{CLIENTS} clients × {CYCLES} cycles.")
    else:
        A("_No cycles were executed._")
    A("")

    # ------------------------------------------------------------------
    A("## DATA INTEGRITY")
    A("")
    integ_areas = OrderedDict([
        ("Inventory Consistency", ("Inventory", "stock", "GRN", "Dispatch", "material")),
        ("Client Ledger Consistency", ("ledger", "balance")),
        ("Account Balance Consistency", ("account",)),
        ("Payment Consistency", ("payment", "Payment")),
        ("Booking Consistency", ("booking", "Booking")),
        ("Sales Consistency", ("sale", "Direct sale")),
        ("Dashboard Consistency", ("dashboard", "report/dashboard")),
        ("Report Consistency", ("report", "profit", "payables")),
        ("Database Consistency", ("orphan", "duplicate", "persisted", "restart")),
    ])
    rows = []
    for label, needles in integ_areas.items():
        rel = [c for c in rec.checks
               if any(n.lower() in c.item.lower() for n in needles)]
        bad = [c for c in rel if c.status == "FAILED"]
        if not rel:
            rows.append((label, "NOT COVERED", "—"))
        elif bad:
            rows.append((label, "❌ FAILED", f"{len(bad)}/{len(rel)} assertions failed"))
        else:
            rows.append((label, "✅ CONSISTENT", f"{len(rel)} assertions passed"))
    A(_table(rows, ["Area", "Result", "Evidence"]))
    A("")

    # ------------------------------------------------------------------
    A("## DETAILED BUG REPORTS")
    A("")
    if rec.bugs:
        for b in rec.bugs:
            A(f"### {b.bug_id} — [{b.severity}] {b.module}")
            A("")
            A(_table([
                ("Module", b.module), ("Page", b.page), ("Severity", b.severity),
                ("Test Client", b.test_client), ("Transaction", b.transaction),
                ("Route / API", b.route or "—"),
                ("Reproduction Steps", b.steps),
                ("Expected Result", b.expected),
                ("Actual Result", b.actual),
                ("Database Impact", b.db_impact),
                ("Financial Impact", b.financial_impact),
                ("Inventory Impact", b.inventory_impact),
                ("Ledger Impact", b.ledger_impact),
                ("Data Loss Risk", b.data_loss_risk),
                ("Duplication Risk", b.duplication_risk),
                ("Consistency Risk", b.consistency_risk),
                ("Root Cause Suspected", b.root_cause),
                ("Status", b.status),
            ], ["Field", "Detail"]))
            if b.evidence:
                A("")
                A("<details><summary>Evidence</summary>")
                A("")
                A("```")
                A(b.evidence[:1500])
                A("```")
                A("")
                A("</details>")
            A("")
    else:
        A("_None._")
        A("")

    # ------------------------------------------------------------------
    A("## TEST COVERAGE TRACKER")
    A("")
    by_area = defaultdict(list)
    for c in rec.checks:
        by_area[c.area].append(c)
    for area in sorted(by_area):
        items = by_area[area]
        np_ = sum(1 for c in items if c.status == "PASSED")
        nf = sum(1 for c in items if c.status == "FAILED")
        nb = sum(1 for c in items if c.status == "BLOCKED")
        ns = sum(1 for c in items if c.status == "SKIPPED")
        verdict = "FAILED" if nf else ("BLOCKED" if nb and not np_ else "PASSED")
        A(f"### {area} — **{verdict}**")
        A("")
        A(f"`{np_} passed · {nf} failed · {nb} blocked · {ns} skipped`")
        A("")
        shown = [c for c in items if c.status in ("FAILED", "BLOCKED")]
        if area.startswith("Phase1-Discovery"):
            # Blocked routes are enumerated once, deduplicated, under
            # UNTESTED AREAS - no need to repeat both crawl passes here.
            shown = [c for c in shown if c.status == "FAILED"]
        if shown:
            A(_table([(c.status, c.item, (c.detail or "")[:220]) for c in shown[:60]],
                     ["Status", "Item", "Detail"]))
            if len(shown) > 60:
                A("")
                A(f"_…and {len(shown) - 60} more non-passing items (see the JSON artifact)._")
        else:
            A("_All items in this area passed._")
        A("")

    # ------------------------------------------------------------------
    A("## FAILED ROUTES")
    A("")
    bad_pages = [(r, v["note"]) for r, v in sorted(pages.items()) if v["status"] == "FAILED"]
    if bad_pages:
        A(_table(bad_pages, ["Route", "Result"]))
    else:
        A("Every reachable page returned a non-error status.")
    A("")

    # ------------------------------------------------------------------
    A("## UNTESTED AREAS")
    A("")
    rows = []
    # Routes: report each distinct route once, using the best result across
    # both crawl passes.
    for route, val in sorted(pages.items()):
        if val["status"] in ("BLOCKED", "SKIPPED"):
            rows.append(("Route", route, val["note"]))
    # Non-route probes that could not run.
    for c in blocked + skipped:
        if not c.area.startswith("Phase1-Discovery"):
            rows.append((c.area, c.item, c.detail or "not run"))
    if rows:
        A(_table(rows[:200], ["Area", "Item", "Why it was not tested"]))
        if len(rows) > 200:
            A("")
            A(f"_…and {len(rows) - 200} more (see the JSON artifact)._")
    else:
        A("Nothing was skipped.")
    A("")
    A("Additionally **not** covered by this harness, and therefore not claimed as working:")
    A("")
    A("- Real-browser JavaScript behaviour (modals, client-side validation, double-click")
    A("  guards implemented purely in the front end). The harness drives HTTP, not a DOM.")
    A("- True concurrency / race conditions. Requests are issued sequentially, so")
    A("  simultaneous-writer races are out of scope.")
    A("- Destructive administrative routes (data wipe, dummy-data generation, tenant")
    A("  deletion, auto-deploy webhook) — excluded deliberately.")
    A("- Outbound integrations (e-mail backup delivery, GitHub deploy) — no network.")
    A("- The root-role / multi-tenant surface (`/tenants`, `/root/*`,")
    A("  `/import_export/tenant_db_export`). `require_root()` hard-disables these in")
    A("  single-store mode, so the audit only proves they stay closed — the features")
    A("  behind them are untested.")
    A("- PDF *visual* fidelity. Generation is exercised; pixel layout is not graded.")
    A("")
    A("---")
    A("")
    A(f"**Verdict:** {len(failed)} failing assertions and {len(rec.bugs)} reproduced defects "
      f"across {len(rec.checks)} checks and {rec.counters.get('cycles', 0)} full transaction cycles.")
    A("")
    return "\n".join(L) + "\n"
