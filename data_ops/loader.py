"""Name-matched JSON loader. Aborts on unknown columns without a declared step."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from data_ops.coerce import CoercionError, coerce_value, is_null
from data_ops.constants import (
    CLIENT_TEXT_FIELDS,
    FORMAT_VERSION,
    OWNED_CHILDREN,
)
from data_ops.steps import covered_unknown_columns, steps_from


class SchemaAbort(RuntimeError):
    pass


def _pragma_table_info(conn: sqlite3.Connection, table: str) -> list[dict]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [{"cid": r[0], "name": r[1], "type": r[2], "notnull": r[3], "dflt": r[4], "pk": r[5]} for r in rows]


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY 1"
        )
    ]


def _truthy_void(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on", "y"):
        return True
    return False


def _client_identity(row: dict) -> str | None:
    for key in CLIENT_TEXT_FIELDS:
        v = row.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    cid = row.get("client_id")
    if cid not in (None, "", 0):
        return f"id:{cid}"
    return None


def load_legacy_json(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    dry_run: bool = False,
) -> dict:
    """Parse, validate, map, load, and report. Single transaction. Upsert on id."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SchemaAbort("Legacy JSON root must be an object with format_version and tables")

    source_version = str(payload.get("format_version") or "").strip()
    if not source_version:
        raise SchemaAbort("Missing format_version on the JSON root (extend this field; do not invent another)")

    tables_in = payload.get("tables")
    if not isinstance(tables_in, dict):
        raise SchemaAbort("JSON must contain a 'tables' object keyed by table name")

    steps = steps_from(source_version, FORMAT_VERSION)
    covered = covered_unknown_columns(steps)
    target_tables = set(_tables(conn))
    report: dict[str, Any] = {
        "format_version_in": source_version,
        "format_version_out": FORMAT_VERSION,
        "steps_applied": [s.__dict__ for s in steps],
        "tables": {},
        "coercions": [],
        "synthetic_clients": [],
        "aborted": False,
        "ok": True,
    }

    # Column diffs before any write
    unknown_uncovered: list[str] = []
    for tname, rows in tables_in.items():
        if tname not in target_tables:
            # Unknown table: abort unless a drop step covers it
            if ("*", tname) not in covered and not any(s.kind == "drop" and s.table == tname for s in steps):
                unknown_uncovered.append(f"table {tname}")
            continue
        info = _pragma_table_info(conn, tname)
        target_cols = {c["name"] for c in info}
        if not rows:
            incoming = set()
        else:
            incoming = set()
            for row in rows:
                incoming.update(row.keys())
        matched = sorted(incoming & target_cols)
        new_in_target = sorted(target_cols - incoming)
        unknown = sorted(incoming - target_cols)
        leftover = [c for c in unknown if (tname, c) not in covered]
        report["tables"].setdefault(tname, {})["column_diff"] = {
            "matched": matched,
            "new_in_target": new_in_target,
            "unknown_in_file": unknown,
        }
        if leftover:
            unknown_uncovered.extend(f"{tname}.{c}" for c in leftover)

    if unknown_uncovered:
        raise SchemaAbort(
            "Unknown columns/tables not covered by a declared migration step: "
            + ", ".join(unknown_uncovered)
            + ". Add a rename/drop/split step or remove them from the file."
        )

    # Apply rename/derive in memory
    working: dict[str, list[dict]] = {}
    for tname, rows in tables_in.items():
        cloned = [dict(r) for r in (rows or [])]
        for s in steps:
            if s.table != tname:
                continue
            if s.kind == "rename":
                for r in cloned:
                    if s.source in r and s.target not in r:
                        r[s.target] = r.pop(s.source)
                    elif s.source in r:
                        r.pop(s.source)
            elif s.kind == "drop":
                for r in cloned:
                    r.pop(s.source, None)
            elif s.kind == "derive" and s.target == "account_status" and tname == "account":
                for r in cloned:
                    if r.get(s.target) in (None, ""):
                        active = r.get(s.source)
                        r[s.target] = "active" if active not in (0, False, "0", "false", "False", None, "") else "inactive"
        working[tname] = cloned

    # Void + cascade
    voided_ids: dict[str, set] = {}
    for tname, rows in working.items():
        info_names = {c["name"] for c in _pragma_table_info(conn, tname)} if tname in target_tables else set()
        if "is_void" not in info_names and tname in target_tables:
            # still honour the field if present in file
            pass
        dropped = []
        kept = []
        vids = set()
        for r in rows:
            if "is_void" in r and _truthy_void(r.get("is_void")):
                dropped.append(r)
                if r.get("id") is not None:
                    vids.add(r["id"])
            else:
                kept.append(r)
        working[tname] = kept
        voided_ids[tname] = vids
        report["tables"].setdefault(tname, {})["in"] = len(tables_in.get(tname) or [])
        report["tables"][tname]["voided"] = len(dropped)

    cascaded = 0
    for parent, children in OWNED_CHILDREN.items():
        pids = voided_ids.get(parent) or set()
        if not pids:
            continue
        fk = f"{parent}_id" if parent != "direct_sale" else "sale_id"
        # booking_item uses booking_id, grn_item grn_id, etc.
        fk_map = {
            "direct_sale": "sale_id",
            "booking": "booking_id",
            "grn": "grn_id",
            "invoice": "invoice_id",
        }
        fk = fk_map[parent]
        for child in children:
            if child not in working:
                continue
            before = len(working[child])
            working[child] = [r for r in working[child] if r.get(fk) not in pids]
            n = before - len(working[child])
            cascaded += n
            report["tables"].setdefault(child, {})["cascaded"] = report["tables"][child].get("cascaded", 0) + n

    # Existing clients
    existing_clients: dict[str, int] = {}
    if "client" in target_tables:
        for row in conn.execute("SELECT id, code, name FROM client"):
            if row[1]:
                existing_clients[str(row[1]).strip().casefold()] = row[0]
            if row[2]:
                existing_clients[str(row[2]).strip().casefold()] = row[0]
        for r in working.get("client") or []:
            if r.get("code"):
                existing_clients[str(r["code"]).strip().casefold()] = r.get("id")
            if r.get("name"):
                existing_clients[str(r["name"]).strip().casefold()] = r.get("id")

    known_client_ids = set()
    for r in working.get("client") or []:
        if r.get("id") is not None:
            known_client_ids.add(r["id"])
    if "client" in target_tables:
        for (cid,) in conn.execute("SELECT id FROM client"):
            known_client_ids.add(cid)

    orphan_map: dict[str, int] = {}
    next_orphan = 1
    max_id = 0
    if "client" in target_tables:
        row = conn.execute("SELECT MAX(id) FROM client").fetchone()
        max_id = int(row[0] or 0)
    for r in working.get("client") or []:
        if isinstance(r.get("id"), int) and r["id"] > max_id:
            max_id = r["id"]

    def ensure_orphan(identity: str) -> int:
        nonlocal next_orphan, max_id
        if identity in orphan_map:
            return orphan_map[identity]
        key = identity.casefold()
        if key in existing_clients:
            return existing_clients[key]
        max_id += 1
        oid = max_id
        name = f"Orphan{next_orphan}"
        next_orphan += 1
        rec = {
            "id": oid,
            "code": name,
            "name": name,
            "category": "Orphan",
            "notes": f"Synthetic client for unknown identity: {identity}",
            "is_active": 1,
        }
        working.setdefault("client", []).append(rec)
        orphan_map[identity] = oid
        existing_clients[key] = oid
        existing_clients[name.casefold()] = oid
        known_client_ids.add(oid)
        report["synthetic_clients"].append({"id": oid, "name": name, "original": identity})
        return oid

    for tname, rows in list(working.items()):
        if tname == "client":
            continue
        info = {c["name"] for c in _pragma_table_info(conn, tname)} if tname in target_tables else set()
        for r in rows:
            ident = _client_identity(r)
            cid = r.get("client_id")
            missing_id = cid not in (None, "", 0) and cid not in known_client_ids
            missing_text = False
            if ident and ident.casefold() not in existing_clients and not str(ident).startswith("id:"):
                missing_text = True
            if missing_id or missing_text:
                oid = ensure_orphan(ident or f"id:{cid}")
                if "client_id" in info or "client_id" in r:
                    r["client_id"] = oid
                note_extra = f"[orphan identity {ident}]"
                if "note" in r or "note" in info:
                    prev = r.get("note") or ""
                    r["note"] = (str(prev) + " " + note_extra).strip()
                elif "notes" in r or "notes" in info:
                    prev = r.get("notes") or ""
                    r["notes"] = (str(prev) + " " + note_extra).strip()

    # Optional FK: blank if missing
    fk_list: list[tuple[str, str, str, str]] = []
    for tname in target_tables:
        for fk in conn.execute(f'PRAGMA foreign_key_list("{tname}")'):
            # id, seq, table, from, to, on_update, on_delete, match
            fk_list.append((tname, fk[3], fk[2], fk[4]))

    loaded_ids: dict[str, set] = {t: {r.get("id") for r in working.get(t) or [] if r.get("id") is not None} for t in working}
    if not dry_run:
        for tname in target_tables:
            for (iid,) in conn.execute(f'SELECT id FROM "{tname}"') if "id" in {c["name"] for c in _pragma_table_info(conn, tname)} else []:
                loaded_ids.setdefault(tname, set()).add(iid)

    for tname, rows in working.items():
        if tname not in target_tables:
            continue
        info = _pragma_table_info(conn, tname)
        notnull = {c["name"]: c["notnull"] for c in info}
        for r in rows:
            for t, col, ref_table, ref_col in fk_list:
                if t != tname or col not in r:
                    continue
                val = r.get(col)
                if is_null(val) or val == "":
                    continue
                present = val in (loaded_ids.get(ref_table) or set())
                if present:
                    continue
                if not notnull.get(col):
                    r[col] = None
                    report.setdefault("blanked_optional_fks", []).append(
                        {"table": tname, "id": r.get("id"), "column": col, "was": val}
                    )
                # required FKs: leave as-is; SQLite will fail the transaction

    if dry_run:
        for tname, rows in working.items():
            report["tables"].setdefault(tname, {})["out"] = len(rows)
        report["ok"] = True
        return report

    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        # Parents first: client, then remaining tables by name.
        order = sorted(working.keys(), key=lambda n: (0 if n == "client" else 1, n))
        for tname in order:
            rows = working[tname]
            if tname not in target_tables:
                continue
            info = _pragma_table_info(conn, tname)
            col_type = {c["name"]: c["type"] for c in info}
            target_cols = set(col_type)
            inserted = 0
            for r in rows:
                payload = {}
                for k, v in r.items():
                    if k not in target_cols:
                        continue
                    try:
                        nv, note = coerce_value(v, col_type[k], column=f"{tname}.{k}")
                    except CoercionError as exc:
                        raise SchemaAbort(str(exc)) from exc
                    if note:
                        report["coercions"].append(note)
                    payload[k] = nv
                if not payload:
                    continue
                cols = list(payload.keys())
                placeholders = ",".join("?" * len(cols))
                assignments = ",".join(f'"{c}"=excluded."{c}"' for c in cols if c != "id")
                sql = (
                    f'INSERT INTO "{tname}" ({",".join(chr(34)+c+chr(34) for c in cols)}) '
                    f"VALUES ({placeholders}) "
                )
                if "id" in payload and assignments:
                    sql += f"ON CONFLICT(id) DO UPDATE SET {assignments}"
                elif "id" in payload:
                    sql += "ON CONFLICT(id) DO NOTHING"
                conn.execute(sql, [payload[c] for c in cols])
                inserted += 1
            rec = report["tables"].setdefault(tname, {})
            rec["out"] = inserted
            rec["synthetic"] = len([c for c in report["synthetic_clients"]]) if tname == "client" else 0
        conn.execute("PRAGMA foreign_keys=ON")
        fk_bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_bad:
            raise SchemaAbort(f"foreign_key_check failed: {fk_bad[:20]!r}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        report["ok"] = False
        report["aborted"] = True
        raise

    for tname, rec in report["tables"].items():
        inn = rec.get("in", 0)
        voided = rec.get("voided", 0)
        casc = rec.get("cascaded", 0)
        syn = rec.get("synthetic", 0) if tname == "client" else 0
        if tname == "client":
            syn = len(report["synthetic_clients"])
            rec["synthetic"] = syn
        rec["arithmetic"] = f"{inn} - {voided} - {casc} + {syn} = {rec.get('out', 0)}"
    return report
