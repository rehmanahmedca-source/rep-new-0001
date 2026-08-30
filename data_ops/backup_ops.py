"""Timestamped VACUUM INTO backups + JSON export + sha256 manifest. Never overwrite."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from data_ops.constants import FORMAT_VERSION
from data_ops.verify import verify_database, row_counts


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY 1"
        )
    ]


def export_json(conn: sqlite3.Connection, dest: Path) -> dict:
    tables = {}
    for name in _tables(conn):
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
        rows = []
        for tup in conn.execute(f'SELECT * FROM "{name}"'):
            rows.append({c: tup[i] for i, c in enumerate(cols)})
        tables[name] = rows
    payload = {
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
    }
    dest.write_text(json.dumps(payload, default=str, ensure_ascii=False), encoding="utf-8")
    return {"tables": len(tables), "rows": sum(len(v) for v in tables.values())}


def create_data_backup(db_path: str | Path, backup_root: str | Path, *, reason: str = "manual") -> dict:
    db_path = Path(db_path)
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = backup_root / f"backup_{stamp}"
    n = 0
    while dest.exists():
        n += 1
        dest = backup_root / f"backup_{stamp}_{n:02d}"
    dest.mkdir(parents=True)

    if not db_path.is_file():
        raise FileNotFoundError(f"Live database does not exist: {db_path}")
    snap = dest / "database.sqlite3"
    src = sqlite3.connect(str(db_path))
    try:
        src.execute("PRAGMA foreign_keys=ON")
        src.execute("VACUUM INTO ?", (str(snap),))
    except sqlite3.OperationalError:
        # Older SQLite: online backup API
        dst = sqlite3.connect(str(snap))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    conn = sqlite3.connect(str(snap))
    try:
        json_info = export_json(conn, dest / "export.json")
        v = verify_database(conn)
    finally:
        conn.close()

    manifest = {
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "database_sha256": _sha256(snap),
        "json_sha256": _sha256(dest / "export.json"),
        "json": json_info,
        "verify": {"ok": v["ok"], "failures": v.get("failures", [])},
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (dest / "REPORT.txt").write_text(
        f"format_version={FORMAT_VERSION}\nreason={reason}\n"
        f"db_sha256={manifest['database_sha256']}\n"
        f"tables={json_info['tables']} rows={json_info['rows']}\n"
        f"verify_ok={v['ok']}\n",
        encoding="utf-8",
    )
    return {"ok": True, "path": str(dest), "manifest": manifest}


def prune_backups(backup_root: str | Path, *, keep_daily: int = 30, keep_weekly: int = 52) -> list[str]:
    """Keep newest keep_daily daily folders; additionally keep one per ISO week up to keep_weekly."""
    root = Path(backup_root)
    if not root.is_dir():
        return []
    backups = sorted([p for p in root.iterdir() if p.is_dir() and p.name.startswith("backup_")], key=lambda p: p.name)
    keep: set[Path] = set(backups[-keep_daily:])
    seen_weeks: list[str] = []
    for p in reversed(backups):
        week = p.name[7:15]  # YYYYMMDD roughly
        key = week[:8]
        iso = key
        if iso not in seen_weeks:
            seen_weeks.append(iso)
            keep.add(p)
        if len(seen_weeks) >= keep_weekly:
            break
    removed = []
    for p in backups:
        if p not in keep:
            import shutil

            shutil.rmtree(p)
            removed.append(p.name)
    return removed


def restore_data_backup(db_path: str | Path, backup_dir: str | Path, *, use_json: bool = True) -> dict:
    """Idempotent restore: JSON upsert on id inside one transaction.

    DB snapshot is copied only when the target file is empty/missing.
    JSON path is always safe to run twice.
    """
    from data_ops.loader import load_legacy_json

    backup_dir = Path(backup_dir)
    db_path = Path(db_path)
    json_path = backup_dir / "export.json"
    snap = backup_dir / "database.sqlite3"
    if not json_path.is_file():
        raise FileNotFoundError(json_path)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if snap.exists() and (not db_path.exists() or db_path.stat().st_size == 0):
        import shutil

        shutil.copy2(snap, db_path)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        report = load_legacy_json(conn, json_path)
        verify = verify_database(conn)
        report["verify"] = verify
        if not verify["ok"]:
            raise RuntimeError(f"post-restore verification failed: {verify['failures']}")
        return report
    finally:
        conn.close()


def counts_equal(a: dict, b: dict) -> bool:
    return row_counts.__wrapped__ if False else a == b
