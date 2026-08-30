#!/usr/bin/env python3
"""CLI for the schema-aware data layer (Data Center engine).

Examples (copy-paste):

    # Backup (VACUUM INTO + schema-aware JSON + manifest)
    python tools/data_ops.py backup

    # Export each format
    python tools/data_ops.py export-json   /tmp/ams.json
    python tools/data_ops.py export-db     /tmp/ams.sqlite3
    python tools/data_ops.py export-xlsx   /tmp/ams.xlsx

    # Restore: always plan first (dry run), then apply
    python tools/data_ops.py plan          /tmp/old.json
    python tools/data_ops.py restore-json  /tmp/old.json            # safety backup first
    python tools/data_ops.py restore-json  /tmp/old.json --dry-run

    # Legacy merge + legacy .db conversion
    python tools/data_ops.py import-json   /tmp/legacy.json
    python tools/data_ops.py convert-db    legacy.db /tmp/legacy.json

    # Disaster snapshot restore (offline, explicit)
    python tools/data_ops.py restore-db    backup_x/database.sqlite3 --yes

    # Verify + restore-twice idempotency proof
    python tools/data_ops.py verify
    python tools/data_ops.py restore-twice instance/storage/data_backups/backup_YYYYMMDD_HHMMSS
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("AMS_SKIP_UPDATE_PIPELINE", "1")


def _db_path() -> Path:
    env = os.environ.get("APP_DB_PATH")
    if env:
        return Path(env)
    return ROOT / "instance" / "ahmed_cement_v44_fresh.db"


def _backup_root() -> Path:
    return Path(os.environ.get("AMS_DATA_BACKUP_DIR") or (ROOT / "instance" / "storage" / "data_backups"))


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _safety_snapshot(db: Path):
    from data_ops.backup_ops import create_data_backup

    return create_data_backup(db, _backup_root(), reason="pre-restore")


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="AMS data-layer backup/restore/merge engine")
    p.add_argument("--db", default=None, help="Target database (default: APP_DB_PATH or live file)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("backup")
    sub.add_parser("verify")
    r = sub.add_parser("restore")
    r.add_argument("backup_dir")
    t = sub.add_parser("restore-twice")
    t.add_argument("backup_dir")
    i = sub.add_parser("import-json")
    i.add_argument("json_path")
    i.add_argument("--dry-run", action="store_true")
    e = sub.add_parser("export-json")
    e.add_argument("out_path")
    e.add_argument("--tables", default="", help="comma-separated table names (default: all)")
    ed = sub.add_parser("export-db")
    ed.add_argument("out_path")
    ex = sub.add_parser("export-xlsx")
    ex.add_argument("out_path")
    ex.add_argument("--tables", default="", help="comma-separated table names (default: all)")
    pl = sub.add_parser("plan")
    pl.add_argument("json_path")
    rj = sub.add_parser("restore-json")
    rj.add_argument("json_path")
    rj.add_argument("--dry-run", action="store_true")
    rj.add_argument("--no-safety-backup", action="store_true")
    rd = sub.add_parser("restore-db")
    rd.add_argument("snapshot_path")
    rd.add_argument("--yes", action="store_true", help="confirm destructive replace")
    cd = sub.add_parser("convert-db")
    cd.add_argument("src_db")
    cd.add_argument("out_json")
    tbls = sub.add_parser("tables")
    tbls.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    db = Path(args.db) if getattr(args, "db", None) else _db_path()

    if args.cmd == "backup":
        from data_ops.backup_ops import create_data_backup, prune_backups

        result = create_data_backup(db, _backup_root(), reason="cli")
        pruned = prune_backups(_backup_root())
        _print({"backup": result["path"], "pruned": pruned, "manifest": result["manifest"]})
        return 0

    if args.cmd == "verify":
        from data_ops.verify import verify_database

        conn = _connect(db)
        try:
            v = verify_database(conn)
        finally:
            conn.close()
        _print(v)
        return 0 if v.get("ok") else 1

    if args.cmd == "restore":
        from data_ops.backup_ops import restore_data_backup

        report = restore_data_backup(db, args.backup_dir)
        _print(report)
        return 0 if report.get("ok") else 1

    if args.cmd == "restore-twice":
        from data_ops.backup_ops import restore_data_backup
        from data_ops.verify import row_counts

        restore_data_backup(db, args.backup_dir)
        c1 = row_counts(_connect(db))
        restore_data_backup(db, args.backup_dir)
        c2 = row_counts(_connect(db))
        _print({"first": c1, "second": c2, "identical": c1 == c2})
        return 0 if c1 == c2 else 1

    if args.cmd in ("import-json", "restore-json"):
        from data_ops.engine import execute_restore

        conn = _connect(db)
        try:
            report = execute_restore(
                conn,
                args.json_path,
                dry_run=bool(getattr(args, "dry_run", False)),
                safety_snapshot=None
                if getattr(args, "dry_run", False) or getattr(args, "no_safety_backup", False)
                else lambda: _safety_snapshot(db),
            )
        finally:
            conn.close()
        _print(report)
        return 0 if report.get("ok") else 1

    if args.cmd == "plan":
        from data_ops.engine import execute_restore

        conn = _connect(db)
        try:
            report = execute_restore(conn, args.json_path, dry_run=True)
        finally:
            conn.close()
        _print(report)
        return 0 if report.get("ok") else 1

    if args.cmd == "export-json":
        from data_ops.portable import export_json

        conn = sqlite3.connect(str(db))
        try:
            names = [x.strip() for x in args.tables.split(",") if x.strip()] or None
            info = export_json(conn, Path(args.out_path), tables=names, app_version="cli")
        finally:
            conn.close()
        _print(info)
        return 0

    if args.cmd == "export-db":
        from data_ops.backup_ops import create_data_backup

        out = Path(args.out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(str(db))
        try:
            src.execute("VACUUM INTO ?", (str(out),))
        except sqlite3.OperationalError:
            dst = sqlite3.connect(str(out))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        _print({"path": str(out), "tables": _count_tables(out)})
        return 0

    if args.cmd == "export-xlsx":
        from data_ops.xlsx_export import export_xlsx

        conn = sqlite3.connect(str(db))
        try:
            names = [x.strip() for x in args.tables.split(",") if x.strip()] or None
            info = export_xlsx(conn, Path(args.out_path), tables=names)
        finally:
            conn.close()
        _print(info)
        return 0

    if args.cmd == "restore-db":
        if not args.yes:
            print("REFUSED: restore-db replaces the live database. Re-run with --yes.", file=sys.stderr)
            return 2
        snap = Path(args.snapshot_path)
        if not snap.is_file():
            print(f"snapshot not found: {snap}", file=sys.stderr)
            return 2
        sconn = sqlite3.connect(str(snap))
        try:
            ok = sconn.execute("PRAGMA integrity_check").fetchone()[0]
            fk = sconn.execute("PRAGMA foreign_key_check").fetchall()
            if ok != "ok" or fk:
                print(f"snapshot failed checks: integrity={ok}, fk={fk[:5]!r}", file=sys.stderr)
                return 1
        finally:
            sconn.close()
        safety = _safety_snapshot(db)
        for suffix in ("-wal", "-shm", "-journal"):
            side = Path(str(db) + suffix)
            if side.exists():
                side.unlink()
        shutil.copy2(snap, db)
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            from data_ops.verify import verify_database

            v = verify_database(conn)
        finally:
            conn.close()
        _print({"replaced": str(db), "from": str(snap), "safety": safety["path"], "verify": v})
        return 0 if v.get("ok") else 1

    if args.cmd == "convert-db":
        from data_ops.portable import export_json

        src = sqlite3.connect(str(args.src_db))
        try:
            info = export_json(src, Path(args.out_json), app_version="legacy-convert", db_name=Path(args.src_db).name)
        finally:
            src.close()
        _print(info)
        return 0

    if args.cmd == "tables":
        from data_ops.verify import row_counts

        conn = sqlite3.connect(str(db))
        try:
            counts = row_counts(conn)
        finally:
            conn.close()
        if args.json:
            _print(counts)
        else:
            for name, n in sorted(counts.items()):
                print(f"{name}\t{n}")
        return 0

    return 2


def _count_tables(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
