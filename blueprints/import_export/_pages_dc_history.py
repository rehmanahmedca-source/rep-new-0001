"""Data Center: run history (exports, restores, legacy merges, snapshots)."""
from __future__ import annotations

from flask import render_template, request
from flask_login import login_required

from ._common import *  # noqa
from ._dc_common import dc_guard


@import_export_bp.route("/history")
@login_required
def dc_history_page():
    dc_guard()
    from app.services.data_center_service import recent_runs

    kind = (request.args.get("kind") or "").strip()
    runs = recent_runs(200)
    if kind:
        runs = [r for r in runs if r.kind == kind]
    return render_template(
        "data_center_history.html",
        runs=runs,
        kind=kind,
        dc_active="history",
    )
