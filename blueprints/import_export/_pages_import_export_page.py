"""Data Center hub — replaces the old Excel-centric landing page."""
from __future__ import annotations

from flask import render_template
from flask_login import login_required

from ._common import *  # noqa
from ._dc_common import dc_guard


@import_export_bp.route("/")
@login_required
def import_export_page():
    dc_guard()
    from app.services.data_center_service import recent_runs, server_backups

    runs = recent_runs(12)
    backups = server_backups()[:6]
    return render_template(
        "data_center.html",
        runs=runs,
        backups=backups,
        format_version="2026-08",
        dc_active="home",
    )
