"""Automatic verification after import/restore. Fail the operation if any check fails."""
from __future__ import annotations

import sqlite3
from typing import Any

from data_ops.constants import MONEY_COLUMNS


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        out[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    return out


def money_totals(conn: sqlite3.Connection) -> dict[str, float]:
    totals: dict[str, float] = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
        for col in cols:
            if col in MONEY_COLUMNS:
                val = conn.execute(f'SELECT COALESCE(SUM("{col}"), 0) FROM "{name}"').fetchone()[0]
                totals[f"{name}.{col}"] = float(val or 0)
    return totals


def verify_database(conn: sqlite3.Connection) -> dict[str, Any]:
    failures: list[str] = []
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    void_survivors = 0
    for t in tables:
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')}
        if "is_void" in cols:
            n = conn.execute(
                f'SELECT COUNT(*) FROM "{t}" WHERE is_void IN (1, "1", "true", "TRUE")'
            ).fetchone()[0]
            # Soft-delete tables MAY contain void rows in live DB; after *import* they must not.
            # Verification here only flags them; the loader refuses them. Restore of a live
            # snapshot may still contain voids — report, do not fail restore of snapshots.
            if n:
                void_survivors += n
    fk_bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_bad:
        failures.append(f"unresolved foreign keys: {fk_bad[:15]!r}")

    client_ids = set()
    if "client" in tables:
        client_ids = {r[0] for r in conn.execute("SELECT id FROM client")}
        for t in tables:
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')}
            if "client_id" in cols:
                orphans = conn.execute(
                    f'SELECT COUNT(*) FROM "{t}" WHERE client_id IS NOT NULL AND client_id NOT IN (SELECT id FROM client)'
                ).fetchone()[0]
                if orphans:
                    failures.append(f"{t}: {orphans} client_id values do not resolve")

    counts = row_counts(conn)
    money = money_totals(conn)
    return {
        "ok": not failures,
        "failures": failures,
        "row_counts": counts,
        "money_totals": money,
        "void_rows_present": void_survivors,
        "client_count": len(client_ids),
    }
