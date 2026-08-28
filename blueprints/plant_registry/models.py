"""ORM models owned by the ``plant_registry`` module.

Two tables, both purely additive: no core ERP table is touched, and no core
foreign key is altered.  Money follows the application convention — a legacy
``REAL`` display column plus an authoritative integer minor-unit mirror kept in
step by :func:`utils.money.sync_money_fields`.

Importing this module is what registers the tables on ``db.metadata``; the
manifest declares it under ``database.models_import`` so the registry imports
it *before* the blueprint is mounted.
"""
from __future__ import annotations

from models import db
from models.__base import pk_model_now
from utils.money import sync_money_fields


class PlantAsset(db.Model):
    """One item of plant/equipment on a site."""

    __tablename__ = "plant_asset"

    id = db.Column(db.Integer, primary_key=True)
    asset_code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(60), default="general", index=True)
    status = db.Column(db.String(32), default="available", index=True)
    location = db.Column(db.String(160), default="")
    # Normalised from ``location`` by a *data* revision, never by the UI alone.
    location_code = db.Column(db.String(12), index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=True, index=True)
    commissioned_on = db.Column(db.Date)
    purchase_value = db.Column(db.Float, default=0.0)
    purchase_value_minor = db.Column(db.BigInteger, default=0)
    notes = db.Column(db.String(500), default="")
    is_active = db.Column(db.Boolean, default=True, index=True)
    idempotency_key = db.Column(db.String(64), index=True)
    revision = db.Column(db.Integer, default=1, nullable=False)
    created_by = db.Column(db.String(80))
    updated_by = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=pk_model_now)
    updated_at = db.Column(db.DateTime, default=pk_model_now, onupdate=pk_model_now)

    movements = db.relationship(
        "PlantAssetMovement",
        back_populates="asset",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    __table_args__ = (
        db.Index("ix_plant_asset_status_location", "status", "location"),
    )

    def sync_money(self) -> None:
        sync_money_fields(self, "purchase_value", "purchase_value_minor")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "asset_code": self.asset_code,
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "location": self.location,
            "location_code": self.location_code,
            "supplier_id": self.supplier_id,
            "commissioned_on": self.commissioned_on.isoformat() if self.commissioned_on else None,
            "purchase_value": float(self.purchase_value or 0),
            "purchase_value_minor": int(self.purchase_value_minor or 0),
            "notes": self.notes,
            "is_active": bool(self.is_active),
            "revision": int(self.revision or 1),
        }


class PlantAssetMovement(db.Model):
    """Where an asset moved, kept as a log rather than a mutable field."""

    __tablename__ = "plant_asset_movement"

    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("plant_asset.id"), nullable=False, index=True)
    movement_date = db.Column(db.Date, default=pk_model_now, index=True)
    from_location = db.Column(db.String(160), default="")
    to_location = db.Column(db.String(160), default="")
    reason = db.Column(db.String(200), default="")
    created_by = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=pk_model_now)

    asset = db.relationship("PlantAsset", back_populates="movements")
