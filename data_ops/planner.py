"""Restore planner: schema-diff the archive against the live target.

The plan is computed *before* a single write. It decides, per table:

* which file tables map to target tables (or abort),
* which columns are renamed/dropped by declared steps,
* which file columns have no home (abort unless covered),
* which target columns are missing from the file (defaulted / neutral-filled),
* which rows are refused (void), cascaded, or need synthetic orphan clients,
* which FKs can be safely blanked and which FKs would break the transaction,
* insert/update split against the live primary keys,
* which target tables are *untouched* (the "new tables in new versions"
  guarantee).

Everything that can fail is raised here as :class:`SchemaAbort` so no write
ever happens on a plan that cannot be applied cleanly.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from data_ops.coerce import coerce_value, is_null
from data_ops.constants import CLIENT_TEXT_FIELDS, FORMAT_VERSION, OWNED_CHILDREN
from data_ops.portable import normalize_payload, tables_of
from data_ops.steps import covered_unknown_columns, steps_from


class SchemaAbort(RuntimeError):
    """The archive cannot be loaded into this target schema; nothing was written."""


# ---------------------------------------------------------------------------
# target schema introspection
# ---------------------------------------------------------------------------

def target_catalog(conn: sqlite3.Connection) -> dict[str, dict]:
    catalog: dict[str, dict] = {}
    for t in tables_of(conn):
        cols: list[dict] = []
        for r in conn.execute(f'PRAGMA table_info("{t}")'):
            cols.append(
                {"name": r[1], "type": (r[2] or "").upper(), "notnull": bool(r[3]),
                 "dflt": r[4], "pk": bool(r[5])}
            )
        catalog[t] = {
            "columns": cols,
            "by_name": {c["name"]: c for c in cols},
            "pk": [c["name"] for c in cols if c["pk"]],
            "fks": [
                {"from": fk[3], "table": fk[2], "to": fk[4], "on_delete": fk[6]}
                for fk in conn.execute(f'PRAGMA foreign_key_list("{t}")')
            ],
            "existing_ids": set(),
        }
    return catalog


def _version_tuple(v: str) -> tuple:
    parts = str(v).split("-")
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            out.append(p)
    return tuple(out)


def _file_schema_columns(payload_schema: dict, table: str, rows: list[dict]) -> set[str]:
    """Column names the file declares for a table (schema block, else rows)."""
    entry = payload_schema.get(table)
    if isinstance(entry, dict) and isinstance(entry.get("columns"), list):
        return {
            c["name"] if isinstance(c, dict) else str(c)
            for c in entry["columns"]
        }
    cols: set[str] = set()
    for r in rows:
        cols.update(r.keys())
    return cols


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


def _apply_steps_to_row(row: dict, steps: list, table: str) -> dict:
    out = dict(row)
    for s in steps:
        if s.table != table:
            continue
        if s.kind == "rename":
            if s.source in out and s.target not in out:
                out[s.target] = out.pop(s.source)
            elif s.source in out:
                out.pop(s.source)
        elif s.kind == "drop":
            out.pop(s.source, None)
        elif s.kind == "derive" and s.target == "account_status" and table == "account":
            if out.get(s.target) in (None, ""):
                active = out.get(s.source)
                out[s.target] = (
                    "active" if active not in (0, False, "0", "false", "False", None, "") else "inactive"
                )
    return out


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def build_plan(conn: sqlite3.Connection, payload: dict | str, *, dry_run: bool = True) -> dict:
    """Compute (and validate) the full restore plan. Raises SchemaAbort on any
    unknown table/column, bad value, or newer-than-app archive."""
    from data_ops.portable import load_payload

    raw = load_payload(payload) if isinstance(payload, (str,)) else payload
    norm = normalize_payload(raw)
    source_version = norm["format_version"]
    target_version = FORMAT_VERSION

    if _version_tuple(source_version) > _version_tuple(target_version):
        raise SchemaAbort(
            f"archive format_version {source_version} is newer than this app "
            f"({target_version}). Upgrade the app before restoring."
        )

    steps = steps_from(source_version, target_version)
    covered = covered_unknown_columns(steps)
    catalog = target_catalog(conn)
    target_names = set(catalog)
    # Preload existing ids so FK resolution (orphan/blank checks) sees live rows
    # identically in dry-run and apply.  Only tables with a single ``id`` column
    # participate in id-upserts; others are insert-only (same as legacy engine).
    for tname in list(catalog):
        if "id" in catalog[tname]["by_name"]:
            catalog[tname]["existing_ids"] = {
                r[0] for r in conn.execute(f'SELECT id FROM "{tname}"') if r[0] is not None
            }
    file_tables = norm["tables"]
    payload_schema = norm["schema"]

    report: dict[str, Any] = {
        "ok": True,
        "aborted": False,
        "dry_run": bool(dry_run),
        "format_version_in": source_version,
        "format_version_out": target_version,
        "app_version": norm.get("app_version", ""),
        "exported_at": norm.get("exported_at", ""),
        "legacy_flat": norm.get("legacy", False),
        "steps_applied": [s.__dict__ for s in steps],
        "tables": {},
        "coercions": [],
        "synthetic_clients": [],
        "blanked_optional_fks": [],
        "filled_missing": [],
        "untouched_tables": [],
        "summary": {},
    }

    # ---- pass 1: table + column validation (no row inspection) -----------
    unknown: list[str] = []
    missing_in_file: dict[str, list[str]] = {}
    for tname in sorted(file_tables):
        if tname not in target_names:
            if any(s.kind == "drop" and s.table == tname for s in steps):
                report["tables"][tname] = {
                    "in": len(file_tables[tname] or []),
                    "dropped_table": len(file_tables[tname] or []),
                    "out": 0,
                    "column_diff": {"matched": [], "new_in_target": [], "unknown_in_file": []},
                }
                continue
            unknown.append(f"table {tname}")
            continue
        target_cols = set(catalog[tname]["by_name"])
        file_cols = _file_schema_columns(payload_schema, tname, file_tables[tname] or [])
        # columns the steps consume (rename source, drop source) are legal
        leftover = [c for c in sorted(file_cols - target_cols) if (tname, c) not in covered]
        if leftover:
            unknown.extend(f"{tname}.{c}" for c in leftover)
        missing = sorted(target_cols - file_cols)
        if missing:
            missing_in_file[tname] = missing

    if unknown:
        raise SchemaAbort(
            "Unknown columns/tables not covered by a declared migration step: "
            + ", ".join(sorted(unknown))
            + ". Add a rename/drop/split step or remove them from the file."
        )

    # New tables in the target that the file does not know about: untouched.
    report["untouched_tables"] = sorted(target_names - set(file_tables))

    # ---- pass 2: rows (void, cascade, orphans, fk, coercion, counts) ------
    working: dict[str, list[dict]] = {}
    voided_ids: dict[str, set] = {}

    for tname, rows in file_tables.items():
        cloned = [_apply_steps_to_row(dict(r), steps, tname) for r in (rows or [])]
        working[tname] = cloned
        dropped, kept, vids = [], [], set()
        for r in cloned:
            if "is_void" in r and _truthy_void(r.get("is_void")):
                dropped.append(r)
                if r.get("id") is not None:
                    vids.add(r["id"])
            else:
                kept.append(r)
        working[tname] = kept
        voided_ids[tname] = vids
        report["tables"].setdefault(tname, {"in": len(rows or []), "out": 0})
        report["tables"][tname]["in"] = len(rows or [])
        report["tables"][tname]["voided"] = len(dropped)

    # cascade children of voided parents
    cascaded_total = 0
    _FK_CHILD = {
        "direct_sale": "sale_id",
        "booking": "booking_id",
        "grn": "grn_id",
        "invoice": "invoice_id",
    }
    for parent, children in OWNED_CHILDREN.items():
        pids = voided_ids.get(parent) or set()
        if not pids:
            continue
        fk = _FK_CHILD[parent]
        for child in children:
            if child not in working:
                continue
            before = len(working[child])
            working[child] = [r for r in working[child] if r.get(fk) not in pids]
            n = before - len(working[child])
            cascaded_total += n
            report["tables"].setdefault(child, {"in": 0, "out": 0})
            report["tables"][child]["cascaded"] = report["tables"][child].get("cascaded", 0) + n
    report["cascaded_rows"] = cascaded_total

    # existing clients (code/name -> id) for orphan resolution
    existing_clients: dict[str, int] = {}
    if "client" in catalog:
        for row in conn.execute("SELECT id, code, name FROM client"):
            if row[1]:
                existing_clients[str(row[1]).strip().casefold()] = row[0]
            if row[2]:
                existing_clients[str(row[2]).strip().casefold()] = row[0]
    for r in working.get("client") or []:
        if r.get("code"):
            existing_clients.setdefault(str(r["code"]).strip().casefold(), r.get("id"))
        if r.get("name"):
            existing_clients.setdefault(str(r["name"]).strip().casefold(), r.get("id"))

    known_client_ids: set = set()
    if "client" in catalog:
        known_client_ids = {r[0] for r in conn.execute("SELECT id FROM client")}
    for r in working.get("client") or []:
        if r.get("id") is not None:
            known_client_ids.add(r["id"])

    orphan_map: dict[str, int] = {}
    if "client" in catalog:
        max_id = int(conn.execute("SELECT MAX(id) FROM client").fetchone()[0] or 0)
        for r in working.get("client") or []:
            v = r.get("id")
            if isinstance(v, int) and v > max_id:
                max_id = v
    else:
        max_id = 0
    next_orphan = 1

    def ensure_orphan(identity: str) -> int:
        nonlocal max_id, next_orphan
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

    for tname in list(working):
        if tname == "client" or tname not in catalog:
            continue
        col_names = set(catalog[tname]["by_name"])
        for r in working[tname]:
            ident = _client_identity(r)
            cid = r.get("client_id")
            missing_id = cid not in (None, "", 0) and cid not in known_client_ids
            missing_text = bool(
                ident and ident.casefold() not in existing_clients and not str(ident).startswith("id:")
            )
            if missing_id or missing_text:
                oid = ensure_orphan(ident or f"id:{cid}")
                if "client_id" in col_names or "client_id" in r:
                    r["client_id"] = oid
                note_extra = f"[orphan identity {ident}]"
                if "note" in col_names:
                    r["note"] = (str(r.get("note") or "") + " " + note_extra).strip()
                elif "notes" in col_names:
                    r["notes"] = (str(r.get("notes") or "") + " " + note_extra).strip()

    # optional FK blanking + required-FK accounting
    fk_forbidden: list[dict] = []
    for tname in list(working):
        if tname not in catalog:
            continue
        col_by_name = catalog[tname]["by_name"]
        for fk in catalog[tname]["fks"]:
            col = fk["from"]
            for r in working[tname]:
                if col not in r:
                    continue
                val = r.get(col)
                if is_null(val) or val == "":
                    continue
                if val in known_client_ids and fk["table"] == "client":
                    continue
                # general FK presence: file rows + live rows for the ref table
                ref_known = set()
                if fk["table"] in catalog:
                    ref_known |= catalog[fk["table"]]["existing_ids"]
                for rr in working.get(fk["table"]) or []:
                    if rr.get("id") is not None:
                        ref_known.add(rr["id"])
                if val in ref_known:
                    continue
                if not col_by_name[col]["notnull"]:
                    r[col] = None
                    report["blanked_optional_fks"].append(
                        {"table": tname, "id": r.get("id"), "column": col, "was": val}
                    )
                else:
                    fk_forbidden.append(
                        {"table": tname, "id": r.get("id"), "column": col, "value": val}
                    )
    if fk_forbidden:
        raise SchemaAbort(
            "required foreign keys without a target: "
            + ", ".join(f"{x['table']}#{x['id']}.{x['column']}={x['value']}" for x in fk_forbidden[:10])
        )

    # per-row coercion (existing ids already preloaded above)
    inserts = 0
    updates = 0
    for tname in sorted(working):
        if tname not in catalog:
            continue
        col_by_name = catalog[tname]["by_name"]
        out_rows: list[dict] = []
        for r in working[tname]:
            payload_row: dict = {}
            for k, v in r.items():
                if k not in col_by_name:
                    continue
                try:
                    nv, note = coerce_value(v, col_by_name[k]["type"], column=f"{tname}.{k}")
                except Exception as exc:
                    raise SchemaAbort(f"{tname} row {r.get('id')}: {exc}") from exc
                if note:
                    report["coercions"].append(note)
                payload_row[k] = nv
            if not payload_row:
                continue
            rid = payload_row.get("id")
            if rid is None:
                inserts += 1
                report["tables"].setdefault(tname, {})["missing_pk_rows"] = (
                    report["tables"].get(tname, {}).get("missing_pk_rows", 0) + 1
                )
            elif rid in catalog[tname]["existing_ids"]:
                updates += 1
            else:
                inserts += 1
            out_rows.append(payload_row)
        working[tname] = out_rows
        report["tables"].setdefault(tname, {})
        report["tables"][tname]["inserts"] = 0
        report["tables"][tname]["updates"] = 0
        report["tables"][tname]["out"] = len(out_rows)
        report["tables"][tname]["renamed"] = 0
        report["tables"][tname]["column_diff"] = {
            "matched": sorted(set(_file_schema_columns(payload_schema, tname, file_tables.get(tname) or [])) & set(col_by_name)),
            "new_in_target": sorted(set(col_by_name) - _file_schema_columns(payload_schema, tname, file_tables.get(tname) or [])),
            "unknown_in_file": [],
        }

    # recount inserts/updates per table on the coerced set
    for tname in sorted(working):
        if tname not in catalog:
            continue
        ins = up = 0
        for r in working[tname]:
            rid = r.get("id")
            if rid is not None and rid in catalog[tname]["existing_ids"]:
                up += 1
            else:
                ins += 1
        report["tables"][tname]["inserts"] = ins
        report["tables"][tname]["updates"] = up

    # missing target columns -> reported (defaulted at write time)
    for tname, cols in missing_in_file.items():
        if tname not in catalog:
            continue
        for c in cols:
            info = catalog[tname]["by_name"].get(c) or {}
            report["filled_missing"].append(
                {
                    "table": tname,
                    "column": c,
                    "type": info.get("type", ""),
                    "has_default": bool(info.get("dflt") is not None),
                    "notnull": bool(info.get("notnull")),
                    "policy": "database DEFAULT" if info.get("dflt") is not None else (
                        "type-neutral fill" if info.get("notnull") else "NULL"
                    ),
                }
            )

    row_total = sum(len(v) for v in working.values())
    report["summary"] = {
        "file_tables": len(file_tables),
        "target_tables": len(catalog),
        "tables_to_write": len([t for t in working if t in catalog]),
        "untouched_tables": len(report["untouched_tables"]),
        "rows_in_file": sum(len(rows or []) for rows in file_tables.values()),
        "rows_to_write": row_total,
        "inserts": inserts,
        "updates": updates,
        "voided_refused": sum(report["tables"][t].get("voided", 0) for t in report["tables"]),
        "cascaded": cascaded_total,
        "synthetic_clients": len(report["synthetic_clients"]),
        "coercions": len(report["coercions"]),
        "filled_columns": len(report["filled_missing"]),
    }
    report["working"] = working  # internal; stripped before return for dry UI
    return report


def plan_summary(plan: dict) -> dict:
    """Public, JSON-safe summary for the UI/CLI (drops internal row payloads)."""
    out = {k: v for k, v in plan.items() if k not in ("working",)}
    tables_out = {}
    for t, rec in out.get("tables", {}).items():
        tables_out[t] = {k: v for k, v in rec.items() if k != "rows"}
    out["tables"] = tables_out
    return out
