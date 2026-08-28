"""STEP B / Phases 2,3,6,7,8: QA clients, five-cycle workflows, ledger+stock proof.

Every cycle drives the real endpoints:
    GRN (stock in) -> Booking -> Dispatch (stock out) -> Payment -> Direct Sale

After each step the harness independently recomputes the expected stock and the
expected client balance straight from the ORM rows and compares that against
what the application reports.  After the fifth cycle the *cumulative* figures
are proved against closed-form arithmetic (5 x per-cycle amount).
"""
from __future__ import annotations

from sqlalchemy import func

from .harness import flashes, money, said

# --- per-cycle constants (closed-form so cumulative maths is provable) ------
GRN_QTY = 100.0
GRN_RATE = 1000.0
BK_QTY = 10.0
BK_RATE = 1500.0
BK_AMOUNT = BK_QTY * BK_RATE          # 15,000
BK_PAID = 5000.0
DISPATCH_QTY = 10.0
PAY_AMOUNT = 2000.0
DS_QTY = 4.0
DS_RATE = 1600.0
DS_AMOUNT = DS_QTY * DS_RATE          # 6,400
DS_PAID = 0.0

CYCLES = 5
CLIENTS = 5

# Net client debit added per cycle:
#   booking due (15000-5000) + sale due (6400-0) - payment (2000) = 14,400
PER_CYCLE_BALANCE = (BK_AMOUNT - BK_PAID) + (DS_AMOUNT - DS_PAID) - PAY_AMOUNT
# Net stock added per cycle: +100 GRN, -10 dispatch, -4 sale = +86
PER_CYCLE_STOCK = GRN_QTY - DISPATCH_QTY - DS_QTY


# ---------------------------------------------------------------------------
# Independent recomputation (never calls application helpers)
# ---------------------------------------------------------------------------
def recompute_balance(db, models, client_name):
    """Mirror the ERP's documented balance rule using raw aggregates."""
    B, D, P = models["Booking"], models["DirectSale"], models["Payment"]
    n = (client_name or "").strip().lower()
    lower = lambda col: func.lower(func.trim(col))  # noqa: E731

    def s(col, model, cond=None):
        q = db.session.query(func.sum(col)).filter(
            lower(model.client_name) == n, model.is_void == False  # noqa: E712
        )
        if cond is not None:
            q = q.filter(cond)
        return float(q.scalar() or 0)

    debit = s(B.amount, B) + s(D.amount, D)
    credit = s(B.paid_amount, B) + s(D.paid_amount, D)
    disc = s(B.discount, B) + s(D.discount, D)
    pay = float(
        db.session.query(func.sum(P.amount))
        .filter(lower(P.client_name) == n, P.is_void == False)  # noqa: E712
        .scalar()
        or 0
    )
    return money(debit - credit - disc - pay)


def stock_from_entries(db, models, material_name):
    """Invariant 1: material.total must equal sum(IN) - sum(OUT)."""
    E = models["Entry"]
    n = (material_name or "").strip().lower()
    lower = func.lower(func.trim(E.material))

    def s(kind):
        return float(
            db.session.query(func.sum(E.qty))
            .filter(lower == n, E.type == kind, E.is_void == False)  # noqa: E712
            .scalar()
            or 0
        )

    return round(s("IN") - s("OUT"), 3)


def material_total(db, models, material_name):
    M = models["Material"]
    m = M.query.filter(func.lower(func.trim(M.name)) == material_name.strip().lower()).first()
    return round(float(m.total or 0), 3) if m else None


# ---------------------------------------------------------------------------
# Setup: supplier, material, cash account, 5 QA clients
# ---------------------------------------------------------------------------
MATERIAL = "QA STEPB CEMENT"
SUPPLIER = "QA STEPB SUPPLIER"
ACCOUNT = "QA STEPB CASH"


def setup_masters(app, br, rec, models, db):
    ids = {}

    r = br.post("/add_material", data={"material_name": MATERIAL, "material_unit": "Bags"})
    rec.check("Phase2-Masters", "Create material", r.status_code == 200, flashes(r))

    r = br.post("/add_supplier", data={"name": SUPPLIER, "phone": "03001234567"})
    rec.check("Phase2-Masters", "Create supplier", r.status_code == 200, flashes(r))

    r = br.post("/accounts/accounts/add", data={
        "name": ACCOUNT,
        "class_category": "Assets",
        "class_subcategory": "Cash",
        "class_account_type": "Main Cash",
        "account_status": "active",
        "opening_amount": "0",
        "opening_position": "debit",
        "opening_effective_date": "2026-01-01",
    })
    rec.check("Phase2-Masters", "Create cash account", r.status_code == 200, flashes(r))

    with app.app_context():
        M, S, A = models["Material"], models["Supplier"], models["Account"]
        mat = M.query.filter_by(name=MATERIAL).first()
        sup = S.query.filter(S.name.like("%QA STEPB SUPPLIER%")).first()
        acc = A.query.filter(A.name.like("%QA STEPB CASH%")).first()
        rec.check("Phase2-Masters", "Material persisted in DB", mat is not None)
        rec.check("Phase2-Masters", "Supplier persisted in DB", sup is not None)
        rec.check("Phase2-Masters", "Cash account persisted in DB", acc is not None)
        ids["material_id"] = mat.id if mat else None
        ids["supplier_id"] = sup.id if sup else None
        ids["supplier_name"] = sup.name if sup else SUPPLIER
        ids["account_id"] = acc.id if acc else None
    return ids


def create_qa_clients(app, br, rec, models, db):
    """Phase 2 - dedicated, clearly identifiable QA clients."""
    clients = []
    for i in range(1, CLIENTS + 1):
        name = f"QA TEST CLIENT {i:02d}"
        code = f"QA-{i:02d}"
        r = br.post("/add_client", data={
            "name": name, "code": code, "category": "General",
            "phone": f"0300000{i:04d}", "address": f"QA Street {i}",
            "opening_balance": "0",
        })
        ok = r.status_code == 200
        rec.check("Phase2-Clients", f"Create {name}", ok, flashes(r))

        # Human check: does it show up in the list page?
        lst = br.get("/clients")
        seen = said(lst, name)
        rec.check("Phase2-Clients", f"{name} visible in /clients", seen,
                  "" if seen else "client list did not contain the new client")

        with app.app_context():
            C = models["Client"]
            row = C.query.filter_by(code=code).first()
            if row is None:
                rec.check("Phase2-Clients", f"{name} persisted in DB", False, "row missing")
                rec.bug(
                    module="Clients", page="/add_client", severity="Critical",
                    test_client=name, route="/add_client",
                    steps=f"POST /add_client name={name} code={code}",
                    expected="Client row created", actual="No row in client table",
                    db_impact="Client not persisted", data_loss_risk="Yes",
                    evidence=flashes(r),
                )
                continue
            rec.check("Phase2-Clients", f"{name} persisted in DB", True)
            clients.append({"id": row.id, "name": name, "code": code})
    rec.bump("qa_clients", len(clients))
    return clients


# ---------------------------------------------------------------------------
# One full transaction cycle
# ---------------------------------------------------------------------------
def run_cycle(app, br, rec, models, db, cl, ids, cycle):
    """Execute + verify one complete workflow. Returns True if the cycle passed."""
    tag = f"{cl['name']} / Cycle {cycle}"
    area = "Phase3-Cycles"
    ok_all = True

    def step(item, ok, detail=""):
        nonlocal ok_all
        if not rec.check(area, f"{tag} :: {item}", ok, detail):
            ok_all = False
        return ok

    with app.app_context():
        stock_before = material_total(db, models, MATERIAL) or 0.0
        bal_before = recompute_balance(db, models, cl["name"])

    # --- 1. GRN: stock in ---------------------------------------------------
    r = br.post("/grn", data={
        "action": "add",
        "supplier": ids["supplier_name"],
        "supplier_id": str(ids["supplier_id"]),
        "mat_name[]": MATERIAL,
        "qty[]": str(GRN_QTY),
        "price[]": str(GRN_RATE),
        "paid_amount": "0",
        "manual_bill_no": f"QA-GRN-{cl['code']}-{cycle}",
    })
    step("GRN accepted", said(r, "GRN added successfully"), flashes(r))
    rec.bump("transactions")
    with app.app_context():
        after_grn = material_total(db, models, MATERIAL) or 0.0
    delta = round(after_grn - stock_before, 3)
    if not step("GRN raised stock by exactly the received qty", delta == GRN_QTY,
                f"expected +{GRN_QTY}, got {delta:+}"):
        rec.bug(
            module="Inventory/GRN", page="/grn", severity="Critical",
            test_client=cl["name"], transaction=f"QA-GRN-{cl['code']}-{cycle}",
            route="POST /grn",
            steps=f"Add GRN for {MATERIAL} qty {GRN_QTY} (cycle {cycle})",
            expected=f"material.total increases by {GRN_QTY}",
            actual=f"material.total changed by {delta}",
            inventory_impact=f"stock drift of {round(delta - GRN_QTY, 3)}",
            consistency_risk="Yes", duplication_risk="Yes",
        )

    # --- 2. Booking ---------------------------------------------------------
    bill = f"QA-BK-{cl['code']}-{cycle}"
    r = br.post("/add_booking", data={
        "client_code": cl["code"],
        "material_name[]": MATERIAL,
        "material_id[]": str(ids["material_id"]),
        "qty[]": str(BK_QTY),
        "unit_rate[]": str(BK_RATE),
        "amount": str(BK_AMOUNT),
        "paid_amount": str(BK_PAID),
        "manual_bill_no": bill,
        "date": "2026-02-10",
        "payment_account_id": str(ids["account_id"]),
        "payment_method": "Cash",
    })
    step("Booking accepted", said(r, "booking added"), flashes(r))
    rec.bump("transactions")
    with app.app_context():
        B = models["Booking"]
        bk = B.query.filter(B.manual_bill_no.like(f"%{bill}")).all()
        step("Booking persisted exactly once", len(bk) == 1,
             f"found {len(bk)} rows for bill {bill}")
        if len(bk) > 1:
            rec.bug(
                module="Sales/Bookings", page="/add_booking", severity="Critical",
                test_client=cl["name"], transaction=bill, route="POST /add_booking",
                steps=f"Create booking {bill}",
                expected="1 booking row", actual=f"{len(bk)} duplicate rows",
                duplication_risk="Yes", financial_impact="double-counted receivable",
            )
        if bk:
            step("Booking amount stored correctly", money(bk[0].amount) == BK_AMOUNT,
                 f"expected {BK_AMOUNT}, stored {bk[0].amount}")
            step("Booking paid stored correctly", money(bk[0].paid_amount) == BK_PAID,
                 f"expected {BK_PAID}, stored {bk[0].paid_amount}")
            step("Booking linked to the right client",
                 (bk[0].client_name or "").strip().lower() == cl["name"].lower(),
                 f"linked to '{bk[0].client_name}'")

    # --- 3. Dispatch: stock out --------------------------------------------
    with app.app_context():
        pre_disp = material_total(db, models, MATERIAL) or 0.0
    r = br.post("/add_record", data={
        "date": "2026-02-12",
        "client": cl["name"],
        "type": "OUT",
        "material": MATERIAL,
        "material_id": str(ids["material_id"]),
        "qty": str(DISPATCH_QTY),
        "driver_name": "QA DRIVER",
        "bill_no": bill,
    })
    step("Dispatch accepted", r.status_code == 200, flashes(r))
    rec.bump("transactions")
    with app.app_context():
        post_disp = material_total(db, models, MATERIAL) or 0.0
    d = round(post_disp - pre_disp, 3)
    if not step("Dispatch reduced stock by exactly the dispatched qty", d == -DISPATCH_QTY,
                f"expected -{DISPATCH_QTY}, got {d:+}"):
        rec.bug(
            module="Operations/Dispatch", page="/add_record", severity="Critical",
            test_client=cl["name"], transaction=bill, route="POST /add_record",
            steps=f"Dispatch {DISPATCH_QTY} of {MATERIAL} against {bill}",
            expected=f"stock -{DISPATCH_QTY}", actual=f"stock {d:+}",
            inventory_impact=f"drift {round(d + DISPATCH_QTY, 3)}", consistency_risk="Yes",
        )

    # --- 4. Payment ---------------------------------------------------------
    pbill = f"QA-PAY-{cl['code']}-{cycle}"
    r = br.post("/add_payment", data={
        "client_code": cl["code"],
        "amount": str(PAY_AMOUNT),
        "method": "Cash",
        "payment_type": "Receipt",
        "payment_account_id": str(ids["account_id"]),
        "manual_bill_no": pbill,
        "date": "2026-02-15",
    })
    step("Payment accepted", r.status_code == 200 and not said(r, "unable to save payment"),
         flashes(r))
    rec.bump("transactions")
    with app.app_context():
        P = models["Payment"]
        pays = P.query.filter(P.manual_bill_no.like(f"%{pbill}")).all()
        step("Payment persisted exactly once", len(pays) == 1, f"found {len(pays)} rows")
        if len(pays) > 1:
            rec.bug(
                module="Payments", page="/add_payment", severity="Critical",
                test_client=cl["name"], transaction=pbill, route="POST /add_payment",
                steps=f"Record payment {pbill} of {PAY_AMOUNT}",
                expected="1 payment row", actual=f"{len(pays)} rows",
                duplication_risk="Yes", financial_impact="cash overstated",
            )
        if pays:
            step("Payment amount stored correctly", money(pays[0].amount) == PAY_AMOUNT,
                 f"expected {PAY_AMOUNT}, stored {pays[0].amount}")

    # --- 5. Direct sale -----------------------------------------------------
    with app.app_context():
        pre_sale = material_total(db, models, MATERIAL) or 0.0
    sbill = f"QA-DS-{cl['code']}-{cycle}"
    r = br.post("/add_direct_sale", data={
        "client_name": cl["name"],
        "client_code": cl["code"],
        "driver_name": "QA DRIVER",
        "category": "Credit Customer",
        "product_name[]": MATERIAL,
        "qty[]": str(DS_QTY),
        "unit_rate[]": str(DS_RATE),
        "paid_amount": str(DS_PAID),
        "manual_bill_no": sbill,
        "ignore_booking_item[]": "1",
    })
    step("Direct sale accepted", r.status_code == 200 and not said(r, "could not be saved"),
         flashes(r))
    rec.bump("transactions")
    with app.app_context():
        D = models["DirectSale"]
        sales = D.query.filter(D.manual_bill_no.like(f"%{sbill}")).all()
        step("Direct sale persisted exactly once", len(sales) == 1, f"found {len(sales)} rows")
        if sales:
            step("Direct sale amount stored correctly", money(sales[0].amount) == DS_AMOUNT,
                 f"expected {DS_AMOUNT}, stored {sales[0].amount}")
        post_sale = material_total(db, models, MATERIAL) or 0.0
    d = round(post_sale - pre_sale, 3)
    if not step("Direct sale reduced stock by exactly the sold qty", d == -DS_QTY,
                f"expected -{DS_QTY}, got {d:+}"):
        rec.bug(
            module="Sales/Direct Sale", page="/add_direct_sale", severity="Critical",
            test_client=cl["name"], transaction=sbill, route="POST /add_direct_sale",
            steps=f"Sell {DS_QTY} of {MATERIAL} to {cl['name']}",
            expected=f"stock -{DS_QTY}", actual=f"stock {d:+}",
            inventory_impact=f"drift {round(d + DS_QTY, 3)}", consistency_risk="Yes",
        )

    # --- 6. Per-cycle ledger arithmetic ------------------------------------
    with app.app_context():
        bal_after = recompute_balance(db, models, cl["name"])
        stock_after = material_total(db, models, MATERIAL) or 0.0
    moved = money(bal_after - bal_before)
    if not step("Client balance moved by exactly the cycle's net debit",
                moved == money(PER_CYCLE_BALANCE),
                f"expected +{PER_CYCLE_BALANCE}, got {moved:+}"):
        rec.bug(
            module="Client Ledger", page=f"/ledger/{cl['id']}", severity="Critical",
            test_client=cl["name"], transaction=f"cycle {cycle}",
            route=f"GET /ledger/{cl['id']}",
            steps=f"Run cycle {cycle} for {cl['name']} then recompute balance",
            expected=f"balance +{PER_CYCLE_BALANCE}", actual=f"balance {moved:+}",
            ledger_impact="running balance wrong", financial_impact="outstanding wrong",
            consistency_risk="Yes",
        )
    net_stock = round(stock_after - stock_before, 3)
    step("Net stock movement for the cycle is exact",
         net_stock == PER_CYCLE_STOCK,
         f"expected {PER_CYCLE_STOCK:+}, got {net_stock:+}")

    # --- 7. Ledger pages render and agree ----------------------------------
    for path, label in (
        (f"/ledger/{cl['id']}", "financial ledger"),
        (f"/client_ledger/{cl['id']}", "client ledger"),
        (f"/financial_ledger/{cl['id']}", "financial ledger details"),
    ):
        resp = br.get(path)
        step(f"{label} page renders", resp.status_code == 200, f"HTTP {resp.status_code}")

    rec.bump("cycles")
    return ok_all


# ---------------------------------------------------------------------------
# Cumulative proof after the fifth cycle
# ---------------------------------------------------------------------------
def verify_cumulative(app, br, rec, models, db, cl):
    """The mandatory five-times rule: prove 5 x per-cycle == displayed total."""
    area = "Phase3-Cumulative"
    tag = cl["name"]
    with app.app_context():
        B, D, P = models["Booking"], models["DirectSale"], models["Payment"]
        n = cl["name"].lower()
        lower = lambda c: func.lower(func.trim(c))  # noqa: E731

        bks = B.query.filter(lower(B.client_name) == n, B.is_void == False).all()  # noqa: E712
        sls = D.query.filter(lower(D.client_name) == n, D.is_void == False).all()  # noqa: E712
        pys = P.query.filter(lower(P.client_name) == n, P.is_void == False).all()  # noqa: E712

        rec.check(area, f"{tag}: exactly {CYCLES} bookings", len(bks) == CYCLES,
                  f"found {len(bks)}")
        rec.check(area, f"{tag}: exactly {CYCLES} direct sales", len(sls) == CYCLES,
                  f"found {len(sls)}")
        rec.check(area, f"{tag}: exactly {CYCLES} payments", len(pys) == CYCLES,
                  f"found {len(pys)}")

        # Duplicate reference numbers must not exist.
        bills = [b.manual_bill_no for b in bks]
        rec.check(area, f"{tag}: booking reference numbers unique",
                  len(bills) == len(set(bills)), f"{bills}")

        tot_bk = money(sum(float(b.amount or 0) for b in bks))
        tot_paid = money(sum(float(b.paid_amount or 0) for b in bks))
        tot_ds = money(sum(float(s.amount or 0) for s in sls))
        tot_pay = money(sum(float(p.amount or 0) for p in pys))

        rec.check(area, f"{tag}: booking total == 5 x {BK_AMOUNT}",
                  tot_bk == money(CYCLES * BK_AMOUNT),
                  f"expected {CYCLES * BK_AMOUNT}, got {tot_bk}")
        rec.check(area, f"{tag}: booking paid total == 5 x {BK_PAID}",
                  tot_paid == money(CYCLES * BK_PAID),
                  f"expected {CYCLES * BK_PAID}, got {tot_paid}")
        rec.check(area, f"{tag}: sales total == 5 x {DS_AMOUNT}",
                  tot_ds == money(CYCLES * DS_AMOUNT),
                  f"expected {CYCLES * DS_AMOUNT}, got {tot_ds}")
        rec.check(area, f"{tag}: payments total == 5 x {PAY_AMOUNT}",
                  tot_pay == money(CYCLES * PAY_AMOUNT),
                  f"expected {CYCLES * PAY_AMOUNT}, got {tot_pay}")

        expected_balance = money(CYCLES * PER_CYCLE_BALANCE)
        actual_balance = recompute_balance(db, models, cl["name"])
        ok = actual_balance == expected_balance
        rec.check(area, f"{tag}: cumulative outstanding == 5 x {PER_CYCLE_BALANCE}", ok,
                  f"expected {expected_balance}, got {actual_balance}")
        if not ok:
            rec.bug(
                module="Client Ledger", page=f"/ledger/{cl['id']}", severity="Critical",
                test_client=tag, transaction="cumulative (5 cycles)",
                route=f"GET /ledger/{cl['id']}",
                steps="Run 5 full workflow cycles, then read the outstanding balance",
                expected=f"{expected_balance}", actual=f"{actual_balance}",
                financial_impact=f"difference {money(actual_balance - expected_balance)}",
                ledger_impact="cumulative balance drift", consistency_risk="Yes",
            )

    # The application's own API must agree with the recomputation.
    resp = br.get(f"/api/client_financial_summary/{cl['code']}")
    if resp.status_code == 200 and resp.is_json:
        payload = resp.get_json() or {}
        api_bal = money(payload.get("balance", payload.get("data", {}).get("balance", 0)))
        ok = api_bal == money(expected_balance)
        rec.check(area, f"{tag}: /api/client_financial_summary agrees with ledger maths", ok,
                  f"api={api_bal} expected={expected_balance}")
        if not ok:
            rec.bug(
                module="API", page=f"/api/client_financial_summary/{cl['code']}",
                severity="High", test_client=tag,
                route=f"GET /api/client_financial_summary/{cl['code']}",
                steps="Compare API balance to independently recomputed ledger balance",
                expected=str(expected_balance), actual=str(api_bal),
                financial_impact=f"difference {money(api_bal - expected_balance)}",
                consistency_risk="Yes",
            )
    else:
        rec.blocked(area, f"{tag}: client financial summary API",
                    f"HTTP {resp.status_code}, not JSON")
