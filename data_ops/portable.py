"""Portable envelope: schema-aware, versioned data archive.

Wire format (see docs/DATA_CENTER.md §2)::

    {
      "kind": "ams.data-archive",
      "format_version": "...",
      "app_version": "...",
      "exported_at": "...",
      "database": {...},
      "schema": {table: {columns, primary_key, foreign_keys}},
      "tables": {table: [row, ...]},
      "stats": {table: {rows, columns}}
    }

The ``schema`` block is what makes a restore safe after the app has shipped
new versions: the restore planner diffs *file schema* against *target schema*
instead of trusting a version string alone.  Old flat files
(``{"format_version": ..., "tables": {...}}``) are still accepted; their schema
is inferred from the rows.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data_ops.constants import FORMAT_VERSION, ARCHIVE_KIND

_TTABLE_SQL = (
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY 1"
)


def tables_of(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(_TTABLE_SQL)]


def table_columns(conn: sqlite3.Connection, table: str) -> list[dict]:
    """PRAGMA table_info rows as dicts ({cid,name,type,notnull,dflt,pk})."""
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [
        {"cid": r[0], "name": r[1], "type": r[2], "notnull": r[3], "dflt": r[4], "pk": r[5]}
        for r in rows
    ]


def table_schema(conn: sqlite3.Connection, table: str) -> dict:
    cols = table_columns(conn, table)
    pks = [c["name"] for c in cols if c.get("pk")]
    fks = []
    for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
        # (id, seq, table, from, to, on_update, on_delete, match)
        fks.append(
            {"from": fk[3], "table": fk[2], "to": fk[4],
             "on_update": fk[5], "on_delete": fk[6]}
        )
    return {"columns": cols, "primary_key": pks, "foreign_keys": fks}


def db_schema(conn: sqlite3.Connection) -> dict[str, dict]:
    return {t: table_schema(conn, t) for t in tables_of(conn)}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _jsonable(value: Any) -> Any:
    """Make sqlite values JSON-safe (bytes, Decimal, dates)."""
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if hasattr(value, "isoformat") and not isinstance(value, (str, int, float, bool)):
        return value.isoformat()
    if isinstance(value, float) and value != value:  # NaN is not valid JSON
        return None
    return value


def export_json(
    conn: sqlite3.Connection,
    dest: str | Path,
    *,
    tables: list[str] | None = None,
    app_version: str = "",
    db_name: str = "",
) -> dict:
    """Write a schema-aware archive to *dest*; return {"tables", "rows", "path"}."""
    dest = Path(dest)
    names = tables_of(conn) if tables is None else [t for t in tables if t in set(tables_of(conn))]
    schema: dict[str, Any] = {}
    data: dict[str, list[dict]] = {}
    stats: dict[str, Any] = {}
    total = 0
    for name in names:
        cols = table_columns(conn, name)
        col_names = [c["name"] for c in cols]
        rows = [
            {c: _jsonable(tup[i]) for i, c in enumerate(col_names)}
            for tup in conn.execute(f'SELECT * FROM "{name}"')
        ]
        schema[name] = table_schema(conn, name)
        data[name] = rows
        stats[name] = {"rows": len(rows), "columns": len(col_names)}
        total += len(rows)

    db_name = db_name or str(getattr(conn, "_ams_db_name", "") or "")
    payload = {
        "kind": ARCHIVE_KIND,
        "format_version": FORMAT_VERSION,
        "app_version": app_version,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "database": {"name": db_name, "journal_mode": _journal_mode(conn)},
        "schema": schema,
        "tables": data,
        "stats": stats,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return {
        "path": str(dest),
        "tables": len(names),
        "rows": total,
        "sha256": _sha256_file(dest) if dest.is_file() else "",
        "format_version": FORMAT_VERSION,
    }


def _journal_mode(conn: sqlite3.Connection) -> str:
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0] or "")
    except sqlite3.Error:
        return ""


def load_payload(source: str | Path) -> dict:
    """Read an archive (path or file-like text) and return the parsed JSON dict."""
    if hasattr(source, "read"):
        raw = source.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    return json.loads(Path(source).read_text(encoding="utf-8"))


def normalize_payload(payload: dict) -> dict:
    """Accept both the envelope and the legacy flat format.

    Returns a dict with ``format_version``, ``tables`` and ``schema`` (possibly
    empty) plus ``legacy`` flag.
    """
    if not isinstance(payload, dict):
        raise ValueError("archive root must be a JSON object")
    kind = str(payload.get("kind") or "").strip()
    if kind and kind != ARCHIVE_KIND:
        raise ValueError(f"unknown archive kind {kind!r} — not an AMS data archive")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("archive must contain a 'tables' object keyed by table name")
    fmt = str(payload.get("format_version") or "").strip()
    if not fmt:
        raise ValueError("missing format_version (extend this field; do not invent another)")
    schema = payload.get("schema")
    if schema is None:
        schema = {}
    if not isinstance(schema, dict):
        raise ValueError("'schema' must be an object keyed by table name")
    return {
        "format_version": fmt,
        "tables": tables,
        "schema": schema,
        "legacy": not bool(kind) or not schema,
        "app_version": str(payload.get("app_version") or ""),
        "exported_at": str(payload.get("exported_at") or ""),
    }
