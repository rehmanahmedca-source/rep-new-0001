"""Single format_version string. Extend this field; do not invent a second one."""

FORMAT_VERSION = "2026-04"

# Parent table -> child tables whose rows die with a voided parent.
OWNED_CHILDREN = {
    "direct_sale": ["direct_sale_item", "delivery_rent"],
    "booking": ["booking_item"],
    "grn": ["grn_item"],
    "invoice": ["invoice_item"],
}

MONEY_COLUMNS = {
    "amount",
    "paid_amount",
    "balance",
    "opening_balance",
    "total",
    "subtotal",
    "discount",
    "rate",
    "unit_price",
    "price",
    "rent",
    "qty",
    "quantity",
}

CLIENT_TEXT_FIELDS = ("client_code", "client_name", "client")
