"""Module health check, run by the registry at startup and by ``dbupdate``.

Declares nothing about the rest of the ERP: it only proves that *this* module
is structurally sound (its tables exist, its codes are unique, no movement is
orphaned) so a broken module is reported instead of half-working.
"""
from __future__ import annotations

from sqlalchemy import func, text


def check_plant_registry(app) -> dict:
    from models import db

    try:
        with app.app_context():
            inspector_rows = db.session.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('plant_asset','plant_asset_movement')"
                )
            ).fetchall()
            present = {row[0] for row in inspector_rows}
            if present != {"plant_asset", "plant_asset_movement"}:
                return {
                    "status": "FAIL",
                    "detail": f"missing table(s): {sorted({'plant_asset', 'plant_asset_movement'} - present)}",
                    "next_action": "run: python tools/dbupdate.py apply",
                }
            assets = int(db.session.execute(text("SELECT COUNT(*) FROM plant_asset")).scalar() or 0)
            movements = int(db.session.execute(text("SELECT COUNT(*) FROM plant_asset_movement")).scalar() or 0)
            duplicate_codes = int(
                db.session.execute(
                    text(
                        "SELECT COUNT(*) FROM (SELECT asset_code FROM plant_asset "
                        "GROUP BY asset_code HAVING COUNT(*) > 1)"
                    )
                ).scalar()
                or 0
            )
            orphans = int(
                db.session.execute(
                    text(
                        "SELECT COUNT(*) FROM plant_asset_movement m "
                        "LEFT JOIN plant_asset a ON a.id = m.asset_id WHERE a.id IS NULL"
                    )
                ).scalar()
                or 0
            )
            unlocated = int(
                db.session.execute(
                    text("SELECT COUNT(*) FROM plant_asset WHERE location <> '' AND (location_code IS NULL OR location_code = '')")
                ).scalar()
                or 0
            )
    except Exception as exc:
        return {"status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"}

    if duplicate_codes or orphans:
        return {
            "status": "FAIL",
            "detail": f"{duplicate_codes} duplicate asset code(s), {orphans} orphan movement(s)",
            "next_action": "deduplicate plant_asset.asset_code and remove dangling movements",
            "assets": assets,
        }
    detail = f"{assets} asset(s), {movements} movement(s)"
    if unlocated:
        detail += f"; {unlocated} asset(s) still need a location_code (data revision 2026_002)"
        return {"status": "WARN", "detail": detail, "assets": assets, "movements": movements, "unlocated": unlocated}
    return {"status": "PASS", "detail": detail, "assets": assets, "movements": movements}
