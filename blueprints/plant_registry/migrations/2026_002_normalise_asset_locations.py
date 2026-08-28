"""Revision 2026_002 — normalise ``plant_asset.location_code`` (data change).

This is the template for a *safe data migration* (Phase 8 of the standard):

1. analyse the rows that will be touched;
2. transform them additively (a new column is filled; nothing is deleted,
   renamed or re-typed);
3. rows that cannot be mapped are **left untouched** and listed in an exception
   report instead of being guessed at or dropped;
4. ``verify()`` proves the record count did not move and that every remaining
   gap is an exception that was explicitly reported.

The pipeline takes the database backup before this revision runs, and rolls the
whole transaction back if ``verify()`` fails, so a bad transform cannot half
apply.
"""
from __future__ import annotations

import re

from sqlalchemy import text

REVISION = "2026_002"
TITLE = "normalise_asset_locations"
MODULE = "plant_registry"
KIND = "data"
DESTRUCTIVE = False
DATA_VALIDATION = True
DEPENDS_ON = ("2026_001",)

_CODE_RE = re.compile(r"[^A-Z0-9]+")

#: carried between upgrade() and verify() so the verification can prove what the
#: transform actually did.
_STATE: dict = {}


def _normalise(location: str) -> str:
    text_value = (location or "").strip().upper()
    parts = [part for part in _CODE_RE.split(text_value) if part]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:12]
    return ("".join(part[0] for part in parts) + parts[0][:6])[:12]


def upgrade(connection) -> dict:
    """Backfill ``location_code`` for every asset that has a usable location."""
    rows = connection.execute(
        text("SELECT id, location, location_code FROM plant_asset ORDER BY id")
    ).fetchall()
    total = len(rows)
    candidates = [row for row in rows if (row[1] or "").strip()]
    transformable: list[tuple[int, str]] = []
    exceptions: list[dict] = []
    for row in candidates:
        code = _normalise(row[1])
        if not code:
            exceptions.append(
                {
                    "id": int(row[0]),
                    "location": str(row[1] or "")[:120],
                    "problem": "location contains no A-Z/0-9 characters, so no code can be derived",
                    "action": "left unchanged; fix the location text, then re-run by adding a follow-up revision",
                }
            )
            continue
        if (row[2] or "") != code:
            transformable.append((int(row[0]), code))

    for asset_id, code in transformable:
        connection.execute(
            text("UPDATE plant_asset SET location_code = :code WHERE id = :id AND (location_code IS NULL OR location_code <> :code)"),
            {"code": code, "id": asset_id},
        )

    _STATE.update(
        {
            "rows_seen": total,
            "rows_with_location": len(candidates),
            "rows_updated": len(transformable),
            "exceptions": exceptions,
        }
    )
    return {
        "rows_seen": total,
        "rows_updated": len(transformable),
        "exceptions": len(exceptions),
        "exception_report": exceptions[:50],
        "policy": "no row was deleted, renamed or emptied",
    }


def verify(connection) -> None:
    """Fail loudly rather than let a partial transform pass as applied."""
    rows = connection.execute(text("SELECT id, location, location_code FROM plant_asset")).fetchall()
    if _STATE and len(rows) != _STATE.get("rows_seen"):
        raise AssertionError(
            f"record count changed during a data revision ({_STATE.get('rows_seen')} -> {len(rows)}); "
            "a data migration must not add or remove rows"
        )
    exception_ids = {int(item["id"]) for item in _STATE.get("exceptions", [])}
    unresolved = [
        int(row[0])
        for row in rows
        if (row[1] or "").strip() and not (row[2] or "").strip() and int(row[0]) not in exception_ids
    ]
    if unresolved:
        raise AssertionError(
            f"{len(unresolved)} asset(s) have a location but no location_code and were not reported "
            f"as exceptions (ids: {unresolved[:10]})"
        )
