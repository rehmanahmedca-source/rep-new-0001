"""The administrative surface of the module system must never 500.

The STEP B harness crawls every page a real user can reach, and a management
screen is exactly where a "registry object has no attribute health" style bug
hides: it is only ever opened by an admin, and when something is already wrong.
These tests pin every module/update view and API to a 200 plus the content an
operator actually reads.
"""
from __future__ import annotations

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}
TOKEN = "admin-module-view-token"


def login(client):
    with client.session_transaction() as sess:
        sess["_csrf_token"] = TOKEN
    response = client.post("/login", data={**ADMIN, "_csrf_token": TOKEN})
    assert response.status_code == 302, response.get_data(as_text=True)[:400]
    return client


def test_registry_page_lists_modules_and_statuses(client):
    login(client)
    response = client.get("/admin/modules")

    assert response.status_code == 200, response.get_data(as_text=True)[:2000]
    body = response.get_data(as_text=True)
    for fragment in ("Module Registry", "plant_registry", "module_template", "schema_api"):
        assert fragment in body, fragment


def test_registry_page_shows_disabled_module_with_reason(client):
    login(client)
    body = client.get("/admin/modules").get_data(as_text=True)

    assert "DISABLED" in body, "disabled modules must be visible, not hidden"


def test_updates_page_renders_report_and_history(client):
    login(client)
    response = client.get("/admin/modules/updates")

    assert response.status_code == 200, response.get_data(as_text=True)[:2000]
    body = response.get_data(as_text=True)
    assert "Database &amp; Module Updates" in body or "Module Updates" in body
    for fragment in ("Pipeline steps", "Recent runs", "Applied revisions"):
        assert fragment in body, fragment


def test_api_modules_returns_registry(client):
    login(client)
    payload = client.get("/admin/api/modules").get_json()

    assert payload["success"] is True
    assert payload["registry"]["contract_schema_api"] == 1
    ids = {module["id"] for module in payload["registry"]["modules"]}
    assert {"plant_registry", "accounts", "module_template"} <= ids


def test_api_schema_returns_audit(client):
    login(client)
    payload = client.get("/admin/api/modules/schema").get_json()

    assert payload["status"] in {"OK", "SCHEMA_DRIFT", "MIGRATION_REQUIRED"}
    assert "issues" in payload and "counts" in payload
    for issue in payload["issues"]:
        assert issue["severity"] in {"OK", "ADDITIVE", "MANUAL", "DESTRUCTIVE"}


def test_api_updates_can_run_a_read_only_check(client):
    login(client)
    payload = client.get("/admin/api/modules/updates?run=1").get_json()

    assert payload["success"] is True
    report = payload["report"]
    assert report["mode"] == "check"
    assert report["final_status"]
    assert report["pipeline"], "every step must be reported"
    for step in report["pipeline"]:
        assert step["status"] in {"PASS", "WARN", "SKIPPED", "FAIL"}, step


def test_health_probe_reports_update_state(app):
    body = app.test_client().get("/health").get_json()

    assert body["status"] in {"healthy", "degraded"}
    assert body["database"] in {"ok", "error"}
    assert "modules" in body and "update" in body
    assert body["modules"]["registered"] >= 1
    assert "final_status" in body["update"]


def test_non_admins_cannot_see_the_registry(client, app):
    # The admin blueprint guards itself; a logged-in non-admin still gets 403.
    from models import User
    from werkzeug.security import generate_password_hash

    username = "qa_viewer"
    with app.app_context():
        from models import db

        if db.session.query(User).filter(User.username == username).first() is None:
            db.session.add(
                User(username=username, role="user", password_hash=generate_password_hash("View@12345"))
            )
            db.session.commit()
    with client.session_transaction() as sess:
        sess["_csrf_token"] = TOKEN
    login_as_viewer = client.post(
        "/login", data={"username": username, "password": "View@12345", "_csrf_token": TOKEN}
    )
    assert login_as_viewer.status_code == 302, login_as_viewer.get_data(as_text=True)[:400]

    for path in ("/admin/modules", "/admin/modules/updates", "/admin/api/modules"):
        response = client.get(path)
        assert response.status_code == 403, f"{path} returned {response.status_code}"
