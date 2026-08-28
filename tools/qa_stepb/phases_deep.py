"""STEP B / Phases 5, 9-15: validation, persistence, reversal, reconciliation.

Each function is an independent probe so a failure in one area never prevents
the rest of the audit from running (the mission's "record the blocker, keep
testing" rule).
"""
from __future__ import annotations

from sqlalchemy import func

from .harness import flashes, money, said
from .phase3_cycles import (
    ACCOUNT, CLIENTS, CYCLES, MATERIAL, PER_CYCLE_BALANCE,
    BK_AMOUNT, BK_PAID, DS_AMOUNT, PAY_AMOUNT,
    material_total, recompute_balance, stock_from_entries,
)


# ---------------------------------------------------------------------------
# PHASE 5 - deep form / field validation
# ---------------------------------------------------------------------------
def phase5_forms(app, br, rec, models, db, ids, clients):
    area = "Phase5-Forms"
    cl = clients[0]

    def count(model, **kw):
        with app.app_context():
            return model.query.filter_by(**kw).count() if kw else model.query.count()

    C = models["Client"]

    # --- required-field validation -----------------------------------------
    before = count(C)
    r = br.post("/add_client", data={"name": "", "code": "", "category": "General"})
    after = count(C)
    rec.check(area, "add_client rejects an empty name", after == before,
              f"client rows {before} -> {after}; flash: {flashes(r)}")
    if after != before:
        rec.bug(module="Clients", page="/add_client", severity="High",
                route="POST /add_client", steps="Submit the add-client form with no name",
                expected="Validation error, no row created",
                actual="A nameless client row was created",
                db_impact="junk row", data_loss_risk="No", consistency_risk="Yes")

    # --- duplicate client code ---------------------------------------------
    before = count(C)
    r = br.post("/add_client", data={
        "name": "QA DUPLICATE CODE", "code": cl["code"],
        "category": "General", "opening_balance": "0",
    })
    after = count(C)
    ok = after == before
    rec.check(area, "add_client rejects a duplicate client code", ok,
              f"rows {before} -> {after}; flash: {flashes(r)}")
    if not ok:
        rec.bug(module="Clients", page="/add_client", severity="High",
                test_client=cl["name"], route="POST /add_client",
                steps=f"Create a second client re-using code {cl['code']}",
                expected="Duplicate code rejected", actual="Second client accepted",
                duplication_risk="Yes", consistency_risk="Yes",
                db_impact="two clients share one code; ledger lookups become ambiguous")

    # --- special characters + long text ------------------------------------
    weird = "QA <script>alert(1)</script> & 'quote\" ünïcødé"
    r = br.post("/add_client", data={
        "name": weird, "code": "QA-SPECIAL", "category": "General",
        "address": "L" * 1000, "opening_balance": "0",
    })
    rec.check(area, "add_client survives special characters and 1000-char text",
              r.status_code == 200, f"HTTP {r.status_code}")
    body = r.get_data(as_text=True)
    escaped = "<script>alert(1)</script>" not in body
    rec.check(area, "client name is HTML-escaped on render (no stored XSS)", escaped,
              "raw <script> tag was echoed back into the page")
    if not escaped:
        rec.bug(module="Clients", page="/clients", severity="High",
                route="POST /add_client",
                steps="Create a client whose name contains a <script> tag, then view the list",
                expected="Name rendered escaped", actual="Script tag rendered unescaped",
                consistency_risk="Yes", root_cause="Missing autoescaping / |safe filter")

    # --- numeric edge cases on payments ------------------------------------
    P = models["Payment"]
    for label, amount, should_save in (
        ("zero amount", "0", False),
        ("negative amount", "-500", False),
        ("non-numeric amount", "abc", False),
        ("decimal amount", "1234.56", True),
    ):
        before = count(P)
        r = br.post("/add_payment", data={
            "client_code": cl["code"], "amount": amount, "method": "Cash",
            "payment_type": "Receipt", "payment_account_id": str(ids["account_id"]),
            "manual_bill_no": f"QA-EDGE-{label.replace(' ', '-')}",
            "date": "2026-03-01",
        })
        after = count(P)
        saved = after > before
        stored = None
        if saved:
            with app.app_context():
                stored = money(P.query.order_by(P.id.desc()).first().amount)
        ok = saved == should_save
        rec.check(area, f"add_payment handles {label}", ok,
                  f"saved={saved} stored={stored} expected_saved={should_save}; "
                  f"flash: {flashes(r)}")
        if not ok and saved and not should_save:
            sign_flipped = (
                label == "negative amount" and stored is not None and stored > 0
            )
            rec.bug(
                module="Payments", page="/add_payment",
                severity="High" if amount.startswith("-") else "Medium",
                test_client=cl["name"], route="POST /add_payment",
                steps=(f"Submit a payment with a {label} ({amount}) and check the "
                       f"stored row"),
                expected="Rejected with a validation error",
                actual=(f"Payment saved, and the amount was silently changed from "
                        f"{amount} to {stored}" if sign_flipped
                        else f"Payment saved with amount {stored}"),
                financial_impact=(
                    "A user entering a negative figure (intending a refund or a "
                    "correction) has it silently converted into a positive receipt, "
                    "so cash and the client credit are both overstated. The correct "
                    "route is payment_type='Refund', but nothing tells the user that."
                    if sign_flipped else
                    "cash/ledger balance corrupted by an invalid amount"),
                ledger_impact="client credited instead of debited" if sign_flipped else "-",
                consistency_risk="Yes",
                root_cause=(
                    "app/services/payments_crud.py:334 - "
                    "`submitted_minor = abs(to_minor(amount, field='Amount'))` "
                    "discards the sign, and the direction is taken only from "
                    "payment_type. A negative Receipt is therefore silently "
                    "normalised to a positive one instead of being rejected."
                    if sign_flipped else "Missing amount validation"),
            )
        # Keep the ledger arithmetic of later phases clean.
        if saved and label == "decimal amount":
            with app.app_context():
                row = P.query.order_by(P.id.desc()).first()
                rec.check(area, "decimal payment keeps 2dp precision",
                          money(row.amount) == 1234.56,
                          f"stored {row.amount}")
                db.session.delete(row)
                db.session.commit()
        elif saved:
            with app.app_context():
                db.session.delete(P.query.order_by(P.id.desc()).first())
                db.session.commit()

    # --- unknown foreign key ------------------------------------------------
    before = count(P)
    r = br.post("/add_payment", data={
        "client_code": "NO-SUCH-CLIENT-9999", "amount": "100", "method": "Cash",
        "payment_type": "Receipt", "payment_account_id": str(ids["account_id"]),
    })
    after = count(P)
    ok = after == before
    rec.check(area, "add_payment rejects an unknown client code", ok,
              f"rows {before} -> {after}; flash: {flashes(r)}")
    if not ok:
        rec.bug(module="Payments", page="/add_payment", severity="High",
                route="POST /add_payment",
                steps="Post a payment for a client code that does not exist",
                expected="Rejected", actual="Orphan payment created",
                db_impact="payment with no owning client", consistency_risk="Yes",
                data_loss_risk="Yes")
        with app.app_context():
            db.session.delete(P.query.order_by(P.id.desc()).first())
            db.session.commit()

    # --- negative / zero quantity on a sale ---------------------------------
    D = models["DirectSale"]
    for label, qty in (("zero quantity", "0"), ("negative quantity", "-5")):
        before = count(D)
        r = br.post("/add_direct_sale", data={
            "client_name": cl["name"], "client_code": cl["code"],
            "category": "Credit Customer", "product_name[]": MATERIAL,
            "qty[]": qty, "unit_rate[]": "1000", "paid_amount": "0",
            "manual_bill_no": f"QA-BADQTY-{qty}", "ignore_booking_item[]": "1",
        })
        after = count(D)
        ok = after == before
        rec.check(area, f"add_direct_sale rejects {label}", ok,
                  f"rows {before} -> {after}; flash: {flashes(r)}")
        if not ok:
            rec.bug(module="Sales/Direct Sale", page="/add_direct_sale", severity="High",
                    test_client=cl["name"], route="POST /add_direct_sale",
                    steps=f"Create a direct sale with {label}",
                    expected="Rejected", actual="Sale saved",
                    inventory_impact="stock moves by an impossible quantity",
                    financial_impact="revenue distorted", consistency_risk="Yes")
            with app.app_context():
                db.session.delete(D.query.order_by(D.id.desc()).first())
                db.session.commit()

    # --- CSRF gate must hold ------------------------------------------------
    raw = app.test_client()
    raw.get("/login")
    raw.post("/login", data={"username": "Admin", "password": "Admin@fbm12345"})
    r = raw.post("/add_client", data={"name": "QA NO CSRF", "code": "QA-NOCSRF"},
                 follow_redirects=True)
    with app.app_context():
        leaked = C.query.filter_by(code="QA-NOCSRF").count()
    rec.check(area, "mutating POST without a CSRF token is rejected", leaked == 0,
              f"{leaked} rows created without a token")
    if leaked:
        rec.bug(module="Security", page="/add_client", severity="Critical",
                route="POST /add_client",
                steps="POST the add-client form without _csrf_token",
                expected="403 / rejected", actual="Record created",
                root_cause="CSRF enforcement not applied to this endpoint",
                consistency_risk="Yes")


# ---------------------------------------------------------------------------
# PHASE 9 - reload / persistence
# ---------------------------------------------------------------------------
def phase9_persistence(app_builder, rec, models, clients, db_path, workdir):
    """Rebuild the app process against the same file: a genuine cold reload."""
    area = "Phase9-Persistence"
    from .harness import Browser

    app2 = app_builder(db_path, workdir)
    br2 = Browser(app2)
    r = br2.login()
    rec.check(area, "login still works after an application restart",
              r.status_code in (302, 303), f"HTTP {r.status_code}")

    import models as Mp
    db2 = Mp.db
    with app2.app_context():
        C, B, D, P = Mp.Client, Mp.Booking, Mp.DirectSale, Mp.Payment
        for cl in clients:
            row = C.query.filter_by(code=cl["code"]).first()
            rec.check(area, f"{cl['name']} survives restart", row is not None)
            if row is None:
                rec.bug(module="Clients", page="/clients", severity="Critical",
                        test_client=cl["name"], route="restart",
                        steps="Create client, restart the app, reload /clients",
                        expected="Client still present", actual="Client gone",
                        data_loss_risk="Yes", db_impact="record lost")
                continue
            n = cl["name"].lower()
            lo = lambda c: func.lower(func.trim(c))  # noqa: E731
            nb = B.query.filter(lo(B.client_name) == n, B.is_void == False).count()  # noqa: E712
            nd = D.query.filter(lo(D.client_name) == n, D.is_void == False).count()  # noqa: E712
            np_ = P.query.filter(lo(P.client_name) == n, P.is_void == False).count()  # noqa: E712
            rec.check(area, f"{cl['name']}: {CYCLES} bookings survive restart", nb == CYCLES,
                      f"found {nb}")
            rec.check(area, f"{cl['name']}: {CYCLES} sales survive restart", nd == CYCLES,
                      f"found {nd}")
            rec.check(area, f"{cl['name']}: {CYCLES} payments survive restart", np_ == CYCLES,
                      f"found {np_}")

            bal = recompute_balance(db2, {"Booking": B, "DirectSale": D, "Payment": P},
                                    cl["name"])
            expected = money(CYCLES * PER_CYCLE_BALANCE)
            ok = bal == expected
            rec.check(area, f"{cl['name']}: balance unchanged by restart", ok,
                      f"expected {expected}, got {bal}")
            if not ok:
                rec.bug(module="Client Ledger", page=f"/ledger/{row.id}", severity="Critical",
                        test_client=cl["name"], route="restart",
                        steps="Complete 5 cycles, restart the app, re-read the balance",
                        expected=str(expected), actual=str(bal),
                        financial_impact="balance changes across a restart",
                        consistency_risk="Yes", ledger_impact="non-deterministic ledger")

        # Stock must also be stable across the restart.
        total = material_total(db2, {"Material": Mp.Material}, MATERIAL)
        derived = stock_from_entries(db2, {"Entry": Mp.Entry}, MATERIAL)
        ok = total == derived
        rec.check(area, "material.total == sum(IN) - sum(OUT) after restart", ok,
                  f"material.total={total}, entries={derived}")
        if not ok:
            rec.bug(module="Inventory", page="/materials", severity="Critical",
                    route="restart", steps="Run all cycles, restart, compare stock to entries",
                    expected=f"{derived}", actual=f"{total}",
                    inventory_impact=f"drift {round((total or 0) - derived, 3)}",
                    consistency_risk="Yes",
                    root_cause="Cached material.total diverges from the movement ledger")

    # Pages must still render post-restart.
    for path in ("/", "/clients", "/materials", "/payments", "/direct_sales", "/bookings"):
        resp = br2.get(path)
        rec.check(area, f"{path} renders after restart", resp.status_code == 200,
                  f"HTTP {resp.status_code}")
    return app2, br2


# ---------------------------------------------------------------------------
# PHASE 10 - repeated / duplicate submission
# ---------------------------------------------------------------------------
def phase10_repeat(app, br, rec, models, db, ids, clients):
    area = "Phase10-Repeat"
    cl = clients[-1]
    P = models["Payment"]
    bill = "QA-DUP-PAYMENT-001"
    payload = {
        "client_code": cl["code"], "amount": "777", "method": "Cash",
        "payment_type": "Receipt", "payment_account_id": str(ids["account_id"]),
        "manual_bill_no": bill, "date": "2026-03-05",
    }
    br.post("/add_payment", data=dict(payload))
    br.post("/add_payment", data=dict(payload))  # the impatient double-click

    with app.app_context():
        rows = P.query.filter(P.manual_bill_no.like(f"%{bill}"), P.is_void == False).all()  # noqa: E712
    ok = len(rows) == 1
    rec.check(area, "re-submitting an identical payment does not double-post", ok,
              f"{len(rows)} rows for bill {bill}")
    if not ok:
        rec.bug(module="Payments", page="/add_payment", severity="Critical",
                test_client=cl["name"], transaction=bill, route="POST /add_payment",
                steps="Submit the exact same payment form twice in a row",
                expected="One payment (duplicate suppressed)",
                actual=f"{len(rows)} identical payments recorded",
                duplication_risk="Yes",
                financial_impact=f"cash overstated by {money(777 * (len(rows) - 1))}",
                ledger_impact="client credited twice", consistency_risk="Yes",
                root_cause="No idempotency key / duplicate-reference guard on repeat POST")

    # Clean up so later reconciliation maths stays closed-form.
    with app.app_context():
        for row in P.query.filter(P.manual_bill_no.like(f"%{bill}")).all():
            db.session.delete(row)
        db.session.commit()

    # Duplicate booking reference number.
    B = models["Booking"]
    bbill = "QA-DUP-BOOKING-001"
    bp = {
        "client_code": cl["code"], "material_name[]": MATERIAL,
        "material_id[]": str(ids["material_id"]), "qty[]": "1", "unit_rate[]": "100",
        "amount": "100", "paid_amount": "0", "manual_bill_no": bbill, "date": "2026-03-06",
    }
    br.post("/add_booking", data=dict(bp))
    br.post("/add_booking", data=dict(bp))
    with app.app_context():
        rows = B.query.filter(B.manual_bill_no.like(f"%{bbill}"), B.is_void == False).all()  # noqa: E712
    ok = len(rows) == 1
    rec.check(area, "duplicate booking bill number is rejected", ok,
              f"{len(rows)} bookings share bill {bbill}")
    if not ok:
        rec.bug(module="Sales/Bookings", page="/add_booking", severity="High",
                test_client=cl["name"], transaction=bbill, route="POST /add_booking",
                steps="Create two bookings with the same manual bill number",
                expected="Second rejected as a duplicate reference",
                actual=f"{len(rows)} bookings share one bill number",
                duplication_risk="Yes",
                root_cause="manual_bill_no has no uniqueness constraint",
                financial_impact="invoice lookup by bill number becomes ambiguous")
    with app.app_context():
        for row in B.query.filter(B.manual_bill_no.like(f"%{bbill}")).all():
            db.session.delete(row)
        db.session.commit()


# ---------------------------------------------------------------------------
# PHASE 11 - edit / void / reversal
# ---------------------------------------------------------------------------
def phase11_reversal(app, br, rec, models, db, ids, clients):
    area = "Phase11-Reversal"
    cl = clients[1]
    B, P = models["Booking"], models["Payment"]

    with app.app_context():
        n = cl["name"].lower()
        bk = (B.query.filter(func.lower(func.trim(B.client_name)) == n)
              .order_by(B.id.desc()).first())
        if bk is None:
            rec.blocked(area, "booking edit/void", "no booking available for this client")
            return
        bid = bk.id
        bal_before = recompute_balance(db, models, cl["name"])

    # --- EDIT ---------------------------------------------------------------
    new_qty, new_rate = 12.0, 1500.0
    new_amount = new_qty * new_rate
    r = br.post(f"/edit_bill/Booking/{bid}", data={
        "client_code": cl["code"], "material_name[]": MATERIAL,
        "material_id[]": str(ids["material_id"]),
        "qty[]": str(new_qty), "unit_rate[]": str(new_rate),
        "amount": str(new_amount), "paid_amount": str(BK_PAID),
        "booking_item_id[]": "",
    })
    rec.check(area, "booking edit accepted", r.status_code == 200, flashes(r))
    with app.app_context():
        bk = db.session.get(B, bid)
        rec.check(area, "edited booking amount persisted", money(bk.amount) == new_amount,
                  f"expected {new_amount}, stored {bk.amount}")
        bal_after_edit = recompute_balance(db, models, cl["name"])
    delta = money(bal_after_edit - bal_before)
    expect = money(new_amount - BK_AMOUNT)
    ok = delta == expect
    rec.check(area, "editing a booking moves the balance by exactly the amount delta", ok,
              f"expected {expect:+}, got {delta:+}")
    if not ok:
        rec.bug(module="Sales/Bookings", page=f"/edit_bill/Booking/{bid}", severity="Critical",
                test_client=cl["name"], transaction=f"booking {bid}",
                route=f"POST /edit_bill/Booking/{bid}",
                steps=f"Edit booking {bid} from {BK_AMOUNT} to {new_amount}",
                expected=f"balance {expect:+}", actual=f"balance {delta:+}",
                ledger_impact="edit not correctly reflected downstream",
                financial_impact="outstanding wrong after edit", consistency_risk="Yes")

    # --- DELETE (permanent by design - there is no soft void here) ---------
    with app.app_context():
        bal_pre_delete = recompute_balance(db, models, cl["name"])
        amt, paid = money(bk.amount), money(bk.paid_amount)
        BI = models.get("BookingItem")
        items_before = BI.query.filter_by(booking_id=bid).count() if BI else None

    # The misleading reversible-sounding routes must no longer exist.
    rules = {str(r) for r in app.url_map.iter_rules()}
    stale = sorted(r for r in rules
                   if r.startswith(("/void_transaction", "/unvoid_transaction")))
    rec.check(area, "no misleading void/unvoid transaction routes are registered",
              not stale, f"still present: {stale}")
    if stale:
        rec.bug(
            module="Sales/Bookings", page="/void_transaction", severity="High",
            test_client=cl["name"], route=", ".join(stale),
            steps="Inspect the URL map for void/unvoid transaction routes",
            expected="Only /delete_transaction exists, because deletion is permanent",
            actual=f"Reversible-sounding routes still registered: {stale}",
            data_loss_risk="Yes", consistency_risk="Yes",
            root_cause="Legacy void alias not removed",
        )

    r = br.post(f"/delete_transaction/Booking/{bid}", data={"reason": "QA reversal test"})
    rec.check(area, "booking delete accepted", r.status_code == 200, flashes(r))

    with app.app_context():
        gone = db.session.get(B, bid) is None
        items_after = BI.query.filter_by(booking_id=bid).count() if BI else None
        bal_post_delete = recompute_balance(db, models, cl["name"])

    rec.check(area, "deleting a booking removes the row permanently", gone,
              "the row is still present after delete")
    if BI is not None:
        ok = (items_after or 0) == 0
        rec.check(area, "deleting a booking leaves no orphan booking items", ok,
                  f"{items_after} booking_item rows still reference booking {bid} "
                  f"(was {items_before})")
        if not ok:
            rec.bug(
                module="Sales/Bookings", page="/delete_transaction", severity="High",
                test_client=cl["name"], transaction=f"booking {bid}",
                route=f"POST /delete_transaction/Booking/{bid}",
                steps=f"Delete booking {bid} and look for its booking_item children",
                expected="0 orphan rows", actual=f"{items_after} orphan rows",
                db_impact="orphaned child rows", consistency_risk="Yes",
            )

    delta = money(bal_post_delete - bal_pre_delete)
    expect = money(-(amt - paid))
    ok = delta == expect
    rec.check(area, "deleting a booking reverses exactly its outstanding contribution",
              ok, f"expected {expect:+}, got {delta:+}")
    if not ok:
        rec.bug(module="Sales/Bookings", page="/delete_transaction", severity="Critical",
                test_client=cl["name"], transaction=f"booking {bid}",
                route=f"POST /delete_transaction/Booking/{bid}",
                steps=f"Delete booking {bid} and re-read the client balance",
                expected=f"balance {expect:+}", actual=f"balance {delta:+}",
                ledger_impact="delete leaves a stale receivable",
                financial_impact="outstanding wrong after delete", consistency_risk="Yes")

    # The void audit screen still serves the entities that genuinely soft-void.
    va = br.get("/void_audit")
    rec.check(area, "void audit page renders", va.status_code == 200, f"HTTP {va.status_code}")

    # --- payment delete -----------------------------------------------------
    with app.app_context():
        pay = (P.query.filter(func.lower(func.trim(P.client_name)) == cl["name"].lower(),
                              P.is_void == False)  # noqa: E712
               .order_by(P.id.desc()).first())
        if pay is None:
            rec.blocked(area, "payment delete", "no payment available")
            return
        pid, pamt = pay.id, money(pay.amount)
        bal_pre = recompute_balance(db, models, cl["name"])
    r = br.post(f"/delete_transaction/Payment/{pid}", data={"reason": "QA reversal test"})
    rec.check(area, "payment delete accepted", r.status_code == 200, flashes(r))
    with app.app_context():
        bal_post = recompute_balance(db, models, cl["name"])
    delta = money(bal_post - bal_pre)
    ok = delta == pamt
    rec.check(area, "deleting a payment adds the amount back to outstanding", ok,
              f"expected +{pamt}, got {delta:+}")
    if not ok:
        rec.bug(module="Payments", page="/delete_transaction", severity="Critical",
                test_client=cl["name"], transaction=f"payment {pid}",
                route=f"POST /delete_transaction/Payment/{pid}",
                steps=f"Delete payment {pid} ({pamt}) and re-read outstanding",
                expected=f"+{pamt}", actual=f"{delta:+}",
                financial_impact="deleted cash still credited to the client",
                ledger_impact="ledger not reversed", consistency_risk="Yes")


# ---------------------------------------------------------------------------
# PHASE 12 - search / filter / date boundaries
# ---------------------------------------------------------------------------
def phase12_filters(app, br, rec, models, db, clients):
    area = "Phase12-Filters"
    cl = clients[0]

    # The list page must render for both a matching and a non-matching term.
    r = br.get(f"/clients?search={cl['name'].replace(' ', '+')}")
    rec.check(area, "client list renders for a matching search term",
              r.status_code == 200 and said(r, cl["name"]), f"HTTP {r.status_code}")
    r = br.get("/clients?search=ZZZ-NO-SUCH-CLIENT-XYZ")
    rec.check(area, "client list renders for a non-matching search term",
              r.status_code == 200, f"HTTP {r.status_code}")

    # Search *semantics* are asserted against the JSON API, because the HTML
    # page also embeds a hidden client-picker datalist that legitimately
    # contains every client regardless of the filter.
    r = br.get(f"/api/clients/search?q={cl['name'].replace(' ', '+')}")
    if r.status_code == 200 and r.is_json:
        names = [x.get("name") for x in (r.get_json() or [])]
        rec.check(area, "client search API returns the searched client",
                  cl["name"] in names, f"got {names[:5]}")
        r2 = br.get("/api/clients/search?q=ZZZ-NO-SUCH-CLIENT-XYZ")
        empty = r2.is_json and not (r2.get_json() or [])
        rec.check(area, "client search API returns nothing for a nonsense term", empty,
                  f"got {str(r2.get_json())[:200]}")
        if not empty:
            rec.bug(module="Clients", page="/api/clients/search", severity="Medium",
                    route="GET /api/clients/search", steps="Search a term that matches nothing",
                    expected="Empty result set", actual="Unrelated clients returned",
                    root_cause="Search filter not applied to the query",
                    consistency_risk="Yes")
        # Partial / case-insensitive matching.
        r3 = br.get("/api/clients/search?q=qa+test+client")
        n3 = len(r3.get_json() or []) if r3.is_json else 0
        rec.check(area, "client search is case-insensitive and matches partials",
                  n3 >= len(clients) - 1, f"matched {n3} of {len(clients)} QA clients")
    else:
        rec.blocked(area, "client search API", f"HTTP {r.status_code}")

    # Date-boundary probes: our data is all dated 2026-02-xx / 2026-03-xx.
    for path, label in (
        ("/daily_transactions?start_date=2026-02-01&end_date=2026-02-28", "February range"),
        ("/daily_transactions?start_date=2026-02-10&end_date=2026-02-10", "single-day range"),
        ("/daily_transactions?start_date=2030-01-01&end_date=2030-01-02", "far-future empty range"),
        ("/daily_transactions?start_date=2026-02-28&end_date=2026-02-01", "reversed range"),
        ("/daily_transactions?start_date=not-a-date&end_date=also-bad", "invalid date strings"),
    ):
        resp = br.get(path)
        ok = resp.status_code < 500
        rec.check(area, f"daily transactions handles a {label}", ok, f"HTTP {resp.status_code}")
        if not ok:
            rec.bug(module="Reports", page=path, severity="Medium", route=f"GET {path}",
                    steps=f"Apply a {label} to the daily transactions report",
                    expected="Page renders (empty result or validation notice)",
                    actual=f"HTTP {resp.status_code}", consistency_risk="Yes",
                    root_cause="Unguarded date parsing")

    # Pagination / sorting must not explode.
    for path in ("/clients?page=1", "/clients?page=99999", "/clients?sort=name",
                 "/materials?page=1", "/payments?page=1"):
        resp = br.get(path)
        rec.check(area, f"{path} handled", resp.status_code < 500, f"HTTP {resp.status_code}")


# ---------------------------------------------------------------------------
# PHASE 13/14 - dashboard, reports and database reconciliation
# ---------------------------------------------------------------------------
def phase13_reconcile(app, br, rec, models, db, clients):
    area = "Phase13-Reconciliation"

    with app.app_context():
        B, D, P, M, E = (models["Booking"], models["DirectSale"], models["Payment"],
                         models["Material"], models["Entry"])

        # --- Inventory invariant --------------------------------------------
        total = material_total(db, models, MATERIAL)
        derived = stock_from_entries(db, models, MATERIAL)
        ok = total == derived
        rec.check(area, "Inventory: material.total == sum(IN) - sum(OUT)", ok,
                  f"material.total={total}, entry-derived={derived}")
        if not ok:
            rec.bug(module="Inventory", page="/materials", severity="Critical",
                    route="GET /materials",
                    steps="Run all QA cycles, then compare material.total to the entry ledger",
                    expected=str(derived), actual=str(total),
                    inventory_impact=f"drift {round((total or 0) - derived, 3)}",
                    consistency_risk="Yes",
                    root_cause="Denormalised stock counter not kept in step with movements")

        # --- Per-client ledgers ---------------------------------------------
        grand = 0.0
        for cl in clients:
            bal = recompute_balance(db, models, cl["name"])
            grand += bal
        rec.bump("clients_reconciled", len(clients))

        # --- Orphan / integrity sweep ---------------------------------------
        orphan_payments = P.query.filter(
            P.is_void == False,  # noqa: E712
            ~P.client_name.in_([c["name"] for c in clients]),
            P.client_id.is_(None),
        ).count()
        rec.check(area, "no payments with neither client_id nor a known client name",
                  orphan_payments == 0, f"{orphan_payments} orphan payment rows")
        if orphan_payments:
            rec.bug(module="Payments", page="/payments", severity="High",
                    route="DB sweep", steps="Query payments with no client_id and no known client",
                    expected="0", actual=str(orphan_payments),
                    db_impact="orphan financial rows", consistency_risk="Yes")

        dup_bills = (db.session.query(B.manual_bill_no, func.count(B.id))
                     .filter(B.is_void == False, B.manual_bill_no.isnot(None))  # noqa: E712
                     .group_by(B.manual_bill_no).having(func.count(B.id) > 1).all())
        rec.check(area, "no duplicate booking bill numbers", not dup_bills,
                  f"duplicates: {dup_bills[:5]}")
        if dup_bills:
            rec.bug(module="Sales/Bookings", page="/bookings", severity="High",
                    route="DB sweep", steps="Group bookings by manual_bill_no having count > 1",
                    expected="none", actual=f"{len(dup_bills)} duplicated bill numbers",
                    duplication_risk="Yes", evidence=str(dup_bills[:5]),
                    root_cause="No unique index on booking.manual_bill_no")

        dup_pay = (db.session.query(P.manual_bill_no, func.count(P.id))
                   .filter(P.is_void == False, P.manual_bill_no.isnot(None))  # noqa: E712
                   .group_by(P.manual_bill_no).having(func.count(P.id) > 1).all())
        rec.check(area, "no duplicate payment reference numbers", not dup_pay,
                  f"duplicates: {dup_pay[:5]}")
        if dup_pay:
            rec.bug(module="Payments", page="/payments", severity="High",
                    route="DB sweep", steps="Group payments by manual_bill_no having count > 1",
                    expected="none", actual=f"{len(dup_pay)} duplicated references",
                    duplication_risk="Yes", evidence=str(dup_pay[:5]),
                    financial_impact="double-counted receipts")

        neg_stock = M.query.filter(M.total < 0).count()
        rec.check(area, "no material sits at negative stock", neg_stock == 0,
                  f"{neg_stock} materials below zero")

        exp_bookings = CLIENTS * CYCLES
        live_bookings = B.query.filter(
            B.is_void == False,  # noqa: E712
            B.client_name.in_([c["name"] for c in clients]),
        ).count()
        rec.check(area, f"exactly {exp_bookings} live QA bookings across all clients",
                  live_bookings == exp_bookings, f"found {live_bookings}")

        exp_sales = CLIENTS * CYCLES
        live_sales = D.query.filter(
            D.is_void == False,  # noqa: E712
            D.client_name.in_([c["name"] for c in clients]),
        ).count()
        rec.check(area, f"exactly {exp_sales} live QA direct sales", live_sales == exp_sales,
                  f"found {live_sales}")

    # --- Dashboard and report pages must render and stay consistent ---------
    for path in ("/", "/daily_transactions", "/financial_details", "/profit_reports",
                 "/stock_summary", "/current_payables", "/unpaid_transactions",
                 "/financial_tracker", "/system_report", "/accounts/", "/accounts/audit"):
        resp = br.get(path)
        ok = resp.status_code < 500
        rec.check(area, f"report/dashboard {path} renders", ok, f"HTTP {resp.status_code}")
        if not ok:
            rec.bug(module="Reports/Dashboard", page=path, severity="High",
                    route=f"GET {path}",
                    steps=f"After 25 QA transaction cycles, open {path}",
                    expected="Page renders", actual=f"HTTP {resp.status_code}",
                    evidence=resp.get_data(as_text=True)[:300], consistency_risk="Yes")

    # --- The financial integrity audit endpoint the app ships ----------------
    resp = br.get("/api/audit/financial-integrity")
    if resp.status_code == 200 and resp.is_json:
        data = resp.get_json() or {}
        issues = data.get("issues") or data.get("problems") or []
        rec.check(area, "built-in financial integrity audit reports no issues",
                  not issues, f"{len(issues)} issues: {str(issues)[:300]}")
        if issues:
            rec.bug(module="Accounts", page="/api/audit/financial-integrity", severity="High",
                    route="GET /api/audit/financial-integrity",
                    steps="Run the shipped financial integrity audit after the QA cycles",
                    expected="No issues", actual=f"{len(issues)} issues reported",
                    evidence=str(issues)[:500], consistency_risk="Yes")
    else:
        rec.blocked(area, "financial integrity audit API", f"HTTP {resp.status_code}")


# ---------------------------------------------------------------------------
# PHASE 15 - error / edge / abuse
# ---------------------------------------------------------------------------
def phase15_edges(app, br, rec, models, db, clients):
    area = "Phase15-EdgeCases"

    # Invalid routes and non-existent record ids.
    for path, expect in (
        ("/this-route-does-not-exist", 404),
        ("/ledger/999999", None),
        ("/client_ledger/999999", None),
        ("/view_bill_detail/Booking/999999", None),
        ("/material_ledger/999999", None),
    ):
        resp = br.get(path)
        ok = resp.status_code < 500
        rec.check(area, f"{path} degrades gracefully", ok, f"HTTP {resp.status_code}")
        if not ok:
            rec.bug(module="Navigation", page=path, severity="Medium", route=f"GET {path}",
                    steps=f"Open {path} (record does not exist)",
                    expected="404 or a friendly empty state", actual=f"HTTP {resp.status_code}",
                    evidence=resp.get_data(as_text=True)[:300])

    # Deleting a client that has live transactions must not silently orphan them.
    cl = clients[2]
    C, B = models["Client"], models["Booking"]
    r = br.post(f"/delete_client/{cl['id']}", data={})
    with app.app_context():
        row = db.session.get(C, cl["id"])
        still_there = row is not None and getattr(row, "is_active", True)
        live = B.query.filter(
            func.lower(func.trim(B.client_name)) == cl["name"].lower(),
            B.is_void == False,  # noqa: E712
        ).count()
        gone = row is None
    if gone and live:
        rec.check(area, "deleting a client with live transactions is blocked or cascades", False,
                  f"client row deleted while {live} live bookings remain")
        rec.bug(module="Clients", page=f"/delete_client/{cl['id']}", severity="Critical",
                test_client=cl["name"], route=f"POST /delete_client/{cl['id']}",
                steps=f"Delete {cl['name']} while it still has {live} bookings",
                expected="Deletion blocked, or transactions cascaded/reassigned",
                actual="Client hard-deleted; its transactions are now orphaned",
                db_impact=f"{live} orphan bookings", data_loss_risk="Yes",
                consistency_risk="Yes", ledger_impact="ledger unreachable from the client list")
    else:
        rec.check(area, "deleting a client with live transactions is blocked or cascades", True,
                  f"client preserved (active={still_there}), {live} bookings intact; "
                  f"flash: {flashes(r)}")

    # Session expiry: an anonymous browser must not reach protected pages.
    anon = app.test_client()
    resp = anon.get("/clients", follow_redirects=False)
    ok = resp.status_code in (301, 302, 303, 401, 403)
    rec.check(area, "protected page redirects an unauthenticated visitor", ok,
              f"HTTP {resp.status_code}")
    if not ok:
        rec.bug(module="Security", page="/clients", severity="Critical", route="GET /clients",
                steps="Request /clients with no session cookie",
                expected="Redirect to /login", actual=f"HTTP {resp.status_code} served",
                root_cause="Missing @login_required", consistency_risk="Yes",
                data_loss_risk="Yes")

    anon2 = app.test_client()
    resp = anon2.post("/add_client", data={"name": "ANON", "code": "ANON-1"},
                      follow_redirects=False)
    with app.app_context():
        leaked = C.query.filter_by(code="ANON-1").count()
    rec.check(area, "anonymous POST cannot create records", leaked == 0,
              f"{leaked} rows created anonymously")
    if leaked:
        rec.bug(module="Security", page="/add_client", severity="Critical",
                route="POST /add_client", steps="POST /add_client with no session",
                expected="Rejected", actual="Client created by an anonymous caller",
                root_cause="Endpoint not protected", consistency_risk="Yes")

    # Bad login must fail.
    bad = app.test_client()
    bad.get("/login")
    with bad.session_transaction() as s:
        tok = s.get("_csrf_token") or "t"
        s["_csrf_token"] = tok
    resp = bad.post("/login", data={"username": "Admin", "password": "wrong-password",
                                    "_csrf_token": tok}, follow_redirects=False)
    landed = resp.headers.get("Location", "")
    ok = resp.status_code != 302 or "login" in landed
    rec.check(area, "login with a wrong password is refused", ok,
              f"HTTP {resp.status_code} -> {landed}")
    if not ok:
        rec.bug(module="Security", page="/login", severity="Critical", route="POST /login",
                steps="Log in as Admin with an incorrect password",
                expected="Rejected", actual="Authenticated",
                root_cause="Password not verified", consistency_risk="Yes")
