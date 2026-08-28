"""The reference module, tested the way the pipeline tests it.

``blueprints/plant_registry`` is not decoration: it is the worked example of the
module contract, and these are the tests the update pipeline runs before it will
mark a module READY (``[tests] paths`` in ``module.toml``).  A new module author
should be able to copy this file and change the names.
"""
from __future__ import annotations

import pytest

from app.services.dbupdate import migrations as MIG
from app.services.dbupdate import policy
from app.services.module_system import contract as C

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}
MODULE_ROOT = "blueprints/plant_registry"


def login(client):
    with client.session_transaction() as sess:
        sess["_csrf_token"] = "plant-registry-test-token"
    response = client.post(
        "/login",
        data={**ADMIN, "_csrf_token": "plant-registry-test-token"},
        follow_redirects=False,
    )
    assert response.status_code == 302, response.get_data(as_text=True)[:500]
    return client


# ---------------------------------------------------------------------------
# the manifest itself
# ---------------------------------------------------------------------------


def test_manifest_validates_against_the_real_application(app):
    spec = C.validate_manifest(
        _read_manifest(),
        module_root=_module_dir(),
        package="blueprints.plant_registry",
        manifest_path=_module_dir() / "module.toml",
        known_modules={"accounts", "inventory", "import_export", "admin", "data_lab"},
        existing_prefixes={},
        existing_endpoint_owners={},
        registered_tables=(),
        app_version=str(app.config.get("APP_VERSION") or "1.0.0"),
    )

    assert spec.errors() == [], [p.as_dict() for p in spec.errors()]
    assert spec.module_id == "plant_registry"
    assert spec.enabled is True
    assert spec.depends_on == ("accounts",)
    assert set(spec.tables) == {"plant_asset", "plant_asset_movement"}
    assert len(spec.navigation) == 2
    assert len(spec.migrations) == 2


def _module_dir():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "blueprints" / "plant_registry"


def _read_manifest():
    return C.read_manifest(_module_dir() / "module.toml")


def test_module_is_registered_and_its_routes_exist(app):
    registry = app.extensions["ams_modules"]
    spec = registry.specs["plant_registry"]

    assert spec.status in {"REGISTERED", "READY", "MIGRATION_REQUIRED"}
    for endpoint in spec.expected_endpoints:
        assert endpoint in app.view_functions, f"{endpoint} declared but not mounted"


def test_registered_module_tables_exist_in_the_database(app):
    from models import db
    from sqlalchemy import inspect

    registry = app.extensions["ams_modules"]
    with app.app_context():
        names = set(inspect(db.engine).get_table_names())
        spec = registry.specs["plant_registry"]

    for table in spec.tables:
        assert table in names, f"module declares {table} but the schema does not have it"


def test_module_revisions_are_discovered_and_valid(app):
    registry = app.extensions["ams_modules"]
    revisions = MIG.collect(app, registry=registry)
    checked = MIG.validate(revisions, policy=policy.resolve(app), applied={})

    assert {r.module_id for r in checked} == {"plant_registry"}
    assert [r.revision for r in checked] == sorted(r.revision for r in checked)
    assert all(r.problems == [] for r in checked), [r.as_dict() for r in checked]
    schema_revision, data_revision = checked
    assert schema_revision.kind == "schema"
    assert data_revision.kind == "data"
    assert data_revision.has_verify is True, "a data revision must be able to verify itself"
    assert data_revision.data_validation is True


# ---------------------------------------------------------------------------
# pages and business rules (through HTTP, like a user)
# ---------------------------------------------------------------------------


def test_index_lists_assets_and_shows_the_form(client):
    login(client)
    response = client.get("/plant_registry/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Plant" in body
    assert "asset_code" in body or "Asset code" in body


def test_create_view_update_and_status_change(client):
    login(client)
    save = client.post(
        "/plant_registry/asset/save",
        data={
            "name": "Weighbridge No. 2",
            "asset_code": "WB-2",
            "category": "weighing",
            "location": "Main Gate",
            "status": "available",
            "purchase_value": "1250000.50",
            "commissioned_on": "2024-03-15",
            "notes": "Calibrated yearly",
        },
        follow_redirects=False,
    )
    assert save.status_code == 302, save.get_data(as_text=True)[:800]
    detail_url = save.headers["Location"]

    detail = client.get(detail_url)
    assert detail.status_code == 200
    body = detail.get_data(as_text=True)
    assert "Weighbridge No. 2" in body
    assert "WB-2" in body

    asset_id = int(detail_url.rstrip("/").split("/")[-1])
    toggle = client.post(
        f"/plant_registry/asset/{asset_id}/status",
        data={"status": "on-site"},
        follow_redirects=True,
    )
    assert toggle.status_code == 200
    assert "On site" in toggle.get_data(as_text=True)

    from models import db

    from blueprints.plant_registry.models import PlantAsset

    with app_of(client).app_context():
        asset = db.session.query(PlantAsset).filter(PlantAsset.asset_code == "WB-2").one()
        assert asset.status == "on-site"
        assert asset.revision >= 2, "an update must bump the revision counter"
        assert asset.purchase_value_minor == 125000050, "money is stored in exact minor units"


def app_of(client):
    return client.application


def test_negative_purchase_value_is_rejected_and_nothing_is_written(client):
    login(client)
    response = client.post(
        "/plant_registry/asset/save",
        data={"name": "Bad Mixer", "asset_code": "MIX-NEG", "purchase_value": "-500"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "negative" in response.get_data(as_text=True).lower()
    from models import db

    from blueprints.plant_registry.models import PlantAsset

    with app_of(client).app_context():
        assert db.session.query(PlantAsset).filter(PlantAsset.asset_code == "MIX-NEG").first() is None


def test_duplicate_asset_code_is_rejected(client):
    login(client)
    first = client.post(
        "/plant_registry/asset/save",
        data={"name": "Genset A", "asset_code": "GEN-1", "purchase_value": "10"},
    )
    assert first.status_code == 302

    second = client.post(
        "/plant_registry/asset/save",
        data={"name": "Genset B", "asset_code": "GEN-1", "purchase_value": "10"},
    )

    assert second.status_code == 400
    body = second.get_data(as_text=True)
    assert "already used" in body.lower()
    with app_of(client).app_context():
        from models import db

        from blueprints.plant_registry.models import PlantAsset

        assert db.session.query(PlantAsset).count() == 1, "the rejected save must not add a row"


def test_invalid_status_is_reported_not_silently_kept(client):
    login(client)
    created = client.post("/plant_registry/asset/save", data={"name": "Crane", "asset_code": "CRN-1"})
    asset_id = int(created.headers["Location"].rstrip("/").split("/")[-1])

    response = client.post(f"/plant_registry/asset/{asset_id}/status", data={"status": "exploded"}, follow_redirects=True)

    assert response.status_code == 200
    with app_of(client).app_context():
        from models import db

        from blueprints.plant_registry.models import PlantAsset

        asset = db.session.query(PlantAsset).filter(PlantAsset.asset_code == "CRN-1").one()
        assert asset.status != "exploded"
        assert "Status must be one of" in response.get_data(as_text=True)


def test_missing_asset_renders_the_missing_page(client):
    login(client)
    response = client.get("/plant_registry/asset/999999")

    assert response.status_code == 404
    assert "999999" in response.get_data(as_text=True)


def test_api_summary_returns_json(client):
    login(client)
    response = client.get("/plant_registry/api/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert "totals" in payload["data"] or "assets" in payload["data"]


def test_anonymous_visitors_are_redirected_to_login(client):
    response = client.get("/plant_registry/", follow_redirects=False)

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_navigation_entry_appears_in_the_sidebar(client):
    login(client)
    body = client.get("/").get_data(as_text=True)

    assert "/plant_registry/" in body, "the module's nav item must reach the real sidebar"


def test_module_health_check_passes(app):
    from app.services.module_system.health import run_module_health

    registry = app.extensions["ams_modules"]
    with app.app_context():
        result = run_module_health(app, registry, module_ids=["plant_registry"])

    assert result["status"] == "PASS", result["modules"]["plant_registry"]["checks"]


@pytest.mark.parametrize("verb", ["GET"])
def test_read_only_mode_still_lists_assets(app, client, verb):
    login(client)
    with app.app_context():
        app.config["READ_ONLY_MODE"] = True
        try:
            response = client.get("/plant_registry/")
            assert response.status_code == 200
            blocked = client.post("/plant_registry/asset/save", data={"name": "Nope", "asset_code": "NOPE-1"})
            assert blocked.status_code in (403, 405, 302)
        finally:
            app.config["READ_ONLY_MODE"] = False
