"""Data Center: JSON restore (upload → plan → apply → result).

Only JSON is a restore/merge transport in the new data path.  The old Excel
tooling remains registered under the same blueprint for backward compatibility
but is no longer linked from the Data Center.
"""
from __future__ import annotations

from pathlib import Path

from flask import flash, redirect, render_template, request, session, url_for
from flask_login import login_required

from ._common import *  # noqa
from ._dc_common import dc_guard

PLAN_KEY = "dc_plan"


@import_export_bp.route("/restore")
@login_required
def dc_restore_page():
    dc_guard()
    from app.services.data_center_service import server_backups

    return render_template(
        "data_center_restore.html",
        backups=server_backups(),
        dc_active="restore",
    )


@import_export_bp.route("/restore/plan", methods=["POST"])
@login_required
def dc_restore_plan():
    dc_guard()
    from app.services.data_center_service import (
        plan_for,
        record_failed,
        save_upload,
    )

    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a JSON archive (.json) exported by AMS.", "danger")
        return redirect(url_for("import_export.dc_restore_page"))
    try:
        saved = save_upload(file, kind="restore")
        plan, upload = plan_for(saved["token"])
    except Exception as exc:
        record_failed("restore", file.filename or "?", str(exc))
        flash(f"Could not plan this archive: {exc}", "danger")
        return redirect(url_for("import_export.dc_restore_page"))

    session[PLAN_KEY] = {
        "token": saved["token"],
        "kind": "restore",
        "server_folder": "",
        "filename": saved["filename"],
        "sha256": saved["sha256"],
    }
    return render_template(
        "data_center_plan.html",
        plan=plan,
        upload=upload,
        action=url_for("import_export.dc_restore_apply"),
        kind="restore",
        back=url_for("import_export.dc_restore_page"),
        dc_active="restore",
    )


@import_export_bp.route("/restore/plan-server", methods=["POST"])
@login_required
def dc_restore_plan_server():
    dc_guard()
    from app.services.data_center_service import (
        _backup_root,
        plan_for,
        record_failed,
    )

    folder = (request.form.get("folder") or "").strip()
    json_path = _backup_root() / folder / "export.json"
    if not folder or not json_path.is_file():
        flash("Choose a valid server backup folder (one that contains export.json).", "danger")
        return redirect(url_for("import_export.dc_restore_page"))
    try:
        plan, upload = plan_for(folder, path_override=str(json_path))
    except Exception as exc:
        record_failed("restore", folder, str(exc))
        flash(f"Could not plan this backup: {exc}", "danger")
        return redirect(url_for("import_export.dc_restore_page"))

    session[PLAN_KEY] = {
        "token": folder,
        "kind": "restore",
        "server_folder": folder,
        "filename": "export.json",
        "sha256": upload.get("sha256", ""),
    }
    return render_template(
        "data_center_plan.html",
        plan=plan,
        upload=upload,
        action=url_for("import_export.dc_restore_apply"),
        kind="restore",
        back=url_for("import_export.dc_restore_page"),
        dc_active="restore",
    )


@import_export_bp.route("/restore/apply", methods=["POST"])
@login_required
def dc_restore_apply():
    dc_guard()
    from app.services.data_center_service import (
        _backup_root,
        apply_upload,
        record_failed,
    )

    plan_meta = session.get(PLAN_KEY) or {}
    if not plan_meta.get("token"):
        flash("No pending plan. Upload an archive and review the plan first.", "danger")
        return redirect(url_for("import_export.dc_restore_page"))
    if str(request.form.get("confirm") or "").strip() != "APPLY":
        flash("Type APPLY to confirm the restore.", "warning")
        return redirect(url_for("import_export.dc_restore_page"))

    path_override = None
    if plan_meta.get("server_folder"):
        path_override = str(_backup_root() / plan_meta["server_folder"] / "export.json")
    try:
        report = apply_upload(
            plan_meta["token"],
            kind="restore",
            path_override=path_override,
        )
    except Exception as exc:
        record_failed("restore", plan_meta.get("filename", "?"), str(exc))
        flash(f"Restore was rolled back (nothing changed): {exc}", "danger")
        session.pop(PLAN_KEY, None)
        return redirect(url_for("import_export.dc_restore_page"))
    session.pop(PLAN_KEY, None)
    return redirect(url_for("import_export.dc_restore_result", run_id=report["run_id"]))


@import_export_bp.route("/restore/result/<int:run_id>")
@login_required
def dc_restore_result(run_id):
    dc_guard()
    from app.services.data_center_service import run_by_id

    run = run_by_id(run_id)
    if run is None:
        flash("Run not found.", "warning")
        return redirect(url_for("import_export.dc_restore_page"))
    return render_template(
        "data_center_result.html",
        run=run,
        summary=run.summary_json or {},
        dc_active="restore",
    )
