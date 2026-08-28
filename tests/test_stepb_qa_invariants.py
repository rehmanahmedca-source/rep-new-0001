"""STEP B regression locks.

Condensed, fast versions of the invariants proved by ``tools/qa_stepb``.  They
guard the arithmetic that the deep audit verified, so a future change that
re-introduces stock drift, ledger drift or duplicate posting fails CI.

The two defects the audit reproduced are pinned with ``xfail(strict=True)``:
when they are fixed these tests go XPASS and must be flipped to plain asserts.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func

from models import db, Account, Booking, Client, DirectSale, Entry, Material, Payment, Supplier

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}

MATERIAL = "REG CEMENT"
SUPPLIER = "REG SUPPLIER"

GRN_QTY, BK_QTY, BK_RATE, BK_PAID = 100.0, 10.0, 1500.0, 5000.0
DISPATCH_QTY, PAY_AMOUNT, DS_QTY, DS_RATE = 10.0, 2000.0, 4.0, 1600.0
BK_AMOUNT, DS_AMOUNT = BK_QTY * BK_RATE, DS_QTY * DS_RATE
CYCLES = 5

PER_CYCLE_BALANCE = (BK_AMOUNT - BK_PAID) + DS_AMOUNT - PAY_AMOUNT   # 14,400
PER_CYCLE_STOCK = GRN_QTY - DISPATCH_QTY - DS_QTY                    # +86


def _login(client):
    client.get("/login")
    resp = client.post("/login", data=ADMIN, follow_redirects=False)
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def _balance(name):
    """Independent recomputation of a client's outstanding balance."""
    n = name.strip().lower()
    lo = lambda c: func.lower(func.trim(c))  # noqa: E731

    def s(col, model):
        return float(db.session.query(func.sum(col)).filter(
            lo(model.client_name) == n, model.is_void == False  # noqa: E712
        ).scalar() or 0)

    debit = s(Booking.amount, Booking) + s(DirectSale.amount, DirectSale)
    credit = s(Booking.paid_amount, Booking) + s(DirectSale.paid_amount, DirectSale)
    disc = s(Booking.discount, Booking) + s(DirectSale.discount, DirectSale)
    pay = s(Payment.amount, Payment)
    return round(debit - credit - disc - pay, 2)


def _stock():
    m = Material.query.filter_by(name=MATERIAL).first()
    return round(float(m.total or 0), 3)


def _stock_from_entries():
    def s(kind):
        return float(db.session.query(func.sum(Entry.qty)).filter(
            func.lower(func.trim(Entry.material)) == MATERIAL.lower(),
            Entry.type == kind, Entry.is_void == False,  # noqa: E712
        ).scalar() or 0)
    return round(s("IN") - s("OUT"), 3)


@pytest.fixture()
def erp(app, client):
    """A logged-in ERP with one client, one material, one supplier, one account."""
    _login(client)
    client.post("/add_material", data={"material_name": MATERIAL, "material_unit": "Bags"})
    client.post("/add_supplier", data={"name": SUPPLIER, "phone": "0300"})
    client.post("/accounts/accounts/add", data={
        "name": "REG CASH", "class_category": "Assets", "class_subcategory": "Cash",
        "class_account_type": "Main Cash", "account_status": "active",
        "opening_amount": "0", "opening_position": "debit",
        "opening_effective_date": "2026-01-01",
    })
    client.post("/add_client", data={
        "name": "REG CLIENT", "code": "REG-01", "category": "General",
        "opening_balance": "0",
    })
    with app.app_context():
        ctx = {
            "mat_id": Material.query.filter_by(name=MATERIAL).first().id,
            "sup_id": Supplier.query.filter(Supplier.name.like(f"%{SUPPLIER}%")).first().id,
            "acc_id": Account.query.filter(Account.name.like("%REG CASH%")).first().id,
            "cli_id": Client.query.filter_by(code="REG-01").first().id,
        }
    return ctx


def _cycle(client, ctx, i):
    client.post("/grn", data={
        "action": "add", "supplier": SUPPLIER, "supplier_id": str(ctx["sup_id"]),
        "mat_name[]": MATERIAL, "qty[]": str(GRN_QTY), "price[]": "1000",
        "paid_amount": "0", "manual_bill_no": f"REG-GRN-{i}",
    })
    client.post("/add_booking", data={
        "client_code": "REG-01", "material_name[]": MATERIAL,
        "material_id[]": str(ctx["mat_id"]), "qty[]": str(BK_QTY),
        "unit_rate[]": str(BK_RATE), "amount": str(BK_AMOUNT),
        "paid_amount": str(BK_PAID), "manual_bill_no": f"REG-BK-{i}",
        "date": "2026-02-10", "payment_account_id": str(ctx["acc_id"]),
        "payment_method": "Cash",
    })
    client.post("/add_record", data={
        "date": "2026-02-12", "client": "REG CLIENT", "type": "OUT",
        "material": MATERIAL, "material_id": str(ctx["mat_id"]),
        "qty": str(DISPATCH_QTY), "driver_name": "REG DRIVER",
        "bill_no": f"REG-BK-{i}",
    })
    client.post("/add_payment", data={
        "client_code": "REG-01", "amount": str(PAY_AMOUNT), "method": "Cash",
        "payment_type": "Receipt", "payment_account_id": str(ctx["acc_id"]),
        "manual_bill_no": f"REG-PAY-{i}", "date": "2026-02-15",
    })
    client.post("/add_direct_sale", data={
        "client_name": "REG CLIENT", "client_code": "REG-01",
        "driver_name": "REG DRIVER", "category": "Credit Customer",
        "product_name[]": MATERIAL, "qty[]": str(DS_QTY),
        "unit_rate[]": str(DS_RATE), "paid_amount": "0",
        "manual_bill_no": f"REG-DS-{i}", "ignore_booking_item[]": "1",
    })


# ---------------------------------------------------------------------------
# The five-times rule: cumulative figures must be exactly 5x the per-cycle ones
# ---------------------------------------------------------------------------
def test_five_cycles_produce_exact_cumulative_totals(app, client, erp):
    for i in range(1, CYCLES + 1):
        with app.app_context():
            bal_before, stock_before = _balance("REG CLIENT"), _stock()
        _cycle(client, erp, i)
        with app.app_context():
            assert round(_balance("REG CLIENT") - bal_before, 2) == PER_CYCLE_BALANCE, (
                f"cycle {i}: client balance moved by the wrong amount")
            assert round(_stock() - stock_before, 3) == PER_CYCLE_STOCK, (
                f"cycle {i}: stock moved by the wrong amount")

    with app.app_context():
        lo = lambda c: func.lower(func.trim(c))  # noqa: E731
        n = "reg client"
        bks = Booking.query.filter(lo(Booking.client_name) == n,
                                   Booking.is_void == False).all()  # noqa: E712
        sls = DirectSale.query.filter(lo(DirectSale.client_name) == n,
                                      DirectSale.is_void == False).all()  # noqa: E712
        pys = Payment.query.filter(lo(Payment.client_name) == n,
                                   Payment.is_void == False).all()  # noqa: E712

        assert len(bks) == CYCLES, "duplicate or missing bookings after 5 cycles"
        assert len(sls) == CYCLES, "duplicate or missing sales after 5 cycles"
        assert len(pys) == CYCLES, "duplicate or missing payments after 5 cycles"

        assert round(sum(float(b.amount) for b in bks), 2) == CYCLES * BK_AMOUNT
        assert round(sum(float(s.amount) for s in sls), 2) == CYCLES * DS_AMOUNT
        assert round(sum(float(p.amount) for p in pys), 2) == CYCLES * PAY_AMOUNT
        assert _balance("REG CLIENT") == CYCLES * PER_CYCLE_BALANCE

        # Invariant 1 from the Skills Book: stock cannot silently drift.
        assert _stock() == _stock_from_entries()


# ---------------------------------------------------------------------------
# Data survives a genuine application restart
# ---------------------------------------------------------------------------
def test_totals_survive_an_application_restart(app_factory, tmp_path):
    from tests.conftest import make_csrf_client

    db_file = tmp_path / "restart.db"
    app1 = app_factory(db_file)
    c1 = make_csrf_client(app1)
    _login(c1)
    c1.post("/add_material", data={"material_name": MATERIAL, "material_unit": "Bags"})
    c1.post("/add_supplier", data={"name": SUPPLIER, "phone": "0300"})
    c1.post("/accounts/accounts/add", data={
        "name": "REG CASH", "class_category": "Assets", "class_subcategory": "Cash",
        "class_account_type": "Main Cash", "account_status": "active",
        "opening_amount": "0", "opening_position": "debit",
        "opening_effective_date": "2026-01-01"})
    c1.post("/add_client", data={"name": "REG CLIENT", "code": "REG-01",
                                 "category": "General", "opening_balance": "0"})
    with app1.app_context():
        ctx = {
            "mat_id": Material.query.filter_by(name=MATERIAL).first().id,
            "sup_id": Supplier.query.filter(Supplier.name.like(f"%{SUPPLIER}%")).first().id,
            "acc_id": Account.query.filter(Account.name.like("%REG CASH%")).first().id,
        }
    for i in range(1, CYCLES + 1):
        _cycle(c1, ctx, i)
    with app1.app_context():
        before, stock_before = _balance("REG CLIENT"), _stock()

    # Cold restart against the very same file.
    app2 = app_factory(db_file)
    with app2.app_context():
        assert _balance("REG CLIENT") == before, "balance changed across a restart"
        assert _stock() == stock_before, "stock changed across a restart"
        assert Booking.query.filter(Booking.is_void == False).count() == CYCLES  # noqa: E712


# ---------------------------------------------------------------------------
# Repeated identical submission must not double-post
# ---------------------------------------------------------------------------
def test_resubmitting_the_same_payment_does_not_double_post(app, client, erp):
    payload = {
        "client_code": "REG-01", "amount": "777", "method": "Cash",
        "payment_type": "Receipt", "payment_account_id": str(erp["acc_id"]),
        "manual_bill_no": "REG-DUP-1", "date": "2026-03-05",
    }
    client.post("/add_payment", data=dict(payload))
    client.post("/add_payment", data=dict(payload))
    with app.app_context():
        rows = Payment.query.filter(Payment.manual_bill_no.like("%REG-DUP-1"),
                                    Payment.is_void == False).all()  # noqa: E712
        assert len(rows) == 1, f"double-posted: {len(rows)} identical payments stored"


# ---------------------------------------------------------------------------
# Confirmed defects - pinned so a fix is noticed
# ---------------------------------------------------------------------------
@pytest.mark.xfail(
    strict=True,
    reason="BUG-001: payments_crud.py:334 abs()-normalises the amount, so a "
           "negative Receipt is silently stored as a positive one instead of "
           "being rejected. Remove this xfail when the validation is added.",
)
def test_negative_payment_amount_is_rejected(app, client, erp):
    with app.app_context():
        before = Payment.query.count()
    client.post("/add_payment", data={
        "client_code": "REG-01", "amount": "-500", "method": "Cash",
        "payment_type": "Receipt", "payment_account_id": str(erp["acc_id"]),
        "manual_bill_no": "REG-NEG-1",
    })
    with app.app_context():
        assert Payment.query.count() == before, (
            "a negative Receipt was accepted")


@pytest.mark.xfail(
    strict=True,
    reason="BUG-002: /void_transaction is aliased to hard_delete_transaction, so "
           "the row is destroyed and /unvoid_transaction cannot restore it. "
           "Remove this xfail when voiding becomes a soft void.",
)
def test_voiding_a_booking_is_reversible(app, client, erp):
    _cycle(client, erp, 1)
    with app.app_context():
        bk = Booking.query.first()
        bid, before = bk.id, _balance("REG CLIENT")

    client.post(f"/void_transaction/Booking/{bid}", data={"reason": "regression"})
    with app.app_context():
        row = db.session.get(Booking, bid)
        assert row is not None and row.is_void is True, (
            "void destroyed the row instead of flagging it")

    client.post(f"/unvoid_transaction/Booking/{bid}", data={})
    with app.app_context():
        assert _balance("REG CLIENT") == before, "unvoid did not restore the balance"
