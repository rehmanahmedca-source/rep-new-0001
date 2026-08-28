"""Business rules for the plant register, kept out of the route layer.

Validation mirrors the ERP's own conventions rather than inventing new ones:
amounts are exact minor units and a negative purchase value is *rejected* (the
same rule ``save_client_payment`` learned in the STEP B audit), codes are
normalised here instead of in the template, and every write bumps ``revision``
and stamps the acting user.
"""
from __future__ import annotations

import re

from sqlalchemy import func, or_

from blueprints.plant_registry._common import STATUS_CHOICES, normalise_code, pk_now
from models import db
from utils.money import decimal_money, to_minor

CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,31}$")


class PlantRegistryError(ValueError):
    """A rejected user action: safe to surface as a flash message."""


def _clean(value, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def parse_date(value):
    raw = _clean(value, 20)
    if not raw:
        return None
    from datetime import datetime

    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    raise PlantRegistryError(f"Commissioned date '{raw}' is not a valid date (use YYYY-MM-DD).")


def normalise_code_input(value: str, name: str) -> str:
    code = _clean(value, 32).upper().replace(" ", "-")
    if not code:
        code = normalise_code(name)
    if not CODE_RE.match(code):
        raise PlantRegistryError(
            "Asset code must be 2-32 characters of A-Z, 0-9, '-' or '_'."
        )
    return code


def list_assets(*, status: str = "", location: str = "", search: str = "", include_retired: bool = False):
    from blueprints.plant_registry.models import PlantAsset

    query = db.session.query(PlantAsset)
    if status:
        query = query.filter(PlantAsset.status == status)
    if location:
        query = query.filter(func.upper(func.coalesce(PlantAsset.location_code, "")) == location.upper())
    if not include_retired:
        query = query.filter(PlantAsset.is_active.is_(True))
    if search:
        needle = f"%{search.strip()}%"
        query = query.filter(
            or_(
                PlantAsset.name.ilike(needle),
                PlantAsset.asset_code.ilike(needle),
                PlantAsset.location.ilike(needle),
            )
        )
    return query.order_by(PlantAsset.asset_code.asc()).all()


def get_asset(asset_id: int):
    from blueprints.plant_registry.models import PlantAsset

    return db.session.get(PlantAsset, int(asset_id))


def locations() -> list[dict]:
    """Grouping used by the filter strip and the health check."""
    from blueprints.plant_registry.models import PlantAsset

    rows = (
        db.session.query(
            func.coalesce(PlantAsset.location_code, "").label("code"),
            func.max(PlantAsset.location).label("location"),
            func.count(PlantAsset.id).label("assets"),
        )
        .group_by(func.coalesce(PlantAsset.location_code, ""))
        .order_by(func.count(PlantAsset.id).desc())
        .all()
    )
    return [
        {"code": row.code or "", "location": row.location or "", "assets": int(row.assets or 0)} for row in rows
    ]


def save_asset(form, *, actor: str, asset=None) -> "object":
    """Create or update one asset.  Never commits partial work."""
    from blueprints.plant_registry.models import PlantAsset, PlantAssetMovement

    name = _clean(form.get("name"), 160)
    if not name:
        raise PlantRegistryError("Asset name is required.")
    status = _clean(form.get("status"), 32).lower() or "available"
    if status not in STATUS_CHOICES:
        raise PlantRegistryError(f"Status must be one of: {', '.join(STATUS_CHOICES)}.")
    category = _clean(form.get("category"), 60) or "general"
    location = _clean(form.get("location"), 160)
    notes = _clean(form.get("notes"), 500)
    asset_code = normalise_code_input(_clean(form.get("asset_code"), 32), name)
    raw_value = form.get("purchase_value")
    if raw_value is not None and str(raw_value).strip() == "":
        raw_value = 0
    try:
        amount = decimal_money(raw_value, field="Purchase value")
    except Exception as exc:  # MoneyValueError
        raise PlantRegistryError(str(exc)) from exc
    if amount < 0:
        # A negative purchase value is a mistake, never a silent sign flip.
        raise PlantRegistryError("Purchase value cannot be negative.")

    is_active = str(form.get("is_active", "1")).strip().lower() not in ("0", "false", "no", "off", "")

    with db.session.no_autoflush:
        clash = db.session.query(PlantAsset).filter(PlantAsset.asset_code == asset_code).first()
        if clash is not None and (asset is None or clash.id != asset.id):
            raise PlantRegistryError(f"Asset code '{asset_code}' is already used by another asset.")

    created = asset is None
    if created:
        asset = PlantAsset(asset_code=asset_code, created_by=actor)
        db.session.add(asset)
    asset.name = name
    asset.category = category
    asset.status = status
    asset.location = location
    asset.location_code = normalise_code(location)
    asset.notes = notes
    asset.is_active = is_active
    asset.purchase_value = float(amount)
    asset.purchase_value_minor = to_minor(amount, field="Purchase value")
    asset.updated_by = actor
    asset.revision = int(asset.revision or 1) + (0 if created else 1)
    asset.commissioned_on = parse_date(form.get("commissioned_on"))
    key = _clean(form.get("idempotency_key"), 64)
    if key:
        asset.idempotency_key = key

    moved = _clean(form.get("move_to"), 160)
    try:
        db.session.flush()
        if created and asset.purchase_value_minor < 0:  # defensive, never expected
            raise PlantRegistryError("Purchase value cannot be negative.")
        if created or moved:
            db.session.add(
                PlantAssetMovement(
                    asset_id=asset.id,
                    movement_date=pk_now().date(),
                    from_location="" if created else (asset.location if moved else ""),
                    to_location=(location if created else moved or location),
                    reason="registered" if created else "moved",
                    created_by=actor,
                )
            )
        db.session.commit()
    except PlantRegistryError:
        db.session.rollback()
        raise
    except Exception as exc:
        db.session.rollback()
        raise PlantRegistryError(f"The asset could not be saved ({type(exc).__name__}). No changes were written.") from exc
    return asset


def set_status(asset, status: str, *, actor: str) -> "object":
    from blueprints.plant_registry.models import PlantAsset

    status = _clean(status, 32).lower()
    if status not in STATUS_CHOICES:
        raise PlantRegistryError(f"Status must be one of: {', '.join(STATUS_CHOICES)}.")
    asset.status = status
    asset.is_active = status != "retired"
    asset.updated_by = actor
    asset.revision = int(asset.revision or 1) + 1
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        raise PlantRegistryError(f"Status could not be changed ({type(exc).__name__}).") from exc
    return asset


def summary() -> dict:
    from blueprints.plant_registry.models import PlantAsset

    rows = (
        db.session.query(PlantAsset.status, func.count(PlantAsset.id), func.sum(PlantAsset.purchase_value_minor))
        .group_by(PlantAsset.status)
        .all()
    )
    by_status = {status: {"assets": int(count or 0), "value_minor": int(total or 0)} for status, count, total in rows}
    return {
        "assets": sum(item["assets"] for item in by_status.values()),
        "total_value_minor": sum(item["value_minor"] for item in by_status.values()),
        "by_status": by_status,
        "locations": locations(),
    }
