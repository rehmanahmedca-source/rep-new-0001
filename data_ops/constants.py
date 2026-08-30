"""Single format_version string. Extend this field; do not invent a second one."""

#: Format of the *wire* (JSON envelope / flat tables). Bump only for a
#: non-additive change: a renamed/removed field, a retype, or a new mandatory
#: structure. A new table or column is ADDITIVE and needs no bump — the
#: restore planner diffs file schema vs target schema and bridges the gap.
FORMAT_VERSION = "2026-08"

#: Marker used in the JSON root; one kind for every AMS data archive.
ARCHIVE_KIND = "ams.data-archive"

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

#: Tables that are runtime/security scaffolding and never belong in a user
#: data archive (sessions, wipeclean audit, import job state).
EXPORT_EXCLUDE_TABLES = {
    "user_login_session",
    "tenant_wipe_backup_history",
}
