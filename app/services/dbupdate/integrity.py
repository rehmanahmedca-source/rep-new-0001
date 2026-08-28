"""Post-update data integrity: the ERP's own rules, not generic formulas.

Three layers, cheapest first, all read-only:

1. **SQLite itself** — ``PRAGMA integrity_check`` + ``foreign_key_check``.
2. **Transaction blockers** — ``tools/health/preflight_check.py`` (dangling
   booking-allocation FKs, bill-counter collisions, duplicate client codes,
   ledger/material drift).  Already tuned to this schema, so it is *called*, not
   re-invented.
3. **Business totals** — ``tools/consistency_report.py`` (account balances vs
   the ledger, material totals vs movements, orphaned payments/invoices,
   sales vs pending bills, booking allocations).

Plus a row-count guard: if any table holds *fewer* rows after an update than
before it, that is reported as potential data loss and the update is not
marked successful — even when every individual statement "succeeded".
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from sqlalchemy import func, text

LOG = logging.getLogger("ams.dbupdate.integrity")

_TOOLS_DIR = Path(__file__).resolve().parents[3] / "tools"


def _load_tool(module_name: str, relative: str):
    path = _TOOLS_DIR / relative
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        LOG.warning("integrity tool '%s' could not be loaded", relative, exc_info=True)
        return None
    return module


def snapshot_counts(bind=None) -> dict[str, int]:
    """Row count of every table the models declare (plus the ledger tables)."""
    from models import db

    engine = bind or db.engine
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            )
        }
        for name in sorted(names):
            try:
                counts[name] = int(connection.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar() or 0)
            except Exception:
                counts[name] = -1
    return counts


def compare_counts(before: dict[str, int], after: dict[str, int], *, allowed_shrink: set[str] | None = None) -> dict:
    """Any table that lost rows is a data-loss signal, unless a migration said so."""
    allowed = set(allowed_shrink or ())
    losses: list[dict] = []
    gained: list[str] = []
    for table, count in before.items():
        new_count = after.get(table, count)
        if count < 0 or new_count < 0:
            continue
        if new_count < count:
            entry = {"table": table, "before": count, "after": new_count, "missing": count - new_count}
            if table in allowed:
                entry["declared_by_migration"] = True
            else:
                losses.append(entry)
        elif new_count > count:
            gained.append(table)
    return {
        "status": "FAIL" if losses else "PASS",
        "tables_checked": len(before),
        "row_losses": losses,
        "tables_gained_rows": sorted(gained),
    }


def sqlite_level_checks(bind=None) -> dict:
    from models import db

    engine = bind or db.engine
    detail: dict = {"status": "PASS", "integrity_check": "", "foreign_key_violations": 0, "violations": []}
    with engine.connect() as connection:
        try:
            row = connection.execute(text("PRAGMA integrity_check")).fetchone()
            detail["integrity_check"] = str(row[0]) if row else "no result"
            if detail["integrity_check"].lower() != "ok":
                detail["status"] = "FAIL"
        except Exception as exc:
            detail["status"] = "FAIL"
            detail["integrity_check"] = f"error: {exc}"
        try:
            violations = connection.execute(text("PRAGMA foreign_key_check")).fetchall()
            detail["foreign_key_violations"] = len(violations)
            detail["violations"] = [
                {"table": v[0], "rowid": v[1], "parent": v[2], "fk_id": v[3]} for v in violations[:50]
            ]
            if violations and detail["status"] == "PASS":
                detail["status"] = "FAIL"
                detail["detail"] = f"{len(violations)} foreign-key violation(s) after the update"
        except Exception as exc:
            detail["foreign_key_check_error"] = str(exc)
    return detail


def preflight(bind=None) -> dict:
    """Run the existing transaction-blocker watch (read-only)."""
    tool = _load_tool("ams_preflight_check", "health/preflight_check.py")
    if tool is None:
        return {"status": "SKIPPED", "detail": "tools/health/preflight_check.py unavailable"}
    from models import db

    engine = bind or db.engine
    db_file = engine.url.database
    if not db_file or not Path(db_file).is_file():
        return {"status": "SKIPPED", "detail": "not a file-backed SQLite database"}
    connection = None
    try:
        connection = tool.connect(Path(db_file))
        payload = tool.run_checks(connection)
    except Exception as exc:
        LOG.warning("preflight check failed to run", exc_info=True)
        return {"status": "ERROR", "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    blocks = list(payload.get("blocks") or [])
    watch = list(payload.get("watch") or [])
    return {
        "status": "FAIL" if blocks else "PASS",
        "blocks": blocks[:20],
        "watch": watch[:20],
        "detail": f"{len(blocks)} blocker(s), {len(watch)} watch item(s)",
    }


def consistency(bind=None) -> dict:
    """Run the existing financial/inventory consistency suite (read-only)."""
    tool = _load_tool("ams_consistency_report", "consistency_report.py")
    if tool is None:
        return {"status": "SKIPPED", "detail": "tools/consistency_report.py unavailable"}
    from models import db

    engine = bind or db.engine
    db_file = engine.url.database
    if not db_file:
        return {"status": "SKIPPED", "detail": "not a file-backed SQLite database"}
    try:
        tool.DB_PATH = Path(db_file)
        report = tool.run_all_checks()
    except Exception as exc:
        LOG.warning("consistency report failed to run", exc_info=True)
        return {"status": "ERROR", "detail": f"{type(exc).__name__}: {exc}"}

    def brief(check: dict) -> str:
        if check.get("message"):
            return str(check["message"])
        for key, value in check.items():
            if isinstance(value, list) and value and key not in ("checks",):
                return f"{len(value)} {key.replace('_', ' ')}"
        return ""

    def labelled(check: dict) -> dict:
        return {
            "check": check.get("check") or check.get("name") or "unknown",
            "status": check.get("status"),
            "summary": brief(check),
        }

    failing = [labelled(c) for c in report.get("checks", []) if c.get("status") == "FAIL"]
    warnings = [labelled(c) for c in report.get("checks", []) if c.get("status") == "WARN"]
    return {
        "status": "FAIL" if failing else ("WARN" if warnings else "PASS"),
        "overall": report.get("overall_status"),
        "checks": report.get("total_checks"),
        "failing": failing,
        "warnings": warnings,
        "detail": f"{len(failing)} failing, {len(warnings)} warning of {report.get('total_checks', 0)} check(s)",
    }


def run_integrity(app=None, *, bind=None, deep: bool = True) -> dict:
    """All layers, one verdict.  ``deep=False`` skips the slow business suites."""
    from models import db

    engine = bind or (db.engine if app is not None else None)
    layers = {"sqlite": sqlite_level_checks(engine), "row_counts": {"status": "PASS", "detail": "no baseline supplied"}}
    if deep:
        layers["preflight"] = preflight(engine)
        layers["consistency"] = consistency(engine)
    failing = [name for name, payload in layers.items() if isinstance(payload, dict) and payload.get("status") == "FAIL"]
    errored = [name for name, payload in layers.items() if isinstance(payload, dict) and payload.get("status") == "ERROR"]
    status = "FAIL" if failing else ("ERROR" if errored else "PASS")
    return {
        "status": status,
        "failed_layers": failing,
        "error_layers": errored,
        "deep": deep,
        "layers": layers,
    }
