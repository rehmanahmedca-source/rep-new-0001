"""Apply a validated restore plan — one transaction, then verify.

Execution order (policy, docs/DATA_CENTER.md §4):

1. optional safety snapshot (created by the caller via
   ``data_ops.backup_ops.create_data_backup``) before the first write;
2. single ``BEGIN``; FKs disabled during load, re-enabled afterwards;
3. parents first (``client``, then alphabetical); named-column upsert on ``id``;
4. ``PRAGMA foreign_key_check`` — any violation rolls the whole thing back;
5. ``COMMIT`` → :func:`data_ops.verify.verify_database` + before/after counts.

The same code path serves restore (backup folder JSON), UI upload and legacy
merge — one policy, one report shape.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

from data_ops.planner import SchemaAbort, build_plan, target_catalog
from data_ops.portable import load_payload
from data_ops.verify import row_counts, verify_database


def _neutral_value(column: dict) -> Any:
    t = column["type"] or ""
    if "INT" in t or "BOOL" in t:
        return 0
    if any(x in t for x in ("REAL", "FLOA", "DOUB", "NUMER", "DECIM")):
        return 0.0
    return ""


def _upsert_row(
    conn: sqlite3.Connection,
    table: str,
    row: dict,
    col_info: dict[str, dict],
    pk_cols: list[str],
    report: dict,
) -> str:
    """Insert or update one row. Returns 'insert' | 'update' | 'noop'.

    Conflict key = the table's real primary key (all models use ``id``; the
    generic path also covers composite keys).  Tables with no declared PK are
    insert-only and reported as such — re-applying such a file can duplicate
    rows, which is why the archive carries the schema and the plan warns.
    """
    payload = dict(row)
    conflict = [c for c in pk_cols if c in payload]

    exists = False
    if conflict and all(c in payload for c in conflict):
        where = " AND ".join(f'"{c}" = ?' for c in conflict)
        exists = (
            conn.execute(
                f'SELECT 1 FROM "{table}" WHERE {where}',
                [payload[c] for c in conflict],
            ).fetchone()
            is not None
        )

    # Required target columns the file does not supply: fill for NEW rows only.
    # Existing rows keep their live values (no data loss, no silent overwrite).
    if not exists:
        needed = [
            name
            for name, info in col_info.items()
            if name not in payload
            and info["notnull"]
            and info["dflt"] is None
            and name not in conflict
        ]
        for name in needed:
            payload[name] = _neutral_value(col_info[name])
            report.setdefault("filled_missing_at_write", []).append(
                {"table": table, "column": name, "id": payload.get("id"), "value": payload[name]}
            )

    if not payload:
        return "noop"
    if not conflict:
        report.setdefault("tables_without_pk", []).append(table)
    cols = list(payload.keys())
    placeholders = ",".join("?" * len(cols))
    assignments = ",".join(f'"{c}"=excluded."{c}"' for c in cols if c not in conflict)
    sql = f'INSERT INTO "{table}" ({",".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({placeholders}) '
    if conflict and assignments:
        keys = ",".join(chr(34) + c + chr(34) for c in conflict)
        sql += f"ON CONFLICT({keys}) DO UPDATE SET {assignments}"
    elif conflict:
        keys = ",".join(chr(34) + c + chr(34) for c in conflict)
        sql += f"ON CONFLICT({keys}) DO NOTHING"
    conn.execute(sql, [payload[c] for c in cols])
    return "update" if exists else "insert"


def apply_plan(conn: sqlite3.Connection, plan: dict) -> dict:
    """Write the already-validated plan into *conn* inside one transaction."""
    working = plan.get("working") or {}
    before = row_counts(conn)
    report = {k: v for k, v in plan.items() if k not in ("working",)}
    report["dry_run"] = False
    report["before_counts"] = before

    catalog = target_catalog(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    try:
        order = sorted(working.keys(), key=lambda n: (0 if n == "client" else 1, n))
        for tname in order:
            col_info = {c["name"]: c for c in catalog[tname]["columns"]}
            pk_cols = catalog[tname]["pk"]
            ins = up = 0
            for r in working[tname]:
                kind = _upsert_row(conn, tname, r, col_info, pk_cols, report)
                if kind == "insert":
                    ins += 1
                elif kind == "update":
                    up += 1
            plan["tables"].setdefault(tname, {})
            plan["tables"][tname]["inserts"] = ins
            plan["tables"][tname]["updates"] = up
            plan["tables"][tname]["out"] = len(working[tname])
        conn.execute("PRAGMA foreign_keys=ON")
        fk_bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_bad:
            raise SchemaAbort(f"foreign_key_check failed: {fk_bad[:20]!r}")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        report["ok"] = False
        report["aborted"] = True
        raise

    after = row_counts(conn)
    verify = verify_database(conn)
    report["after_counts"] = after
    report["verify"] = verify
    report["ok"] = verify["ok"]
    if not verify["ok"]:
        report["aborted"] = True
        raise SchemaAbort(f"post-restore verification failed: {verify['failures']}")
    return report


def execute_restore(
    conn: sqlite3.Connection,
    source: str | Path | dict,
    *,
    dry_run: bool = False,
    safety_snapshot: Callable[[], dict] | None = None,
) -> dict:
    """Full restore flow: plan (validates) → safety snapshot → apply.

    ``dry_run=True`` returns the plan summary with no writes at all; the same
    validation the real run would do.  ``safety_snapshot`` is a zero-arg
    callable (usually ``create_data_backup`` bound to the live DB) that runs
    before the first write; its result is attached to the report.
    """
    payload = load_payload(source) if isinstance(source, (str, Path)) else source
    plan = build_plan(conn, payload, dry_run=dry_run)
    if dry_run:
        from data_ops.planner import plan_summary

        return plan_summary(plan)

    if safety_snapshot is not None:
        plan["safety_backup"] = safety_snapshot()
    return apply_plan(conn, plan)
