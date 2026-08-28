"""Legacy workbook discovery & adaptation layer for the migration pipeline.

This module never writes to the database.  It inspects an arbitrary legacy
XLSX workbook (sheets, headers, cells, merged ranges, hidden state), profiles
columns, detects which business entity each sheet most likely represents, and
suggests a column mapping onto the official migration templates defined in
``app.services.legacy_migration.TEMPLATES``.

Design rules encoded here:

* Sheet discovery is automatic — the importer must never claim "0 rows" for a
  workbook that contains data.  Every sheet is accounted for with a real row
  count and an explicit reason when it is not mapped.
* Detection is name + header + data driven, with confidence levels
  (HIGH / MEDIUM / LOW / UNKNOWN).  HIGH/MEDIUM are adapted for review;
  LOW/UNKNOWN are reported but never imported.
* The adapter only produces *template-shaped rows*.  Actual persistence still
  flows through the controlled model adapters in legacy_migration.import_run
  (masters) or stays locked behind domain services (transactions).  No raw
  INSERTs, no stock/ledger bypassing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

def norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _tokens(header: str) -> set[str]:
    """Split a header like 'Legacy GRN Reference*' into fuzzy tokens."""
    return {t for t in re.split(r"[^a-z0-9]+", norm(header).replace("_", " ")) if t}


#: Sheet-name tokens per entity. A sheet whose (singularised) name contains
#: one of these tokens gets a strong prior toward that entity.
SHEET_ENTITY_TOKENS: dict[str, set[str]] = {
    "CLIENTS": {"client", "clients", "customer", "customers", "party", "parties", "khata"},
    "SUPPLIERS": {"supplier", "suppliers", "vendor", "vendors"},
    "MATERIALS": {"material", "materials", "product", "products", "item", "items", "stock"},
    "ACCOUNTS": {"account", "accounts", "bank", "banks"},
    "BOOKINGS": {"booking", "bookings", "order", "orders"},
    "SALES": {"sale", "sales", "invoice", "invoices", "bill", "bills"},
    "DIRECT_SALES": {"direct sale", "directsales", "direct sales"},
    "GRN": {"grn", "goods receipt", "purchase", "purchases", "receipt note"},
    "PAYMENTS": {"payment", "payments", "receipt", "receipts"},
    "EXPENSES": {"expense", "expenses", "spend", "spends", "outflow"},
    "DELIVERIES": {"delivery", "deliveries", "dispatch", "dispatches"},
    "OPENING_BALANCES": {"opening balance", "opening balances", "opening"},
}

#: Sheets that belong to the operational schema itself (audit, meta, join
#: tables). They are reported with a reason, never auto-mapped.
INTERNAL_SHEET_TOKENS = {
    "audit", "log", "settings", "user", "users", "schema", "version", "lock", "meta",
    "backup", "recovery", "counter", "session", "draft", "basket", "recon", "repair",
    "archive", "history", "import", "reminder", "contact", "email", "category", "categories",
    "drawer", "rental", "pending", "waive", "allocation", "persons", "person",
}
#: *_item sheets are legitimate child sheets of transaction templates.
ITEM_SHEET_SUFFIXES = ("_item", "_items", "items")

#: Auxiliary/lookup tables — penalised so a category/counter table can never
#: masquerade as the master entity itself.
AUXILIARY_TOKENS = {"category", "categories", "type", "counter", "group", "subcat", "subcategory", "kind",
                      "person", "persons", "driver", "loader", "staff", "employee"}

#: Movement markers: a sheet carrying several of these is a transaction
#: register, not master data (master entities get a penalty).
TRANSACTION_MARKER_TOKENS = {"qty", "quantity", "bags", "rate", "amount", "bill", "invoice", "voucher", "entry", "movement", "journal", "paid"}

#: Business-header signatures: (min matched columns, required-any set, prefer-any set)
#: A sheet qualifies for an entity when headers carry the entity's key columns.
HEADER_SIGNATURES: dict[str, dict] = {
    "CLIENTS": {"name_any": {"client", "customer", "party", "name"},
                "extra_any": {"phone", "mobile", "address", "balance", "due", "opening", "code", "contact"},
                "row_hint": {"text": 0.7}},
    "SUPPLIERS": {"name_any": {"supplier", "vendor", "name", "party"},
                  "extra_any": {"phone", "address", "balance", "opening", "material"},
                  "row_hint": {"text": 0.7}},
    "MATERIALS": {"name_any": {"material", "product", "item", "goods", "name"},
                  "extra_any": {"unit", "price", "rate", "category", "stock", "code"},
                  "row_hint": {"text": 0.6}},
    "ACCOUNTS": {"name_any": {"account", "bank", "name"},
                 "extra_any": {"balance", "type", "category", "number", "holder", "branch"},
                 "row_hint": {"text": 0.5}},
    "BOOKINGS": {"name_any": {"booking", "client", "customer", "party"},
                 "extra_any": {"qty", "quantity", "bags", "rate", "price", "amount", "total", "date", "paid", "discount"},
                 "row_hint": {"num": 0.2}},
    "SALES": {"name_any": {"sale", "bill", "invoice", "client", "customer", "party"},
              "extra_any": {"qty", "quantity", "bags", "rate", "price", "amount", "total", "date", "paid", "discount", "product", "material"},
              "row_hint": {"num": 0.2}},
    "DIRECT_SALES": {"name_any": {"direct", "sale", "client", "party", "walkin", "name"},
                     "extra_any": {"amount", "paid", "date", "account", "method", "payment", "discount", "bill"},
                     "row_hint": {"num": 0.2}},
    "GRN": {"name_any": {"grn", "supplier", "vendor", "receipt", "purchase"},
            "extra_any": {"qty", "quantity", "rate", "price", "amount", "total", "date", "material"},
            "row_hint": {"num": 0.2}},
    "PAYMENTS": {"name_any": {"payment", "receipt", "amount", "party", "client", "supplier"},
                 "extra_any": {"date", "method", "account", "reference", "paid", "type"},
                 "row_hint": {"num": 0.2}},
    "EXPENSES": {"name_any": {"expense", "amount", "account"},
                 "extra_any": {"date", "category", "reference", "note", "paid"},
                 "row_hint": {"num": 0.2}},
    "DELIVERIES": {"name_any": {"delivery", "dispatch", "driver", "loader"},
                   "extra_any": {"date", "qty", "quantity", "bags", "bill", "client", "rent", "vehicle"},
                   "row_hint": {"text": 0.5}},
    "OPENING_BALANCES": {"name_any": {"opening", "balance"},
                         "extra_any": {"party", "account", "material", "amount", "quantity", "date", "type"},
                         "row_hint": {"num": 0.2}},
}

#: Legacy column -> template column aliases. Each entry is a PRIORITY-ORDERED
#: list of normalized source header phrases (underscores are treated as
#: spaces); the first source column that equals an alias wins. Extend this
#: configuration for future legacy files — nothing is hardcoded to one workbook.
COLUMN_ALIASES: dict[str, dict[str, list[str]]] = {
    "CLIENTS": {
        "Client Name": ["client name", "customer name", "party name", "name", "client", "customer", "party", "firm", "company name", "ledger name"],
        "Legacy Reference": ["legacy reference", "client code", "client no", "code", "id", "no", "sr no", "sr", "s no", "reference", "ledger no", "account no"],
        "Phone": ["mobile no", "phone number", "contact no", "cell no", "phone", "mobile", "contact", "cell", "whatsapp", "m no"],
        "Address": ["detail address", "address", "add", "location", "village", "area", "tehsil"],
        "Category": ["client category", "customer category", "category", "class", "type"],
        "Notes": ["note", "notes", "remarks", "description"],
        "Legacy Expected Due": ["opening balance", "closing balance", "current balance", "due amount", "net balance", "balance", "due", "outstanding", "receivable"],
    },
    "SUPPLIERS": {
        "Supplier Name": ["supplier name", "vendor name", "party name", "name", "supplier", "vendor", "firm", "company name"],
        "Legacy Reference": ["legacy reference", "supplier code", "code", "id", "no", "sr no", "sr", "reference"],
        "Phone": ["mobile no", "phone number", "phone", "mobile", "contact", "cell"],
        "Address": ["detail address", "address", "location", "add"],
        "Notes": ["note", "notes", "remarks", "description"],
        "Legacy Expected Due": ["opening balance", "closing balance", "balance", "due", "payable", "outstanding"],
    },
    "MATERIALS": {
        "Material Name": ["material name", "product name", "item name", "name", "material", "product", "item", "goods", "size", "description"],
        "Legacy Reference": ["legacy reference", "material code", "item code", "code", "id", "no", "sr no", "sr", "reference"],
        "Category": ["category name", "category", "category id", "group", "class", "type"],
        "Unit": ["unit", "uom", "measure", "packing"],
        "Unit Price": ["unit price", "current rate", "selling price", "std rate", "rate", "price"],
        "Notes": ["note", "notes", "remarks"],
        "Legacy Expected Stock": ["opening stock", "closing stock", "expected stock", "stock", "quantity", "qty", "total", "balance"],
    },
    "ACCOUNTS": {
        "Account Name": ["account name", "name", "account", "bank name", "title"],
        "Legacy Reference": ["legacy reference", "account code", "code", "id", "no", "sr no", "sr", "reference"],
        "Category": ["source category", "category", "group"],
        "Account Type": ["account type", "type", "kind"],
        "Opening Balance": ["opening balance", "balance"],
        "Bank Name": ["bank name", "bank"],
        "Account Number": ["account number", "account no", "acct no", "number"],
        "Notes": ["note", "notes", "remark", "description"],
        "Legacy Expected Balance": ["closing balance", "expected balance", "net balance"],
    },
    "BOOKINGS": {
        "Legacy Booking Reference": ["legacy booking reference", "booking id", "id", "booking no", "auto bill no", "manual bill no", "bill no", "invoice no", "reference"],
        "Booking Number": ["auto bill no", "booking no", "bill no", "invoice no", "nimbus no", "manual bill no", "voucher no"],
        "Date": ["date", "date posted", "booking date", "bill date", "entry date", "voucher date", "due date"],
        "Client": ["client name", "customer name", "party name", "client", "customer", "party"],
        "Notes": ["note", "notes", "remarks"],
    },
    "SALES": {
        "Legacy Sale Reference": ["legacy sale reference", "sale id", "id", "auto bill no", "bill no", "invoice no", "reference"],
        "Sale/Bill Number": ["auto bill no", "manual bill no", "bill no", "invoice no", "nimbus no", "voucher no"],
        "Date": ["date", "date posted", "sale date", "bill date", "invoice date", "entry date", "voucher date"],
        "Client": ["client name", "customer name", "party name", "client", "customer", "party", "buyer"],
        "Legacy Booking Reference": ["booking reference", "booking no", "booking id", "order no"],
        "Sale Type": ["sale type", "category", "type", "kind", "delivery type"],
        "Account": ["payment account", "received in account", "account name", "account"],
        "Notes": ["note", "notes", "remarks"],
    },
    "DIRECT_SALES": {
        "Legacy Sale Reference": ["legacy sale reference", "sale id", "id", "auto bill no", "bill no", "invoice no", "reference"],
        "Bill Number": ["manual bill no", "auto bill no", "bill no", "invoice no", "nimbus no"],
        "Date": ["date", "date posted", "sale date", "bill date"],
        "Client / Walk-in Name": ["client name", "customer name", "walkin name", "walk in name", "party name", "client", "customer", "buyer", "name"],
        "Account": ["payment account", "account name", "account"],
        "Payment Type": ["payment method", "payment type", "method", "mode"],
        "Notes": ["note", "notes", "remarks"],
    },
    "GRN": {
        "Legacy GRN Reference": ["legacy grn reference", "grn id", "id", "grn no", "auto bill no", "bill no", "invoice no", "reference"],
        "GRN Number": ["auto bill no", "grn no", "manual bill no", "bill no", "invoice no", "supplier invoice no"],
        "Date": ["date", "bill date", "date posted", "grn date", "entry date", "due date"],
        "Supplier": ["supplier name", "vendor name", "supplier", "vendor", "party", "firm"],
        "Account": ["payment account", "account name", "account"],
        "Notes": ["note", "notes", "remarks"],
    },
    "PAYMENTS": {
        "Legacy Payment Reference": ["legacy payment reference", "payment id", "id", "receipt no", "auto bill no", "manual bill no", "no", "sr no", "reference"],
        "Date": ["date", "date posted", "payment date", "voucher date", "receipt date"],
        "Party Type": ["party type", "payment type", "receipt payment", "customer supplier", "type"],
        "Party": ["client name", "customer name", "supplier name", "party", "client", "customer", "supplier", "vendor", "name"],
        "Amount": ["amount", "paid amount", "value", "rs", "rupees", "total"],
        "Account": ["payment account", "account name", "account", "bank name", "bank"],
        "Payment Type": ["payment type", "method", "mode", "direction", "type"],
        "Reference Number": ["reference number", "slip no", "transaction id", "txn id", "reference"],
        "Notes": ["note", "notes", "remark", "remarks"],
    },
    "EXPENSES": {
        "Legacy Expense Reference": ["legacy expense reference", "expense id", "id", "voucher no", "no", "sr no", "reference"],
        "Date": ["date", "date posted", "expense date", "voucher date"],
        "Account": ["account name", "account", "bank name", "bank"],
        "Amount": ["amount", "value", "rs", "total", "paid"],
        "Category": ["category", "type", "head"],
        "Reference Number": ["reference number", "reference", "slip no"],
        "Notes": ["note", "notes", "remarks", "description"],
    },
    "DELIVERIES": {
        "Legacy Delivery Reference": ["legacy delivery reference", "delivery id", "id", "no", "sr no", "reference"],
        "Date": ["date", "date posted", "delivery date"],
        "Client": ["client name", "party name", "client", "customer", "party"],
        "Bill Number": ["bill no", "manual bill no", "auto bill no", "invoice no"],
        "Material": ["material name", "product name", "material", "product", "item", "goods"],
        "Quantity": ["quantity", "qty", "bags", "units"],
        "Notes": ["note", "notes", "remark"],
    },
    "OPENING_BALANCES": {
        "Legacy Reference": ["reference", "id", "no", "sr no", "sr"],
        "Balance Type": ["balance type", "party type", "account type", "type", "kind"],
        "Party / Account / Material": ["party name", "client name", "supplier name", "account name", "name", "party", "client", "supplier", "material", "account"],
        "Amount or Quantity": ["opening balance", "amount", "quantity", "qty", "balance", "value", "due"],
        "Date": ["date", "as on date", "opening date", "date posted"],
        "Notes": ["note", "notes"],
        "Legacy Expected Balance": ["closing balance", "expected balance", "balance"],
    },
}

#: Item (line) sheets keep their own priority-ordered vocabulary.
ITEM_HEADER_ALIASES: dict[str, list[str]] = {
    "Legacy Sale Reference": ["sale id", "sale no", "legacy sale reference", "parent id", "reference", "id"],
    "Legacy GRN Reference": ["grn id", "grn no", "legacy grn reference", "parent id", "reference", "id"],
    "Legacy Booking Reference": ["booking id", "booking no", "legacy booking reference", "parent id", "reference", "id"],
    "Legacy Delivery Reference": ["delivery id", "legacy delivery reference", "parent id", "reference", "id"],
    "Material": ["material name", "product name", "mat name", "material", "product", "item", "goods", "description"],
    "Quantity": ["quantity", "qty", "bags", "units", "nos"],
    "Rate": ["price at time", "rate at time", "unit price", "cost rate", "amount per unit", "rate", "price"],
    "Discount": ["discount", "disc"],
    "Notes": ["note", "notes", "remarks"],
}

#: Preferred header names for numeric total profiling (reconciliation hints).
AMOUNT_HEADER_CANDIDATES = {"amount", "total", "total amount", "net amount", "value"}
QTY_HEADER_CANDIDATES = {"qty", "quantity", "bags", "units"}

#: The dependency-aware order masters→transactions. validate/import follow it.
IMPORT_ORDER: list[tuple[str, list[str]]] = [
    ("CLIENTS", []), ("SUPPLIERS", []), ("MATERIALS", []), ("CATEGORIES", []), ("ACCOUNTS", []),
    ("OPENING_BALANCES", ["CLIENTS", "SUPPLIERS", "MATERIALS", "ACCOUNTS"]),
    ("GRN", ["SUPPLIERS", "MATERIALS", "ACCOUNTS"]),
    ("BOOKINGS", ["CLIENTS", "MATERIALS"]),
    ("SALES", ["CLIENTS", "MATERIALS", "BOOKINGS"]),
    ("DIRECT_SALES", ["MATERIALS", "ACCOUNTS"]),
    ("DELIVERIES", ["MATERIALS"]),
    ("PAYMENTS", ["CLIENTS", "SUPPLIERS", "ACCOUNTS"]),
    ("EXPENSES", ["ACCOUNTS"]),
]

ENTITY_TO_MODEL = {
    "CLIENTS": "Client", "SUPPLIERS": "Supplier", "MATERIALS": "Material", "ACCOUNTS": "Account",
    "BOOKINGS": "Booking", "SALES": "DirectSale", "DIRECT_SALES": "DirectSale", "GRN": "GRN",
    "PAYMENTS": "Payment", "EXPENSES": "AccountTransaction", "DELIVERIES": "DeliveryRent",
    "OPENING_BALANCES": "Account",
}


# --------------------------------------------------------------------------
# Profiling primitives
# --------------------------------------------------------------------------

@dataclass
class ColumnProfile:
    header: str
    index: int
    non_empty: int = 0
    numeric: int = 0
    date_like: int = 0
    distinct: int = 0
    samples: list = field(default_factory=list)
    _seen: set = field(default_factory=set, repr=False)

    def as_dict(self) -> dict:
        return {"header": self.header, "column": self.index + 1, "filled": self.non_empty,
                "numeric": self.numeric, "dates": self.date_like,
                "distinct": self.distinct if self.distinct < 1000 else 1000,
                "samples": [str(s)[:40] for s in self.samples[:3]]}


@dataclass
class SheetProfile:
    name: str
    index: int
    rows_found: int = 0            # non-empty data rows below the header row
    columns_found: int = 0
    header_row: int = 1            # 1-based row number the header was taken from
    headers: list = field(default_factory=list)
    merged_cells: int = 0
    hidden: bool = False
    empty: bool = False
    duplicate_name_of: str = ""
    sample_rows: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    entity: str = "UNKNOWN"
    confidence: str = "UNKNOWN"
    score: float = 0.0
    evidence: list = field(default_factory=list)
    mapping: dict = field(default_factory=dict)      # template header -> source header
    target_sheet: str = ""                            # official template sheet it was adapted to
    status: str = "NOT_MAPPED"                        # MAPPED | IGNORED | NOT_MAPPED
    reason: str = ""
    amount_total: float | None = None
    quantity_total: float | None = None

    def as_dict(self) -> dict:
        return {"sheet": self.name, "rows_found": self.rows_found, "columns_found": self.columns_found,
                "header_row": self.header_row, "headers": self.headers, "merged_cells": self.merged_cells,
                "hidden": self.hidden, "empty": self.empty, "entity": self.entity, "confidence": self.confidence, "score": round(self.score, 2),
                "evidence": self.evidence, "mapping": self.mapping, "target_sheet": self.target_sheet,
                "status": self.status, "reason": self.reason,
                "samples": self.sample_rows, "columns": [c.as_dict() for c in self.columns],
                "amount_total": self.amount_total, "quantity_total": self.quantity_total}


def _is_empty_cell(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _looks_numeric(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    s = str(value or "").strip().replace(",", "")
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def _looks_date(value) -> bool:
    if isinstance(value, (datetime, date)):
        return True
    s = str(value or "").strip()
    if not s:
        return False
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        pass
    return bool(re.match(r"^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}", s))


def _header_score(row_values: list) -> float:
    """Score how header-like a row is: text-dense, distinct, non-numeric."""
    non_empty = [v for v in row_values if not _is_empty_cell(v)]
    if not non_empty:
        return 0.0
    text = sum(1 for v in non_empty if isinstance(v, str) and not _looks_numeric(v) and not _looks_date(v))
    distinct = len({norm(v) for v in non_empty})
    return (text / len(non_empty)) * 2 + (distinct / max(1, len(non_empty)))


def detect_header_row(ws, max_scan: int = 10) -> int:
    """Return 1-based row index most likely holding the headers."""
    best_row, best = 1, -1.0
    for idx, row in enumerate(ws.iter_rows(min_row=1, max_row=min(ws.max_row or 1, max_scan), values_only=True), 1):
        s = _header_score(list(row))
        if s > best:
            best_row, best = idx, s
    return best_row


def profile_workbook(wb) -> list[SheetProfile]:
    """Inspect every sheet: shape, header, column types, samples. Pure read."""
    profiles: list[SheetProfile] = []
    seen_names: dict[str, str] = {}
    for index, ws in enumerate(wb.worksheets):
        p = SheetProfile(name=ws.title, index=index)
        key = norm(ws.title)
        if key in seen_names:
            p.duplicate_name_of = seen_names[key]
        seen_names[key] = ws.title
        p.hidden = (ws.sheet_state or "visible") != "visible"
        try:
            p.merged_cells = len(ws.merged_cells.ranges)
        except Exception:
            p.merged_cells = 0
        rows = list(ws.iter_rows(values_only=True))
        rows = [list(r) for r in rows]
        non_empty_rows = [r for r in rows if any(not _is_empty_cell(v) for v in r)]
        if not non_empty_rows:
            p.empty, p.rows_found, p.status, p.reason = True, 0, "IGNORED", "Sheet contains no data rows."
            profiles.append(p)
            continue
        p.header_row = detect_header_row(ws)
        if p.header_row > len(rows):
            p.header_row = 1
        raw_header = rows[p.header_row - 1] if rows else []
        headers: list[str] = []
        for c in raw_header:
            headers.append(str(c).strip() if not _is_empty_cell(c) else "")
        # fill unnamed columns positionally
        width = max((len(r) for r in non_empty_rows), default=0)
        while len(headers) < width:
            headers.append("")
        for i, h in enumerate(headers):
            if not h:
                headers[i] = f"Column {i + 1}"
        p.headers = headers
        p.columns_found = sum(1 for i, r in enumerate(non_empty_rows) for v in r if not _is_empty_cell(v)) and width
        data_rows = [r for r in rows[p.header_row:] if any(not _is_empty_cell(v) for v in r)]
        # Drop a trailing title/caption row that sits between header and data is
        # not expected; a row entirely of Example/blank is still counted, but the
        # adapter skips placeholder rows the same way the exact-template path does.
        p.rows_found = len(data_rows)
        if p.rows_found == 0:
            p.empty, p.status, p.reason = True, "IGNORED", "Sheet has a header row but no data rows."
            profiles.append(p)
            continue
        cols = [ColumnProfile(header=headers[i] if i < len(headers) else f"Column {i + 1}", index=i) for i in range(width)]
        for r_i, r in enumerate(data_rows):
            if r_i < 25:
                if len(p.sample_rows) < 3:
                    p.sample_rows.append({cols[j].header: (str(r[j])[:60] if j < len(r) and not _is_empty_cell(r[j]) else "") for j in range(min(width, len(cols)))})
            for j in range(width):
                v = r[j] if j < len(r) else None
                if _is_empty_cell(v):
                    continue
                cp = cols[j]
                cp.non_empty += 1
                if _looks_numeric(v):
                    cp.numeric += 1
                if _looks_date(v):
                    cp.date_like += 1
                k = norm(v)
                if k not in cp._seen and len(cp._seen) < 1200:
                    cp._seen.add(k)
                    cp.distinct += 1
                    if len(cp.samples) < 3:
                        cp.samples.append(v)
        p.columns = cols
        numeric_cols = sum(1 for c in cols if c.non_empty and c.numeric / c.non_empty > 0.6)
        text_ratio = sum(1 for c in cols if c.non_empty and c.numeric / c.non_empty <= 0.6) / max(1, sum(1 for c in cols if c.non_empty))
        p._numeric_ratio = 1 - text_ratio  # type: ignore[attr-defined]
        p._text_ratio = text_ratio  # type: ignore[attr-defined]
        p._numeric_cols = numeric_cols  # type: ignore[attr-defined]
        # business totals for reconciliation preview
        for j, h in enumerate(headers):
            hn = norm(h)
            if hn in AMOUNT_HEADER_CANDIDATES and p.amount_total is None:
                vals = [_to_float(r[j]) for r in data_rows if j < len(r)]
                if any(v is not None for v in vals):
                    p.amount_total = round(sum(v for v in vals if v is not None), 2)
            if hn in QTY_HEADER_CANDIDATES and p.quantity_total is None:
                vals = [_to_float(r[j]) for r in data_rows if j < len(r)]
                if any(v is not None for v in vals):
                    p.quantity_total = round(sum(v for v in vals if v is not None), 2)
        profiles.append(p)
    return profiles


def _to_float(v):
    try:
        if _is_empty_cell(v):
            return None
        return float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Entity detection + column mapping
# --------------------------------------------------------------------------

def _name_of(p: SheetProfile) -> str:
    return norm(re.sub(r"[\s_\-]+", " ", p.name))


def _is_internal_sheet(p: SheetProfile) -> str:
    """Return a reason if this sheet is an internal/meta table, else ''."""
    name = _name_of(p)
    if p.duplicate_name_of:
        return f"Duplicate sheet name (also present as '{p.duplicate_name_of}')."
    toks = set(re.split(r"[^a-z0-9]+", name))
    if p.name.strip().startswith("__") or toks & {"meta", "schema_version", "system_lock"}:
        return "Internal metadata sheet of the exported database."
    if "audit" in toks and "log" in toks:
        return "Audit trail table — historical system log, not migration data."
    all_entity_tokens = {t for group in SHEET_ENTITY_TOKENS.values() for t in group}
    # *_item sheets are children, not internal noise
    if any(norm(p.name).endswith(s) for s in ITEM_SHEET_SUFFIXES):
        return ""
    # The ERP's own derived ledgers carry their provenance columns; they are
    # recomputed by the application, never imported from a workbook.
    header_keys = {norm(h).replace("_", " ") for h in p.headers}
    if {"source_module", "source_table"} <= header_keys or {"source module", "source table"} <= header_keys:
        return "Derived ledger/journal table maintained by the application (recomputed, not importable)."
    if toks & INTERNAL_SHEET_TOKENS and not (toks & all_entity_tokens):
        return "Internal application table (settings/users/audit/join table) — not a legacy business entity."
    return ""


def _header_tokens(p: SheetProfile) -> set[str]:
    out: set[str] = set()
    for h in p.headers:
        out |= _tokens(h)
    return out


#: Entity kinds that describe master records — penalised when the sheet looks
#: like a movement register or an auxiliary lookup table.
MASTER_ENTITIES = {"CLIENTS", "SUPPLIERS", "MATERIALS", "ACCOUNTS", "OPENING_BALANCES"}

#: Each master entity must find at least one *distinguishing* header token or it
#: is probably a lookup/child table, not the master table itself.
#: Master entities are only “MAPPED” when a source column backs their identity name.
MASTER_NAME_COLUMN = {"CLIENTS": "Client Name", "SUPPLIERS": "Supplier Name",
                      "MATERIALS": "Material Name", "ACCOUNTS": "Account Name"}

MASTER_DISTINCTIVE = {
    "CLIENTS": {"phone", "mobile", "address", "balance", "due", "opening", "code", "contact", "book", "page"},
    "SUPPLIERS": {"phone", "address", "balance", "opening", "material", "payable", "purchase"},
    "MATERIALS": {"unit", "price", "rate", "stock", "qty", "quantity", "code", "uom", "total"},
    "ACCOUNTS": {"number", "holder", "branch", "balance", "bank", "opening", "type"},
    "OPENING_BALANCES": {"amount", "qty", "quantity", "balance", "value", "date"},
}


def _is_item_sheet(p: SheetProfile) -> bool:
    name = norm(p.name)
    return any(name.endswith(s) for s in ITEM_SHEET_SUFFIXES) or "_item" in name


def detect_entities(profiles: list[SheetProfile]) -> None:
    """Annotate each profile with entity/confidence/evidence/mapping in place."""
    for p in profiles:
        if p.empty or p.rows_found == 0:
            p.status = "IGNORED"
            p.reason = p.reason or "Sheet is empty."
            continue
        internal_reason = _is_internal_sheet(p)
        if internal_reason:
            p.status, p.reason = "IGNORED", internal_reason
            continue
        name_str = _name_of(p)
        name_toks = set(re.split(r"[^a-z0-9]+", name_str))
        header_toks = _header_tokens(p)
        item_sheet = _is_item_sheet(p)
        best_entity, best_score, best_evidence = "UNKNOWN", 0.0, []
        for entity, tokens in SHEET_ENTITY_TOKENS.items():
            score, evidence = 0.0, []
            name_hits = name_toks & tokens
            if entity == "DIRECT_SALES" and "direct sale" in name_str and "draft" not in name_toks:
                name_hits = {"direct sale"}
                score += 0.5  # more specific than plain SALES for a "direct sale" sheet
            if name_hits:
                score += 2.0
                evidence.append(f"sheet name matches {sorted(name_hits)}")
            sig = HEADER_SIGNATURES[entity]
            if name_any := (sig["name_any"] & header_toks):
                score += 1.5
                evidence.append(f"headers carry primary column(s) {sorted(name_any)}")
            extra_hits = header_toks & sig["extra_any"]
            score += min(1.5, 0.35 * len(extra_hits))
            if extra_hits:
                evidence.append(f"headers carry {len(extra_hits)} supporting column(s)")
            hint = sig.get("row_hint", {})
            if "text" in hint and getattr(p, "_text_ratio", 0) >= hint["text"]:
                score += 0.5
                evidence.append("data is mostly textual (master-data like)")
            if "num" in hint and getattr(p, "_numeric_cols", 0) >= 1:
                score += 0.5
                evidence.append("data carries numeric measure columns")
            # Guardrails: master entities must not be claimed by movement
            # registers, lookup tables, or child _item sheets.
            if entity in MASTER_ENTITIES:
                if item_sheet:
                    score -= 3.0
                    evidence.append("child/item sheet — not a master table")
                if len(header_toks & TRANSACTION_MARKER_TOKENS) >= 2:
                    score -= 1.0
                    evidence.append("movement-register style columns present")
                if name_toks & AUXILIARY_TOKENS:
                    score -= 2.0
                    evidence.append("auxiliary lookup table (category/type/counter)")
                elif not (header_toks & MASTER_DISTINCTIVE.get(entity, set())):
                    score -= 1.0
                    evidence.append("no distinctive master column found")
            if score > best_score:
                best_entity, best_score, best_evidence = entity, score, evidence
        if best_score >= 3.5:
            confidence = "HIGH"
        elif best_score >= 2.4:
            confidence = "MEDIUM"
        elif best_score >= 1.4:
            confidence = "LOW"
        else:
            best_entity, confidence, best_evidence = "UNKNOWN", "UNKNOWN", []
        p.entity, p.confidence, p.evidence, p.score = best_entity, confidence, best_evidence, round(best_score, 2)
        if confidence in ("HIGH", "MEDIUM"):
            aliases = ITEM_HEADER_ALIASES if item_sheet else COLUMN_ALIASES.get(best_entity, {})
            p.mapping = _match_template_header(aliases, p)
            mapped = sum(1 for v in p.mapping.values() if v)
            needs_name = MASTER_NAME_COLUMN.get(best_entity)
            name_ok = (not needs_name) or bool(p.mapping.get(needs_name))
            qty_rate_ok = (not item_sheet) or any(p.mapping.get(k) for k in ("Material", "Quantity"))
            if mapped < 2 or not name_ok or not qty_rate_ok:
                p.status = "NOT_MAPPED"
                p.confidence = "LOW"
                missing = []
                if mapped < 2: missing.append("fewer than two template fields matched")
                if not name_ok: missing.append(f"no source column for the required '{needs_name}' field")
                if not qty_rate_ok: missing.append("no Material/Quantity columns on a line-item sheet")
                p.reason = ("Looks like " + best_entity + " but " + "; ".join(missing) +
                            " — mapping review required before import, nothing was auto-imported.")
            else:
                p.status = "MAPPED"
                p.reason = f"Adapted to {best_entity} template ({confidence} confidence); review the mapping before import."
        elif internal_reason:
            p.status, p.reason = "IGNORED", internal_reason
        elif confidence == "LOW":
            p.status, p.reason = "NOT_MAPPED", f"Possible {best_entity} (LOW confidence) — mapping review required, nothing was imported automatically."
        else:
            p.status, p.reason = "NOT_MAPPED", "No matching legacy template or column mapping found for this sheet."



def _match_template_header(aliases: dict[str, list[str]], p: SheetProfile) -> dict[str, str]:
    """Ordered-alias matching. A source header matches a template field when it
    equals a priority-listed alias phrase (underscores treated as spaces) or,
    for multi-token phrases, when the header tokens fully contain the alias
    tokens (>=2) — so a column like ``can_view_client_ledger`` can never steal
    the ``client`` field from a real ``client_name`` column.
    """
    mapping: dict[str, str] = {}
    used: set[int] = set()

    def key(h: str) -> str:
        return norm(h).replace("_", " ").strip()

    for template_col, alias_list in aliases.items():
        for alias in alias_list:
            hit = next((i for i, h in enumerate(p.headers) if i not in used and key(h) == alias), None)
            if hit is not None:
                mapping[template_col] = p.headers[hit]
                used.add(hit)
                break
    for template_col, alias_list in aliases.items():
        if template_col in mapping:
            continue
        placed = False
        for alias in alias_list:
            at = set(alias.split())
            if len(at) < 2:
                continue
            for i, h in enumerate(p.headers):
                if i in used:
                    continue
                if at.issubset(_tokens(h)):
                    mapping[template_col] = p.headers[i]
                    used.add(i)
                    placed = True
                    break
            if placed:
                break
    return mapping


def map_columns(entity: str, p: SheetProfile) -> dict:
    """Return {template_header: source_header} for an adapted sheet."""
    aliases = ITEM_HEADER_ALIASES if _is_item_sheet(p) else (COLUMN_ALIASES.get(entity) or {})
    return _match_template_header(aliases, p)


def find_entity_sheets(profiles: list[SheetProfile], entity: str, allow_low: bool = False) -> list[SheetProfile]:
    out = [p for p in profiles if p.entity == entity and p.status == "MAPPED" and p.mapping]
    if allow_low:
        out += [p for p in profiles if p.entity == entity and p.confidence == "LOW"]
    return out


def _row_value(row: list, headers: list[str], source_header: str | None):
    if not source_header:
        return ""
    try:
        i = headers.index(source_header)
    except ValueError:
        return None
    if i >= len(row) or _is_empty_cell(row[i]):
        return ""
    v = row[i]
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return str(v).strip()


def adapt_sheet_rows(entity: str, p: SheetProfile, rows_provider, id_ref_maps: dict | None = None, target_sheet: str | None = None) -> list[tuple[int, dict, list[dict]]]:
    """Convert a legacy sheet into template-shaped row dicts.

    Returns list of (excel_row_number, data, notes) where data mirrors the
    official template columns of ``target_sheet`` for ``entity``. ``id_ref_maps``
    lets rows resolve foreign keys (e.g. booking_item.booking_id -> booking.id)
    from other sheets inside the same workbook.
    """
    from app.services.legacy_migration import TEMPLATES  # local import: avoid cycle

    spec = TEMPLATES[entity]
    target_sheet = target_sheet or list(spec["sheets"].keys())[0]
    out: list[tuple[int, dict, list[dict]]] = []
    headers = list(p.headers)
    ref_src = p.mapping.get("Legacy Reference") or p.mapping.get(f"Legacy {entity.title()} Reference") \
        or p.mapping.get("Legacy GRN Reference") or p.mapping.get("Legacy Booking Reference") \
        or p.mapping.get("Legacy Sale Reference") or p.mapping.get("Legacy Payment Reference") \
        or p.mapping.get("Legacy Delivery Reference") or p.mapping.get("Legacy Expense Reference")
    all_rows = list(rows_provider(p))
    for offset, row in enumerate(all_rows[p.header_row:]):
        excel_row = p.header_row + 1 + offset
        if not any(not _is_empty_cell(v) for v in row):
            continue
        row_notes: list[dict] = []
        data: dict = {}
        for template_col in _template_columns(spec, target_sheet):
            src = p.mapping.get(template_col)
            data[template_col] = _row_value(list(row), headers, src)
        # synthetic/stable legacy reference (derivable — rule B/C of the policy)
        if not data.get(_reference_field(spec, target_sheet)):
            import hashlib
            digest = hashlib.sha1(("|".join(norm(v) for v in row)).encode()).hexdigest()[:10]
            row_notes.append({"kind": "DERIVED", "column": _reference_field(spec, target_sheet),
                              "message": "No source id column; a stable reference was derived from file+sheet+row for traceability."})
            data[_reference_field(spec, target_sheet)] = f"AUTO-{p.name[:10]}-{excel_row}-{digest}" 
        # resolve FK style values through workbook-internal maps (e.g. category_id -> name)
        for col in ("Category", "Client", "Supplier", "Material", "Party", "Account"):
            val = data.get(col, "")
            if col in p.mapping and _looks_id_value(val):
                lookup = (id_ref_maps or {}).get(f"{entity}:{col}") or (id_ref_maps or {}).get(f"*:{col}") or {}
                resolved = lookup.get(str(val).strip(), "")
                if resolved:
                    data[col] = resolved
        out.append((excel_row, data, row_notes))
    return out


def _template_columns(spec: dict, target_sheet: str) -> list[str]:
    cols = spec["sheets"].get(target_sheet)
    return [h.rstrip("*").strip() for h in (cols or [])]


def _reference_field(spec: dict, target_sheet: str) -> str:
    for h in spec["sheets"].get(target_sheet, []):
        if "Reference" in h:
            return h.rstrip("*").strip()
    return "Legacy Reference"


def _looks_id_value(v) -> bool:
    s = str(v or "").strip()
    return bool(re.fullmatch(r"\d{1,9}", s))


def internal_lookup_maps(profiles: list[SheetProfile], rows_provider) -> dict:
    """id -> name maps built from *other sheets of the same workbook*, used to
    turn legacy foreign-key numbers into the names the ERP adapters expect.
    Keys are '<ENTITY>:<column>' plus generic '*:<column>' fallbacks."""
    maps: dict[str, dict[str, str]] = {}

    def id_name_lookup(p: SheetProfile, name_headers: set[str]):
        headers = list(p.headers)
        id_h = next((h for h in headers if norm(h).replace("_", " ") in {"id", "code"}), None)
        name_h = next((h for h in headers if norm(h).replace("_", " ") in name_headers), None)
        if not id_h or not name_h:
            return {}
        lookup: dict[str, str] = {}
        i, j = headers.index(id_h), headers.index(name_h)
        for row in list(rows_provider(p))[p.header_row:]:
            if i < len(row) and j < len(row) and not _is_empty_cell(row[i]) and not _is_empty_cell(row[j]):
                lookup[str(row[i]).strip()] = str(row[j]).strip()
        return lookup

    # category names from auxiliary *_category sheets (id -> name)
    for p in profiles:
        if "category" in _tokens(p.name) or "categories" in _tokens(p.name):
            lookup = id_name_lookup(p, {"name", "category name"})
            if lookup:
                maps.setdefault("MATERIALS:Category", {}).update(lookup)
                maps.setdefault("*:Category", {}).update(lookup)
    # party names from the detected master sheets themselves (id -> name)
    for entity, col, name_headers in (
        ("CLIENTS", "Client", {"name", "client name"}),
        ("CLIENTS", "Party", {"name", "client name"}),
        ("SUPPLIERS", "Supplier", {"name", "supplier name"}),
        ("SUPPLIERS", "Party", {"name", "supplier name"}),
        ("MATERIALS", "Material", {"name", "material name", "product name"}),
        ("ACCOUNTS", "Account", {"name", "account name"}),
    ):
        for p in profiles:
            if p.entity != entity or p.status != "MAPPED" or _is_item_sheet(p):
                continue
            lookup = id_name_lookup(p, name_headers)
            if lookup:
                maps.setdefault(f"{entity}:{col}", {}).update(lookup)
                maps.setdefault(f"*:{col}", {}).update(lookup)
    return maps


def recommended_order(profiles: list[SheetProfile]) -> list[dict]:
    detected = {p.entity for p in profiles if p.status == "MAPPED"}
    order = []
    for step, deps in IMPORT_ORDER:
        if step == "CATEGORIES":
            continue
        present = step in detected or (step == "DIRECT_SALES" and "SALES" in detected) or (step == "SALES" and "DIRECT_SALES" in detected)
        if not present:
            continue
        satisfied = [d for d in deps if d == "CATEGORIES" or d in detected]
        order.append({"entity": step, "dependencies": deps,
                      "satisfied": all(d in detected or d == "CATEGORIES" for d in deps if d in detected) if deps else True,
                      "pending_dependencies": [d for d in deps if d not in detected]})
    return order
