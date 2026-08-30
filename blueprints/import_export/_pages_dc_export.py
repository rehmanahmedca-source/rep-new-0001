"""Data Center: export (JSON archive / XLSX display / SQLite snapshot)."""
from __future__ import annotations

from flask import flash, render_template, request, send_file
from flask_login import login_required

from ._common import *  # noqa
from ._dc_common import dc_guard

EXPORT_FORMATS = (
    ("json", "AMX JSON archive", "Schema-aware, versioned, restorable + mergeable. THE transport format.", "bi-filetype-json", "success"),
    ("xlsx", "Excel workbook", "Read-only human view. Never used to restore.", "bi-file-earmark-excel", "secondary"),
    ("db", "SQLite snapshot", "Exact database file (VACUUM INTO). Disaster recovery.", "bi-database-add", "warning"),
)


@import_export_bp.route("/data-export")
@login_required
def dc_export_page():
    dc_guard()
    from data_ops.portable import tables_of

    import sqlite3

    from flask import current_app

    conn = sqlite3.connect(str(current_app.config["APP_DB_PATH"]))
    try:
        tables = tables_of(conn)
    finally:
        conn.close()
    return render_template(
        "data_center_export.html",
        formats=EXPORT_FORMATS,
        tables=tables,
        dc_active="export",
    )


def _requested_tables():
    raw = request.form.get("tables") or ""
    if raw.strip().lower() in ("", "all", "*"):
        return None
    return [t.strip() for t in raw.split(",") if t.strip()]


@import_export_bp.route("/export/json", methods=["POST"])
@login_required
def dc_export_json():
    dc_guard()
    from app.services.data_center_service import export_archive, record_export, record_failed

    try:
        path, name, stats = export_archive(format_name="json", tables=_requested_tables())
        record_export("json", name, stats)
        return send_file(path, as_attachment=True, download_name=name, mimetype="application/json")
    except Exception as exc:
        record_failed("export", "json", str(exc))
        flash(f"JSON export failed: {exc}", "danger")
        return _back_export()


@import_export_bp.route("/export/xlsx", methods=["POST"])
@login_required
def dc_export_xlsx():
    dc_guard()
    from app.services.data_center_service import export_archive, record_export, record_failed

    try:
        path, name, stats = export_archive(format_name="xlsx", tables=_requested_tables())
        record_export("xlsx", name, stats)
        return send_file(
            path,
            as_attachment=True,
            download_name=name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        record_failed("export", "xlsx", str(exc))
        flash(f"Excel export failed: {exc}", "danger")
        return _back_export()


@import_export_bp.route("/export/db", methods=["POST"])
@login_required
def dc_export_db():
    dc_guard()
    from app.services.data_center_service import export_archive, record_export, record_failed

    try:
        path, name, stats = export_archive(format_name="db")
        record_export("db", name, stats)
        return send_file(path, as_attachment=True, download_name=name, mimetype="application/vnd.sqlite3")
    except Exception as exc:
        record_failed("export", "db", str(exc))
        flash(f"Snapshot export failed: {exc}", "danger")
        return _back_export()


def _back_export():
    from flask import redirect, url_for

    return redirect(url_for("import_export.dc_export_page"))
