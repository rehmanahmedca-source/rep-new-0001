"""Data Center service: uploads, plans, apply, run history, server backups.

One code path for restore and legacy merge.  The engine has already been
proven by ``tests/test_data_ops.py``; this layer is only about UX safety:

* uploads are stored in ``instance/.tmp/import_uploads/`` (gitignored),
  referenced by a random token held in the session;
* a plan is computed against the LIVE schema before any write;
* apply repeats the plan fresh (the DB may have changed between plan and
  apply), records the run, then redirects to a result page (PRG — a refresh
  never re-applies);
* every apply runs a ``VACUUM INTO`` safety backup first.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path

from flask import current_app

from data_ops.backup_ops import create_data_backup
from data_ops.engine import execute_restore
from data_ops.portable import load_payload
from data_ops.planner import SchemaAbort
from models import DataTransferRun, db, pk_model_now

LOG = logging.getLogger(__name__)

UPLOAD_TTL_SECONDS = 24 * 3600


def _storage() -> Path:
    return Path(current_app.config["IMPORT_UPLOADS_DIR"] or (Path(current_app.root_path).parent / "instance" / ".tmp" / "import_uploads"))


def _backup_root() -> Path:
    return Path(
        current_app.config.get("AMS_DATA_BACKUP_DIR")
        or (Path(current_app.root_path).parent / "instance" / "storage" / "data_backups")
    )


def _db_path() -> Path:
    return Path(current_app.config["APP_DB_PATH"])


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _actor() -> str:
    from flask_login import current_user

    return getattr(current_user, "username", "") or "unknown"


def _cleanup_old_uploads(now: float | None = None) -> None:
    now = now or time.time()
    root = _storage()
    if not root.is_dir():
        return
    for f in root.iterdir():
        try:
            if f.is_file() and now - f.stat().st_mtime > UPLOAD_TTL_SECONDS:
                f.unlink()
        except OSError:
            pass


def save_upload(file_storage, *, kind: str, max_bytes: int | None = None) -> dict:
    """Store an uploaded archive; return {token, path, sha256, filename, size, payload}."""
    _cleanup_old_uploads()
    root = _storage()
    root.mkdir(parents=True, exist_ok=True)
    filename = file_storage.filename or "archive.json"
    raw = file_storage.read()
    if max_bytes and len(raw) > max_bytes:
        raise ValueError(f"file too large ({len(raw)} bytes > {max_bytes})")
    token = uuid.uuid4().hex
    dest = root / f"{kind}_{token}_{Path(filename).name[:80]}"
    dest.write_bytes(raw)
    # validate it is a real archive right now
    payload = load_payload(dest)
    return {
        "token": token,
        "path": str(dest),
        "sha256": _sha256_bytes(raw),
        "filename": filename,
        "size": len(raw),
        "payload": payload,
    }


def plan_for(token: str, *, path_override: str | None = None) -> tuple[dict, dict]:
    """Dry-run plan against the live schema. Returns (plan_summary, upload_meta)."""
    _cleanup_old_uploads()
    meta_path = _upload_path(token)
    path = path_override or meta_path
    conn = sqlite3.connect(str(_db_path()))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        plan = execute_restore(conn, path, dry_run=True)
    finally:
        conn.close()
    upload = {
        "token": token,
        "path": str(path),
        "filename": Path(path).name,
        "sha256": _sha256_bytes(Path(path).read_bytes()) if Path(path).is_file() else "",
    }
    return plan, upload


def _upload_path(token: str) -> str:
    if not token:
        return ""
    root = _storage()
    for f in root.glob(f"*_{token}_*"):
        return str(f)
    return ""


def apply_upload(token: str, *, kind: str, path_override: str | None = None) -> dict:
    """Fresh plan + safety backup + apply; record the run. Returns final report."""
    _cleanup_old_uploads()
    path = path_override or _upload_path(token)
    if not Path(path).is_file():
        raise FileNotFoundError("uploaded archive no longer exists (it may have expired)")
    payload = load_payload(path)
    filename = Path(path).name

    def _safety():
        return create_data_backup(
            _db_path(),
            _backup_root(),
            reason=f"pre-{kind}",
            app_version=str(current_app.config.get("APP_VERSION") or "v44"),
        )

    # release SQLAlchemy connections so the raw sqlite3 writer is not blocked
    db.session.remove()
    conn = sqlite3.connect(str(_db_path()))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        report = execute_restore(conn, payload, safety_snapshot=_safety)
    except SchemaAbort:
        raise
    except Exception:
        LOG.exception("data-center apply failed")
        raise
    finally:
        conn.close()
    db.session.remove()

    summary = {
        k: v
        for k, v in report.items()
        if k not in ("coercions", "synthetic_clients", "filled_missing", "blanked_optional_fks")
    }
    # keep per-table JSON small for the DB row
    for rec in (summary.get("tables") or {}).values():
        for key in ("column_diff", "missing_pk_rows"):
            rec.pop(key, None)
    run = DataTransferRun(
        kind=kind,
        format="json",
        filename=filename,
        file_sha256=_sha256_bytes(Path(path).read_bytes()),
        format_version_in=report.get("format_version_in"),
        format_version_out=report.get("format_version_out"),
        status="ok" if report.get("ok") else "error",
        tables=len(report.get("tables", {})),
        rows=int((report.get("summary") or {}).get("rows_to_write", 0)),
        summary_json=summary,
        actor=_actor(),
    )
    db.session.add(run)
    db.session.commit()
    report["run_id"] = run.id
    return report


def record_failed(kind: str, filename: str, error: str) -> None:
    try:
        run = DataTransferRun(
            kind=kind,
            format="json",
            filename=filename,
            status="error",
            summary_json={"error": str(error)[:2000]},
            actor=_actor(),
        )
        db.session.add(run)
        db.session.commit()
    except Exception:
        db.session.rollback()
        LOG.exception("failed to record data-center run")


def recent_runs(limit: int = 25) -> list[DataTransferRun]:
    return (
        DataTransferRun.query.order_by(DataTransferRun.created_at.desc())
        .limit(limit)
        .all()
    )


def run_by_id(run_id: int) -> DataTransferRun | None:
    return DataTransferRun.query.get(run_id)


def server_backups() -> list[dict]:
    """Folders that contain an export.json / database.sqlite3 on this server."""
    out: list[dict] = []
    root = _backup_root()
    if not root.is_dir():
        return out
    for folder in sorted(root.glob("backup_*"), reverse=True):
        meta = {}
        manifest = folder / "manifest.json"
        if manifest.is_file():
            try:
                meta = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        out.append(
            {
                "folder": folder.name,
                "path": str(folder),
                "has_json": (folder / "export.json").is_file(),
                "has_db": (folder / "database.sqlite3").is_file(),
                "created_at": meta.get("created_at", ""),
                "reason": meta.get("reason", ""),
                "rows": (meta.get("json") or {}).get("rows", 0),
                "tables": (meta.get("json") or {}).get("tables", 0),
            }
        )
    return out[:40]


def record_export(format_name: str, filename: str, stats: dict) -> None:
    try:
        run = DataTransferRun(
            kind="export",
            format=format_name,
            filename=filename,
            file_sha256=stats.get("sha256", ""),
            status="ok",
            tables=int(stats.get("tables", 0)),
            rows=int(stats.get("rows", 0)),
            summary_json={"path": stats.get("path", "")},
            actor=_actor(),
        )
        db.session.add(run)
        db.session.commit()
    except Exception:
        db.session.rollback()
        LOG.exception("failed to record export")


def export_archive(*, format_name: str, tables: list[str] | None = None) -> tuple[Path, str, dict]:
    """Generate an export file; returns (path, download_name, stats)."""
    from data_ops.portable import export_json
    from data_ops.xlsx_export import export_xlsx

    conn = sqlite3.connect(str(_db_path()))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        if format_name == "db":
            out = _storage() / f"ams_data_{int(time.time())}.sqlite3"
            conn.execute("VACUUM INTO ?", (str(out),))
            stats = _file_stats(out)
            return out, out.name, stats
        if format_name == "xlsx":
            out = _storage() / f"ams_export_{int(time.time())}.xlsx"
            info = export_xlsx(conn, out, tables=tables)
            info["sha256"] = _sha256_file(out)
            return out, out.name, info
        out = _storage() / f"ams_export_{int(time.time())}.json"
        stats = export_json(
            conn,
            out,
            tables=tables,
            app_version=str(current_app.config.get("APP_VERSION") or "v44"),
            db_name=Path(_db_path()).name,
        )
        return out, out.name, stats
    finally:
        conn.close()


def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_stats(path: Path) -> dict:
    conn = sqlite3.connect(str(path))
    try:
        from data_ops.verify import row_counts

        counts = row_counts(conn)
    finally:
        conn.close()
    return {"path": str(path), "tables": len(counts), "rows": sum(counts.values()), "sha256": _sha256_file(path)}
