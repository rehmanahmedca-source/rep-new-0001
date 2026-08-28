#!/usr/bin/env python3
"""CLI for versioned backup / restore / legacy JSON import.

Examples (copy-paste):

    python tools/data_ops.py backup
    python tools/data_ops.py restore instance/storage/data_backups/backup_YYYYMMDD_HHMMSS
    python tools/data_ops.py restore-twice instance/storage/data_backups/backup_YYYYMMDD_HHMMSS
    python tools/data_ops.py import-json path/to/legacy.json
    python tools/data_ops.py verify
"""
from __future__ import annotations

import argparse
import json
import os
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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="AMS data-layer backup/restore/import")
    p.add_argument("--db", default=None, help="Target database (default: APP_DB_PATH or live file)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("backup")
    r = sub.add_parser("restore")
    r.add_argument("backup_dir")
    t = sub.add_parser("restore-twice")
    t.add_argument("backup_dir")
    i = sub.add_parser("import-json")
    i.add_argument("json_path")
    i.add_argument("--dry-run", action="store_true")
    sub.add_parser("verify")
    e = sub.add_parser("export-json")
    e.add_argument("out_path")

    args = p.parse_args(argv)
    db = Path(args.db) if getattr(args, "db", None) else _db_path()

    if args.cmd == "backup":
        from data_ops.backup_ops import create_data_backup, prune_backups

        result = create_data_backup(db, _backup_root(), reason="cli")
        pruned = prune_backups(_backup_root())
        print(json.dumps({"backup": result["path"], "pruned": pruned, "manifest": result["manifest"]}, indent=2, default=str))
        return 0

    if args.cmd == "restore":
        from data_ops.backup_ops import restore_data_backup

        report = restore_data_backup(db, args.backup_dir)
        print(json.dumps(report, indent=2, default=str))
        return 0 if report.get("ok") else 1

    if args.cmd == "restore-twice":
        from data_ops.backup_ops import restore_data_backup
        from data_ops.verify import row_counts

        restore_data_backup(db, args.backup_dir)
        c1 = row_counts(sqlite3.connect(str(db)))
        restore_data_backup(db, args.backup_dir)
        c2 = row_counts(sqlite3.connect(str(db)))
        print(json.dumps({"first": c1, "second": c2, "identical": c1 == c2}, indent=2))
        return 0 if c1 == c2 else 1

    if args.cmd == "import-json":
        from data_ops.loader import load_legacy_json

        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            report = load_legacy_json(conn, args.json_path, dry_run=args.dry_run)
        finally:
            conn.close()
        print(json.dumps(report, indent=2, default=str))
        return 0 if report.get("ok") else 1

    if args.cmd == "verify":
        from data_ops.verify import verify_database

        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            v = verify_database(conn)
        finally:
            conn.close()
        print(json.dumps(v, indent=2, default=str))
        return 0 if v.get("ok") else 1

    if args.cmd == "export-json":
        from data_ops.backup_ops import export_json

        conn = sqlite3.connect(str(db))
        try:
            info = export_json(conn, Path(args.out_path))
        finally:
            conn.close()
        print(json.dumps(info, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
