"""Revision 2026_001 — create the plant register tables.

Additive only: two new tables, no existing table is altered, nothing is dropped.
Safe to re-run (every statement is ``IF NOT EXISTS``), and ``verify()`` proves
the tables actually match what the module's models declare, which is what the
old silent ``CREATE TABLE IF NOT EXISTS`` pattern never did.
"""
from __future__ import annotations

from sqlalchemy import text

REVISION = "2026_001"
TITLE = "create_plant_registry_tables"
MODULE = "plant_registry"
KIND = "schema"
DESTRUCTIVE = False
DEPENDS_ON: tuple[str, ...] = ()

PLANT_ASSET = """
CREATE TABLE IF NOT EXISTS plant_asset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_code VARCHAR(32) NOT NULL UNIQUE,
    name VARCHAR(160) NOT NULL,
    category VARCHAR(60) DEFAULT 'general',
    status VARCHAR(32) DEFAULT 'available',
    location VARCHAR(160) DEFAULT '',
    location_code VARCHAR(12),
    supplier_id INTEGER REFERENCES supplier(id),
    commissioned_on DATE,
    purchase_value FLOAT DEFAULT 0,
    purchase_value_minor BIGINT DEFAULT 0,
    notes VARCHAR(500) DEFAULT '',
    is_active BOOLEAN DEFAULT 1,
    idempotency_key VARCHAR(64),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by VARCHAR(80),
    updated_by VARCHAR(80),
    created_at DATETIME,
    updated_at DATETIME
)
"""

PLANT_ASSET_MOVEMENT = """
CREATE TABLE IF NOT EXISTS plant_asset_movement (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES plant_asset(id),
    movement_date DATE,
    from_location VARCHAR(160) DEFAULT '',
    to_location VARCHAR(160) DEFAULT '',
    reason VARCHAR(200) DEFAULT '',
    created_by VARCHAR(80),
    created_at DATETIME
)
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_plant_asset_category ON plant_asset (category)",
    "CREATE INDEX IF NOT EXISTS ix_plant_asset_status ON plant_asset (status)",
    "CREATE INDEX IF NOT EXISTS ix_plant_asset_location_code ON plant_asset (location_code)",
    "CREATE INDEX IF NOT EXISTS ix_plant_asset_supplier_id ON plant_asset (supplier_id)",
    "CREATE INDEX IF NOT EXISTS ix_plant_asset_is_active ON plant_asset (is_active)",
    "CREATE INDEX IF NOT EXISTS ix_plant_asset_status_location ON plant_asset (status, location)",
    "CREATE INDEX IF NOT EXISTS ix_plant_asset_movement_asset_id ON plant_asset_movement (asset_id)",
    "CREATE INDEX IF NOT EXISTS ix_plant_asset_movement_date ON plant_asset_movement (movement_date)",
)

EXPECTED_COLUMNS = {
    "plant_asset": {
        "asset_code",
        "name",
        "category",
        "status",
        "location",
        "location_code",
        "supplier_id",
        "commissioned_on",
        "purchase_value",
        "purchase_value_minor",
        "notes",
        "is_active",
        "idempotency_key",
        "revision",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
    },
    "plant_asset_movement": {"asset_id", "movement_date", "from_location", "to_location", "reason", "created_by", "created_at"},
}


def upgrade(connection) -> dict:
    """Create tables + indexes.  Returns a small report for the ledger."""
    connection.execute(text(PLANT_ASSET))
    connection.execute(text(PLANT_ASSET_MOVEMENT))
    for statement in INDEXES:
        connection.execute(text(statement))
    return {"created": ["plant_asset", "plant_asset_movement"], "indexes": len(INDEXES)}


def verify(connection) -> None:
    """Raise unless both tables exist with every column the module needs."""
    for table, columns in EXPECTED_COLUMNS.items():
        row = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name = :table"), {"table": table}
        ).first()
        if row is None:
            raise AssertionError(f"migration reported success but table '{table}' does not exist")
        live = {str(r[1]) for r in connection.execute(text(f"PRAGMA table_info('{table}')")).fetchall()}
        missing = sorted(columns - live)
        if missing:
            raise AssertionError(f"table '{table}' is missing column(s): {', '.join(missing)}")
