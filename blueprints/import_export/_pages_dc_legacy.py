"""Data Center: legacy data merge (JSON only transport; legacy DB convertible).

Rules (same engine as restore, docs/DATA_CENTER.md §6):
* ids kept as-is; conflicts upsert (never duplicate);
* unknown clients become ``OrphanN`` records — never skipped;
* void rows refused, cascaded children removed, unknown columns abort;
* every run shows the diff plan first; nothing is written without APPLY.
"""
from __future__ import annotations

from pathlib import Path

from flask import flash, redirect, render_template, request, session, send_file, url_for
from flask_login import login_required

from ._common import *  # noqa
from ._dc_common import dc_guard

PLAN_KEY = "dc_plan"


@import_export_bp.route("/legacy")
@login_required
def dc_legacy_page():
    dc_guard()
    from app.services.data_center_service import recent_runs

    return render_template(
        "data_center_legacy.html",
        runs=[r for r in recent_runs(30) if r.kind == "legacy"],
        dc_active="legacy",
    )


@import_export_bp.route("/legacy/plan", methods=["POST"])
@login_required
def dc_legacy_plan():
    dc_guard()
    from app.services.data_center_service import (
        plan_for,
        record_failed,
        save_upload,
    )

    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a JSON file (AMS export or legacy merge payload).", "danger")
        return redirect(url_for("import_export.dc_legacy_page"))
    try:
        saved = save_upload(file, kind="legacy")
        plan, upload = plan_for(saved["token"])
    except Exception as exc:
        record_failed("legacy", file.filename or "?", str(exc))
        flash(f"Could not plan this legacy file: {exc}", "danger")
        return redirect(url_for("import_export.dc_legacy_page"))

    session[PLAN_KEY] = {
        "token": saved["token"],
        "kind": "legacy",
        "server_folder": "",
        "filename": saved["filename"],
        "sha256": saved["sha256"],
    }
    return render_template(
        "data_center_plan.html",
        plan=plan,
        upload=upload,
        action=url_for("import_export.dc_legacy_apply"),
        kind="legacy",
        back=url_for("import_export.dc_legacy_page"),
        dc_active="legacy",
    )


@import_export_bp.route("/legacy/apply", methods=["POST"])
@login_required
def dc_legacy_apply():
    dc_guard()
    from app.services.data_center_service import apply_upload, record_failed

    plan_meta = session.get(PLAN_KEY) or {}
    if not plan_meta.get("token") or plan_meta.get("kind") != "legacy":
        flash("No pending legacy plan. Upload a file and review the plan first.", "danger")
        return redirect(url_for("import_export.dc_legacy_page"))
    if str(request.form.get("confirm") or "").strip() != "APPLY":
        flash("Type APPLY to confirm the merge.", "warning")
        return redirect(url_for("import_export.dc_legacy_page"))
    try:
        report = apply_upload(plan_meta["token"], kind="legacy")
    except Exception as exc:
        record_failed("legacy", plan_meta.get("filename", "?"), str(exc))
        flash(f"Merge was rolled back (nothing changed): {exc}", "danger")
        session.pop(PLAN_KEY, None)
        return redirect(url_for("import_export.dc_legacy_page"))
    session.pop(PLAN_KEY, None)
    return redirect(url_for("import_export.dc_restore_result", run_id=report["run_id"]))


@import_export_bp.route("/legacy/convert", methods=["POST"])
@login_required
def dc_legacy_convert():
    """Convert a legacy SQLite file into the schema-aware JSON envelope.

    One click: legacy .db → AMS JSON archive, then the same merge rules apply.
    """
    dc_guard()
    import sqlite3
    import uuid

    from flask import current_app

    from app.services.data_center_service import _storage, record_failed
    from data_ops.portable import export_json

    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a legacy .db/.sqlite3 file.", "danger")
        return redirect(url_for("import_export.dc_legacy_page"))
    name = Path(file.filename).name
    src = _storage() / f"legacy_{uuid.uuid4().hex}_{name[:80]}"
    src.write_bytes(file.read())
    try:
        conn = sqlite3.connect(str(src))
        try:
            out = _storage() / f"legacy_conv_{uuid.uuid4().hex}.json"
            info = export_json(conn, out, app_version="legacy-convert", db_name=name)
        finally:
            conn.close()
    except Exception as exc:
        record_failed("legacy", name, str(exc))
        flash(f"Conversion failed: {exc}", "danger")
        return redirect(url_for("import_export.dc_legacy_page"))
    flash(f"Converted {info['tables']} tables / {info['rows']} rows. Download and upload the JSON to merge.", "success")
    return send_file(out, as_attachment=True, download_name=out.name, mimetype="application/json")
