"""HTTP routes for the plant register."""
from __future__ import annotations

from flask import jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from blueprints.plant_registry._common import STATUS_CHOICES, STATUS_LABELS, plant_registry_bp
from models import db
from blueprints.plant_registry.service import (
    PlantRegistryError,
    get_asset,
    list_assets,
    locations,
    save_asset,
    set_status,
    summary,
)


def _actor() -> str:
    return str(getattr(current_user, "username", None) or "system")


@plant_registry_bp.route("/")
@login_required
def index():
    status = (request.args.get("status") or "").strip()
    location = (request.args.get("location") or "").strip()
    search = (request.args.get("q") or "").strip()
    include_retired = (request.args.get("retired") or "") in ("1", "true", "on")
    assets = list_assets(status=status, location=location, search=search, include_retired=include_retired)
    return render_template(
        "plant_registry/list.html",
        assets=assets,
        statuses=STATUS_CHOICES,
        status_labels=STATUS_LABELS,
        locations=locations(),
        selected={"status": status, "location": location, "search": search, "retired": include_retired},
        totals=summary(),
    )


@plant_registry_bp.route("/asset/<int:asset_id>")
@login_required
def detail(asset_id: int):
    from blueprints.plant_registry.models import PlantAssetMovement

    asset = get_asset(asset_id)
    if asset is None:
        return render_template("plant_registry/missing.html", asset_id=asset_id), 404
    return render_template(
        "plant_registry/detail.html",
        asset=asset,
        movements=list(
            db.session.query(PlantAssetMovement)
            .filter(PlantAssetMovement.asset_id == asset.id)
            .order_by(PlantAssetMovement.movement_date.desc(), PlantAssetMovement.id.desc())
            .all()
        )
        if asset.id
        else [],
        statuses=STATUS_CHOICES,
        status_labels=STATUS_LABELS,
    )


@plant_registry_bp.route("/asset/save", methods=["POST"])
@login_required
def save():
    asset = None
    asset_id = (request.form.get("asset_id") or "").strip()
    if asset_id:
        asset = get_asset(int(asset_id))
        if asset is None:
            return render_template("plant_registry/missing.html", asset_id=asset_id), 404
    try:
        saved = save_asset(request.form, actor=_actor(), asset=asset)
    except PlantRegistryError as exc:
        # Same UX rule as the rest of the ERP: a flash, then back to the form.
        return (
            render_template(
                "plant_registry/list.html",
                assets=list_assets(include_retired=True),
                statuses=STATUS_CHOICES,
                status_labels=STATUS_LABELS,
                locations=locations(),
                selected={},
                totals=summary(),
                form=request.form,
                error=str(exc),
            ),
            400,
        )
    from flask import flash

    flash(f"Asset '{saved.asset_code}' saved.", "success")
    return redirect(url_for("plant_registry.detail", asset_id=saved.id))


@plant_registry_bp.route("/asset/<int:asset_id>/status", methods=["POST"])
@login_required
def toggle_status(asset_id: int):
    asset = get_asset(asset_id)
    if asset is None:
        return render_template("plant_registry/missing.html", asset_id=asset_id), 404
    try:
        set_status(asset, request.form.get("status") or "", actor=_actor())
    except PlantRegistryError as exc:
        from flask import flash

        flash(str(exc), "danger")
        return redirect(url_for("plant_registry.detail", asset_id=asset_id))
    from flask import flash

    flash(f"'{asset.name}' is now {STATUS_LABELS.get(asset.status, asset.status)}.", "success")
    return redirect(url_for("plant_registry.detail", asset_id=asset_id))


@plant_registry_bp.route("/api/summary")
@login_required
def api_summary():
    return jsonify({"success": True, "data": summary()})
