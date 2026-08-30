"""Data Center: disaster snapshot restore (exact database replace).

JSON restore powers the daily merge/upgrade path; this page exists for one
thing only: rebuild a lost/empty/corrupt database from an exact *.db* file.
It requires typed confirmation, runs an integrity check on the upload, makes
an automatic safety snapshot of the current file, and verifies afterwards.
"""
from __future__ import annotations

import shutil
import sqlite3
import uuid
from pathlib import Path

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required

from ._common import *  # noqa
from ._dc_common import dc_guard


@import_export_bp.route("/restore/db")
@login_required
def dc_db_restore_page():
    dc_guard()
    return render_template("data_center_db_restore.html", dc_active="restore")


@import_export_bp.route("/restore/db", methods=["POST"])
@login_required
def dc_db_restore_apply():
    dc_guard()
    from app.services.data_center_service import (
        _backup_root,
        _db_path,
        _storage,
        _actor,
        record_failed,
    )
    from data_ops.backup_ops import create_data_backup
    from data_ops.verify import verify_database

    if str(request.form.get("confirm") or "").strip() != "RESTORE":
        flash("Type RESTORE to confirm replacing the database.", "warning")
        return redirect(url_for("import_export.dc_db_restore_page"))

    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a .sqlite3/.db snapshot exported by AMS.", "danger")
        return redirect(url_for("import_export.dc_db_restore_page"))

    filename = Path(file.filename).name
    dest = _storage() / f"snap_{uuid.uuid4().hex}_{filename[:80]}"
    dest.write_bytes(file.read())

    # 1. the snapshot itself must be healthy before anything is touched
    try:
        conn = sqlite3.connect(str(dest))
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.close()
    except sqlite3.Error as exc:
        flash(f"Uploaded snapshot is not a valid SQLite file: {exc}", "danger")
        return redirect(url_for("import_export.dc_db_restore_page"))
    if integrity != "ok" or fk_bad:
        flash(
            f"Snapshot failed pre-checks (integrity={integrity}, fk_violations={len(fk_bad)}). Nothing was changed.",
            "danger",
        )
        return redirect(url_for("import_export.dc_db_restore_page"))

    # 2. safety snapshot of the CURRENT live file
    try:
        safety = create_data_backup(
            _db_path(),
            _backup_root(),
            reason="pre-db-restore",
            app_version=str(current_app.config.get("APP_VERSION") or "v44"),
        )
    except Exception as exc:
        record_failed("snapshot", filename, str(exc))
        flash(f"Could not create the safety backup — refusing to replace: {exc}", "danger")
        return redirect(url_for("import_export.dc_db_restore_page"))

    # 3. replace (sidecar cleanup; offline admin operation)
    try:
        for suffix in ("-wal", "-shm", "-journal"):
            side = Path(str(_db_path()) + suffix)
            if side.exists():
                side.unlink()
        shutil.copy2(dest, _db_path())
    except OSError as exc:
        record_failed("snapshot", filename, str(exc))
        flash(f"Replace failed — your database is untouched, safety backup at {safety['path']}: {exc}", "danger")
        return redirect(url_for("import_export.dc_db_restore_page"))

    # 4. verify
    conn = sqlite3.connect(str(_db_path()))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        v = verify_database(conn)
    finally:
        conn.close()

    from models import DataTransferRun, db

    run = DataTransferRun(
        kind="snapshot",
        format="db",
        filename=filename,
        status="ok" if v.get("ok") else "error",
        tables=len(v.get("row_counts", {})),
        rows=sum(v.get("row_counts", {}).values()),
        summary_json={"verify": v, "safety_backup": safety["path"], "pre_existing_database_replaced": True},
        actor=_actor(),
    )
    db.session.add(run)
    db.session.commit()

    if not v.get("ok"):
        flash(
            f"POST-REPLACE VERIFY FAILED: {v['failures']} — restore the safety backup at {safety['path']} immediately.",
            "danger",
        )
    else:
        flash(f"Database replaced from snapshot and verified. Safety backup: {safety['path']}", "success")
    return redirect(url_for("import_export.dc_restore_result", run_id=run.id))
