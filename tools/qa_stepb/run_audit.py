"""STEP B deep-QA runner.

    python -m tools.qa_stepb.run_audit [--out docs/STEP_B_QA_TEST_REPORT.md]

Boots a throw-away instance of the real ERP, logs in, walks the whole
application, runs 5 QA clients x 5 full transaction cycles, then proves the
cumulative ledger / stock / dashboard figures.  Writes a Markdown report plus a
machine-readable JSON artifact.  It never touches a real database.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.qa_stepb.harness import Browser, Recorder, build_app  # noqa: E402
from tools.qa_stepb import phase1_discovery as P1  # noqa: E402
from tools.qa_stepb import phase3_cycles as P3  # noqa: E402
from tools.qa_stepb import phases_deep as PD  # noqa: E402
from tools.qa_stepb.report import render  # noqa: E402

MODEL_NAMES = (
    "Client", "Material", "Supplier", "Account", "Booking",
    "DirectSale", "Payment", "Entry", "GRN",
)


def guarded(rec, area, item):
    """Run a phase; a crash becomes a recorded bug instead of aborting the audit."""
    def deco(fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            rec.blocked(area, item, f"{type(exc).__name__}: {exc}")
            rec.bug(
                module=area, page=item, severity="High", route="-",
                steps=f"Run audit phase {area} / {item}",
                expected="Phase completes",
                actual=f"Phase aborted with {type(exc).__name__}: {exc}",
                evidence=tb[-1200:], status="Needs Investigation",
                root_cause="Unhandled exception reached the QA harness",
            )
            return None
    return deco


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/STEP_B_QA_TEST_REPORT.md")
    ap.add_argument("--json", default="docs/step_b_qa_results.json")
    ap.add_argument("--workdir", default="/tmp/qa_stepb_run")
    args = ap.parse_args()

    started = time.time()
    wd = Path(args.workdir)
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True, exist_ok=True)
    db_path = wd / "qa_stepb.db"

    rec = Recorder()

    # ---------------- Phase 1: login + discovery ----------------------------
    app = build_app(db_path, wd)
    br = Browser(app)
    resp = br.login()
    logged_in = resp.status_code in (302, 303)
    rec.check("Phase1-Login", "Admin can authenticate via /login", logged_in,
              f"HTTP {resp.status_code}")
    if not logged_in:
        rec.bug(module="Auth", page="/login", severity="Critical", route="POST /login",
                steps="Submit valid admin credentials",
                expected="302 redirect to the dashboard",
                actual=f"HTTP {resp.status_code}",
                evidence=resp.get_data(as_text=True)[:400], status="Open")
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        rec.dump(Path(args.json))
        Path(args.out).write_text(render(rec, time.time() - started, blocked_login=True))
        return 1

    dash = br.get("/")
    rec.check("Phase1-Login", "Dashboard loads after login", dash.status_code == 200,
              f"HTTP {dash.status_code}")

    import models as Mp
    db = Mp.db
    models = {n: getattr(Mp, n) for n in MODEL_NAMES}

    # ---------------- Phase 2: masters + QA clients -------------------------
    ids = guarded(rec, "Phase2-Masters", "master data setup")(
        lambda: P3.setup_masters(app, br, rec, models, db)) or {}
    clients = guarded(rec, "Phase2-Clients", "QA client creation")(
        lambda: P3.create_qa_clients(app, br, rec, models, db)) or []

    # ---------------- Phase 1 (cont): first walk, empty-state ---------------
    # Crawling before any transactions exist is the cheapest way to catch
    # "empty table" rendering bugs (Phase 15 of the mission brief).
    empty_sample = {
        "id": clients[0]["id"] if clients else None,
        "client_id": clients[0]["id"] if clients else None,
        "client_code": clients[0]["code"] if clients else None,
        "mat_id": ids.get("material_id"), "material_id": ids.get("material_id"),
        "account_id": ids.get("account_id"), "supplier_id": ids.get("supplier_id"),
        "type": "Booking", "trans_type": "Booking", "entity": "Booking",
        "tenant_id": "default",
    }
    guarded(rec, "Phase1-Discovery", "empty-state crawl")(
        lambda: P1.discover(app, br, rec, empty_sample, label="Phase1-Discovery-Empty"))

    # ---------------- Phase 3/6/7/8: five cycles per client -----------------
    cycle_results: dict[str, list[bool]] = {}
    for cl in clients:
        cycle_results[cl["name"]] = []
        for cycle in range(1, P3.CYCLES + 1):
            ok = guarded(rec, "Phase3-Cycles", f"{cl['name']} cycle {cycle}")(
                lambda cl=cl, cycle=cycle: P3.run_cycle(app, br, rec, models, db, cl, ids, cycle))
            cycle_results[cl["name"]].append(bool(ok))
        guarded(rec, "Phase3-Cumulative", f"{cl['name']} cumulative proof")(
            lambda cl=cl: P3.verify_cumulative(app, br, rec, models, db, cl))

    # ---------------- Phase 5: forms ---------------------------------------
    if clients:
        # Second, deeper crawl - now every list has data and we can address
        # real detail pages, modals and PDF exports.
        br.post("/delivery_persons/add",
                data={"name": "QA DELIVERY PERSON", "phone": "03009999999"})
        br.post("/add_pending_bill", data={
            "client_code": clients[0]["code"], "client_name": clients[0]["name"],
            "amount": "1000", "bill_no": "QA-PB-001", "date": "2026-02-01",
        })
        with app.app_context():
            newest = lambda M: M.query.order_by(M.id.desc()).first()  # noqa: E731
            bk = newest(models["Booking"])
            sale = newest(models["DirectSale"])
            pay = newest(models["Payment"])
            grn = newest(models["GRN"])
            dp = newest(Mp.DeliveryPerson) if hasattr(Mp, "DeliveryPerson") else None
            pb = newest(Mp.PendingBill) if hasattr(Mp, "PendingBill") else None
            full_sample = dict(empty_sample, **{
                "booking_id": bk.id if bk else None,
                "sale_id": sale.id if sale else None,
                "payment_id": pay.id if pay else None,
                "grn_id": grn.id if grn else None,
                "delivery_person_id": dp.id if dp else None,
                "pending_bill_id": pb.id if pb else None,
                "bill_id": pb.id if pb else None,
                "bill_no": (bk.manual_bill_no if bk else None) or "MB NO.1",
            })
        guarded(rec, "Phase1-Discovery", "populated crawl")(
            lambda: P1.discover(app, br, rec, full_sample, label="Phase1-Discovery-Populated"))

        guarded(rec, "Phase5-Forms", "form and field validation")(
            lambda: PD.phase5_forms(app, br, rec, models, db, ids, clients))

        # ---------------- Phase 10: repeated submissions --------------------
        guarded(rec, "Phase10-Repeat", "duplicate submission")(
            lambda: PD.phase10_repeat(app, br, rec, models, db, ids, clients))

        # ---------------- Phase 12: filters and dates -----------------------
        guarded(rec, "Phase12-Filters", "search, filter and date boundaries")(
            lambda: PD.phase12_filters(app, br, rec, models, db, clients))

        # ---------------- Phase 13/14: reconciliation -----------------------
        guarded(rec, "Phase13-Reconciliation", "dashboard/report/DB reconciliation")(
            lambda: PD.phase13_reconcile(app, br, rec, models, db, clients))

        # ---------------- Phase 9: cold restart persistence -----------------
        guarded(rec, "Phase9-Persistence", "cold restart")(
            lambda: PD.phase9_persistence(build_app, rec, models, clients, db_path, wd))

        # -------- Phases 11 & 15 mutate/destroy data, so they run last ------
        app3 = build_app(db_path, wd)
        br3 = Browser(app3)
        br3.login()
        import models as Mp3
        models3 = {n: getattr(Mp3, n) for n in MODEL_NAMES}
        models3["BookingItem"] = getattr(Mp3, "BookingItem", None)
        guarded(rec, "Phase11-Reversal", "edit, void and unvoid")(
            lambda: PD.phase11_reversal(app3, br3, rec, models3, Mp3.db, ids, clients))
        guarded(rec, "Phase15-EdgeCases", "error and edge cases")(
            lambda: PD.phase15_edges(app3, br3, rec, models3, Mp3.db, clients))

    # ---------------- Output ------------------------------------------------
    rec.counters["cycle_results"] = 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    jsn = Path(args.json)
    jsn.parent.mkdir(parents=True, exist_ok=True)
    rec.dump(jsn)
    out.write_text(render(rec, time.time() - started, cycle_results=cycle_results))

    sev = rec.severity_counts()
    print(f"checks: {len(rec.passed)} passed / {len(rec.failed)} failed / {len(rec.checks)} total")
    print(f"bugs:   {sev}")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
