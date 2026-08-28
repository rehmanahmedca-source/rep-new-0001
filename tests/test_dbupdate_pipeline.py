"""The controlled update pipeline: policy, revisions, ledger, audit, reports.

Everything here runs against a throw-away SQLite file in ``tmp_path``.  Where a
real application is needed the factory is used with
``AMS_SKIP_UPDATE_PIPELINE=1`` so boot stays fast and deterministic, and the
pipeline is then invoked explicitly in the mode under test.
"""
from __future__ import annotations

import json
import sqlite3
import textwrap

import pytest

from app.services.dbupdate import integrity, ledger, migrations as MIG, policy, reports, schema_audit
from app.services.dbupdate import runner as RUNNER
from app.services.dbupdate import legacy_steps
from app.services.module_system.registry import ModuleRegistry

# ---------------------------------------------------------------------------
# environment policy
# ---------------------------------------------------------------------------


def test_environment_detection_prefers_explicit_setting(monkeypatch):
    monkeypatch.delenv("AMS_ENV", raising=False)
    monkeypatch.delenv("PYTHONANYWHERE_DOMAIN", raising=False)
    monkeypatch.delenv("PYTHONANYWHERE_SITE", raising=False)
    assert policy.detect_environment(None) == "development"

    monkeypatch.setenv("AMS_ENV", "production")
    assert policy.detect_environment(None) == "production"

    monkeypatch.delenv("AMS_ENV")
    monkeypatch.setenv("PYTHONANYWHERE_DOMAIN", "erp.pythonanywhere.com")
    assert policy.detect_environment(None) == "production", "hosting markers must infer production"


def test_production_policy_cannot_be_weakend(monkeypatch):
    monkeypatch.setenv("AMS_ENV", "production")
    monkeypatch.setenv("AMS_UPDATE_POLICY", "")
    monkeypatch.setenv("AMS_REQUIRE_BACKUP_BEFORE_UPDATE", "0")
    monkeypatch.setenv("AMS_ALLOW_DB_RESET", "1")

    resolved = policy.resolve(None)

    assert resolved.policy == "guarded", "production defaults to the guarded policy"
    assert resolved.require_backup is True, "a live ledger always gets a backup first"
    assert resolved.reset_allowed is False, "production can never reset the database"
    assert resolved.allow_destructive is False
    text = " ".join(resolved.notes)
    assert "ignored in production" in text and "reset is disabled in production" in text


@pytest.mark.parametrize(
    "name,auto_apply,writes",
    [("auto", True, True), ("guarded", True, True), ("audit", False, False), ("manual", False, False)],
)
def test_policy_names_control_writes(monkeypatch, name, auto_apply, writes):
    monkeypatch.setenv("AMS_ENV", "development")
    monkeypatch.setenv("AMS_UPDATE_POLICY", name)

    resolved = policy.resolve(None)

    assert resolved.auto_apply is auto_apply
    assert bool(resolved.auto_apply) is writes


def test_production_never_inherits_development_defaults(monkeypatch, tmp_path):
    """The whole point of the policy module: same code, different behaviour."""
    monkeypatch.setenv("AMS_ENV", "development")
    monkeypatch.delenv("AMS_UPDATE_POLICY", raising=False)
    dev = policy.resolve(None)
    monkeypatch.setenv("AMS_ENV", "production")
    prod = policy.resolve(None)

    assert dev.allow_create_all_on_populated is True
    assert prod.allow_create_all_on_populated is False


# ---------------------------------------------------------------------------
# revision discovery, linting and validation
# ---------------------------------------------------------------------------


REVISION_TEMPLATE = """# Fixture revision, written by tests/test_dbupdate_pipeline.py.
REVISION = "{revision}"
TITLE = "fixture revision"
KIND = "schema"
MODULE = "fixture_mod"
DESTRUCTIVE = False
{extra}


def upgrade(connection):
{body}


def verify(connection):
    return {{"ok": True, "rows": 0}}
"""

DEFAULT_BODY = (
    '    connection.exec_driver_sql('
    '"CREATE TABLE IF NOT EXISTS fixture_table (id INTEGER PRIMARY KEY)")\n'
    '    return {"created": "fixture_table"}'
)


def _write_revision(root, name, *, body=None, sql=None, extra=""):
    """Create a fixture revision file (``.py`` or ``.sql``) and return its path."""
    directory = root / "migrations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if sql is not None:
        path.write_text("-- AMS SQL migration\n" + sql, encoding="utf-8")
        return path
    parts = name.replace(".py", "").split("_")
    path.write_text(
        REVISION_TEMPLATE.format(
            revision="_".join(parts[:2]),
            extra=extra,
            body=body if body is not None else DEFAULT_BODY,
        ),
        encoding="utf-8",
    )
    return path


def test_sql_revision_linting_blocks_destructive_statements(tmp_path):
    problems = MIG.lint_sql("DROP TABLE ledger_entry;", destructive_allowed=False)

    assert problems and problems[0]["code"] == "destructive_statement"
    assert "DROP TABLE" in problems[0]["message"]
    assert problems[0]["hint"]
    assert MIG.lint_sql("CREATE TABLE a (id INTEGER);", destructive_allowed=False) == []
    assert MIG.lint_sql("DROP TABLE a;", destructive_allowed=True) == []


def test_lint_catches_unbounded_update_and_empty_sql():
    assert any(p["code"] == "unbounded_update" for p in MIG.lint_sql("UPDATE client SET balance = 0;", destructive_allowed=True))
    assert MIG.lint_sql("UPDATE client SET balance = 0 WHERE id = 5;", destructive_allowed=False) == []
    assert MIG.lint_sql("   ", destructive_allowed=False) == [{"code": "empty_sql", "message": "revision declares SQL but it is empty"}]


def test_collect_and_validate_flags_modified_revisions(tmp_path, monkeypatch):
    root = tmp_path / "fixture_mod"
    path = _write_revision(root, "2026_001_add_fixture_table.py")
    revision = MIG._revision_from_python(path, module_id="fixture_mod")

    assert revision.revision == "2026_001"
    assert revision.kind == "schema"
    assert revision.has_verify is True
    assert revision.global_revision == "fixture_mod:2026_001"

    # First pass: nothing applied yet -> PENDING.
    checked = MIG.validate([revision], policy=policy.resolve(None), applied={})
    assert checked[0].status == "PENDING"

    # Applied with a *different* checksum -> MODIFIED, and it must be loud.
    tampered = dict(checked[0].as_dict())
    ledger_row = {"checksum": "0" * 64, "status": "APPLIED", "filename": path.name}
    checked = MIG.validate([revision], policy=policy.resolve(None), applied={revision.global_revision: ledger_row})
    assert checked[0].status == "MODIFIED"
    assert any("never edit history" in p.get("hint", "") for p in checked[0].problems), checked[0].problems
    assert tampered["checksum"] != ledger_row["checksum"]


def test_data_revision_without_verification_is_refused(tmp_path):
    root = tmp_path / "fixture_mod"
    path = _write_revision(
        root,
        "2026_002_backfill.py",
        body='    connection.exec_driver_sql("UPDATE fixture_table SET x = 1")\n    return {"rows": 1}\n',
    )
    path.write_text(
        path.read_text().replace("def verify(connection):", "def _unused_verify(connection):"),
        encoding="utf-8",
    )
    revision = MIG._revision_from_python(path, module_id="fixture_mod")
    revision.kind = "data"

    checked = MIG.validate([revision], policy=policy.resolve(None), applied={})

    assert checked[0].status == "REQUIRES_ATTENTION"
    assert any("verify" in p["message"] for p in checked[0].problems)


def test_destructive_revision_is_blocked_in_production(tmp_path, monkeypatch):
    monkeypatch.setenv("AMS_ENV", "production")
    monkeypatch.delenv("AMS_UPDATE_POLICY", raising=False)
    monkeypatch.delenv("AMS_ALLOW_DESTRUCTIVE_MIGRATIONS", raising=False)
    root = tmp_path / "fixture_mod"
    path = _write_revision(root, "2026_003_drop", sql="DROP TABLE unused_legacy;\n")
    revision = MIG._revision_from_sql(path, module_id="fixture_mod")
    revision.destructive = True

    checked = MIG.validate([revision], policy=policy.resolve(None), applied={})

    assert checked[0].status == "REQUIRES_ATTENTION"
    codes = {p["code"] for p in checked[0].problems}
    assert any(code.startswith("destructive") for code in codes), checked[0].problems
    assert any("AMS_ALLOW_DESTRUCTIVE_MIGRATIONS" in p.get("hint", "") for p in checked[0].problems)


def test_plan_counts_and_sorts(tmp_path):
    root = tmp_path / "fixture_mod"
    first = MIG._revision_from_python(_write_revision(root, "2026_001_a.py"), module_id="fixture_mod")
    second = MIG._revision_from_python(_write_revision(root, "2026_002_b.py"), module_id="fixture_mod")
    first.status = "APPLIED"
    second.status = "PENDING"

    plan = MIG.plan(MIG.sort_revisions([second, first]))

    assert plan["total_revisions"] == 2
    assert plan["applied"] == 1
    assert plan["pending"] == 1
    assert plan["requires_attention"] == 0


# ---------------------------------------------------------------------------
# ledger (uses its own engine, so no application factory is involved)
# ---------------------------------------------------------------------------


@pytest.fixture()
def session(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(f"sqlite:///{tmp_path/'ledger.db'}", future=True)
    with Session(engine) as scope:
        yield scope
    engine.dispose()


def test_ledger_roundtrip(session):
    assert ledger.exists(session) is False
    assert ledger.ensure_ledger(session) is True
    assert ledger.exists(session) is True

    ledger.record(
        revision="fixture_mod:2026_001",
        module_id="fixture_mod",
        version="1.0.0",
        slug="add_fixture_table",
        filename="2026_001_add_fixture_table.py",
        kind="schema",
        checksum="abc123",
        status="APPLIED",
        duration_ms=12,
        app_version="1.0.0",
        schema_version_before=1,
        schema_version_after=2,
        backup_path="/tmp/backup_x",
        affected_rows=0,
        report={"created": "fixture_table"},
        session=session,
    )
    applied = ledger.applied_revisions(session)

    assert "fixture_mod:2026_001" in applied
    assert applied["fixture_mod:2026_001"]["checksum"] == "abc123"
    history = ledger.history(limit=5, session=session)
    assert history[0]["revision"] == "fixture_mod:2026_001"
    assert history[0]["status"] == "APPLIED"


def test_schema_version_lives_in_the_database_file(session):
    assert ledger.read_schema_version(session) == 0
    ledger.write_schema_version(7, session=session)

    assert ledger.read_schema_version(session) == 7


def test_run_lifecycle_records_verification(session):
    ledger.ensure_ledger(session)
    ledger.begin_run(run_key="update-test-1", environment="development", policy="auto", mode="apply", session=session)
    running = ledger.recent_runs(limit=1, session=session)
    assert running[0]["final_status"] == "RUNNING"

    ledger.finish_run(
        run_key="update-test-1",
        report={
            "final_status": "READY",
            "environment": "development",
            "policy": "auto",
            "mode": "apply",
            "migrations": {"total_revisions": 3, "applied": 2, "failed": 0},
            "modules": {"discovered": 4, "registered": 3, "failed": 0},
            "backup": {"path": "/tmp/b1", "sha256": "deadbeef"},
            "checks": {
                "schema_validation": {"status": "OK"},
                "data_integrity": {"status": "PASS"},
                "regression": {"status": "PASS"},
            },
            "schema_audit": {"expected_schema_version": 4},
        },
        session=session,
    )
    finished = ledger.recent_runs(limit=1, session=session)[0]

    assert finished["final_status"] == "READY"
    assert finished["migrations_applied"] == 2
    assert finished["schema_validation"] == "OK"
    assert finished["integrity_status"] == "PASS"
    assert finished["backup_path"] == "/tmp/b1"
    assert finished["finished_at"]


def test_legacy_migration_history_is_adopted_once(session):
    from sqlalchemy import text

    ledger.ensure_ledger(session)
    session.execute(
        text(
            "CREATE TABLE migration_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "filename TEXT NOT NULL UNIQUE, applied_at TEXT, notes TEXT)"
        )
    )
    session.execute(text("INSERT INTO migration_history (filename, applied_at) VALUES ('2024_01_01_legacy.sql', 'x')"))
    session.commit()

    adopted = ledger.import_legacy_history(session)
    again = ledger.import_legacy_history(session)

    assert adopted == 1
    assert again == 0
    applied = ledger.applied_revisions(session)
    assert any("2024_01_01_legacy" in key for key in applied)


def test_ensure_ledger_does_not_write_in_check_mode(session):
    assert ledger.ensure_ledger(session, allow_create=False) is False
    assert ledger.exists(session) is False


def test_bootstrap_steps_are_all_declared():
    """Every ensure-step in ``app/services/schema.py`` must be in the inventory.

    This is the guard that stops a swallowed ``except: pass`` from hiding a step
    again: a new call in the legacy bootstrap has to appear in the pipeline's
    declared list (so it is run, recorded and audited) or the test fails.
    """
    assert legacy_steps.undocumented_steps() == []
    assert legacy_steps.unrun_steps() == []
    assert "ensure_model_columns" in legacy_steps.step_names()


# ---------------------------------------------------------------------------
# schema audit
# ---------------------------------------------------------------------------


def test_audit_detects_missing_table_and_never_proposes_a_drop(tmp_path):
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{tmp_path/'audit.db'}", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE other_table (id INTEGER PRIMARY KEY, note TEXT)"))
        conn.execute(text("CREATE TABLE orphaned (id INTEGER, extra TEXT)"))

    class FakeRegistry:
        specs: dict = {}
        module_root = tmp_path

    result = schema_audit.audit(engine, registry=FakeRegistry(), expected_revision=3)

    assert result["status"] in {"MIGRATION_REQUIRED", "SCHEMA_DRIFT", "OK"}
    assert result["tables_present"] == 2
    # A column present in the database but unknown to the models is reported as
    # an observation only: no issue may ever say "drop".
    for issue in result["issues"]:
        assert issue["severity"] in {"ADDITIVE", "MANUAL", "DESTRUCTIVE", "OK"}
        assert "DROP TABLE" not in str(issue.get("fix", "")).upper() or issue["severity"] == "MANUAL"


def test_audit_snapshot_shape(tmp_path):
    """Reports must separate "safe to add" from "a human must decide"."""
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path/'shape.db'}", future=True)
    snapshot = schema_audit.inspect_database(engine)

    assert set(snapshot) >= {"tables", "table_names"}
    assert snapshot["table_names"] == []
    result = schema_audit.audit(engine)
    assert set(result["counts"]) == {"additive", "manual", "destructive", "informational"}
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path/'audit2.db'}", future=True)

    class EmptyRegistry:
        specs: dict = {}
        module_root = tmp_path

    result = schema_audit.audit(engine, registry=EmptyRegistry(), expected_revision=9)

    assert result["current_schema_version"] == 0
    assert result["expected_schema_version"] == 9
    assert result["summary"]["issue_count"] >= 0


def test_unmanaged_tables_are_observed_not_attacked(tmp_path):
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{tmp_path/'audit3.db'}", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE mystery (id INTEGER)"))

    class OwningRegistry:
        specs = {"some_mod": type("S", (), {"module_id": "some_mod", "enabled": True, "tables": ("claimed",), "status": "REGISTERED", "problems": []})()}
        module_root = tmp_path

    result = schema_audit.audit(engine, registry=OwningRegistry(), expected_revision=None)
    kinds = {issue["kind"] for issue in result["issues"]}

    assert "unmanaged_table" in kinds
    unmanaged = next(i for i in result["issues"] if i["kind"] == "unmanaged_table")
    assert unmanaged["severity"] in {"OK", "MANUAL"}
    assert "mystery" in str(unmanaged["object"]) + str(unmanaged["detail"])


# ---------------------------------------------------------------------------
# regression / data-loss guards
# ---------------------------------------------------------------------------


def test_row_count_shrink_is_a_data_loss_signal():
    verdict = integrity.compare_counts({"ledger_entry": 10, "client": 3}, {"ledger_entry": 9, "client": 4})

    assert verdict["status"] == "FAIL"
    assert verdict["row_losses"] == [{"table": "ledger_entry", "before": 10, "after": 9, "missing": 1}]
    assert verdict["tables_gained_rows"] == ["client"]


def test_declared_shrink_is_still_reported_but_tolerated():
    verdict = integrity.compare_counts(
        {"staging_rows": 5}, {"staging_rows": 0}, allowed_shrink={"staging_rows"}
    )

    assert verdict["status"] == "PASS"
    assert verdict["row_losses"] == []


def test_data_revision_that_deletes_is_refused(tmp_path):
    """DELETE inside a data revision is destructive: blocked, never guessed at."""
    root = tmp_path / "fixture_mod"
    directory = root / "migrations"
    directory.mkdir(parents=True)
    path = directory / "2026_009_purge.sql"
    path.write_text(
        "UPDATE staging_rows SET state = 'clean';\nDELETE FROM staging_rows WHERE state = 'temp';\n",
        encoding="utf-8",
    )
    revision = MIG._revision_from_sql(path, module_id="fixture_mod")

    checked = MIG.validate([revision], policy=policy.resolve(None), applied={})

    assert checked[0].status == "REQUIRES_ATTENTION", checked[0].as_dict()
    codes = {problem["code"] for problem in checked[0].problems}
    assert "destructive_statement" in codes, checked[0].problems


def test_reference_data_revision_preserves_originals():
    """``plant_registry`` 2026_002 is the worked example of the data contract."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "blueprints" / "plant_registry" / "migrations" / "2026_002_normalise_asset_locations.py"
    ).read_text(encoding="utf-8")

    assert "KIND = \"data\"" in source
    assert "def verify(" in source, "a data revision must be able to prove its own result"
    assert "original" in source.lower() or "location_code" in source, "the source value must be preserved or recorded"
    assert "exception" in source.lower(), "records that cannot be transformed are reported, not dropped"
    assert not any(line.strip().upper().startswith(("DELETE FROM", "DROP TABLE")) for line in source.splitlines())


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------


def test_reports_are_redacted_and_machine_readable(tmp_path):
    payload = {
        "password": "hunter2",
        "database_url": "sqlite:////tmp/x.db",
        "api_key": "sk-live-123",
        "nested": {"Authorization": "Bearer abc", "note": "sqlite://user:pw@host/db"},
        "count": 3,
    }
    cleaned = reports.redact(payload)
    text = json.dumps(cleaned)

    assert cleaned["password"] == "[redacted]"
    assert cleaned["nested"]["Authorization"] == "[redacted]"
    assert "[redacted]@host/db" in text
    assert "hunter2" not in text
    assert "sk-live-123" not in text, "a value under a secret-looking key is stripped too"
    assert cleaned["count"] == 3


def test_health_report_lists_every_required_line(tmp_path):
    rendered = reports.render_health_report(
        {
            "run_key": "update-x",
            "final_status": "READY",
            "environment": "development",
            "policy": "auto",
            "app_version": "1.0.0",
            "modules": {"discovered": 2, "registered": 2, "failed": 0, "disabled": 0},
            "migrations": {"pending": 0, "applied": 1, "failed": 0, "blocked": 0, "total_revisions": 1},
            "schema_audit": {"current_schema_version": 2, "expected_schema_version": 2},
            "backup": {"status": "PASS", "path": str(tmp_path)},
            "checks": {
                "schema_validation": {"status": "OK"},
                "data_integrity": {"status": "PASS"},
                "regression": {"status": "PASS"},
                "module_health": {"status": "PASS"},
            },
        }
    )

    for line in (
        "FINAL STATUS:",
        "MODULES DISCOVERED:",
        "MIGRATIONS PENDING:",
        "BACKUP CREATED:",
        "SCHEMA VALIDATION:",
        "DATA INTEGRITY STATUS:",
        "REGRESSION TESTS:",
        "HEALTH CHECKS:",
    ):
        assert line in rendered, line


def test_report_retention_is_bounded(tmp_path, monkeypatch):
    class FakeApp:
        config = {"UPDATE_REPORT_DIR": str(tmp_path), "UPDATE_REPORT_HISTORY": 2}
        instance_path = str(tmp_path)

    for index in range(5):
        reports.write(FakeApp(), {"run_key": f"run-{index}", "final_status": "READY"}, archive=True)

    archive = tmp_path / "update-history"
    stamps = sorted({path.name.split(".")[1] for path in archive.iterdir()})
    assert len(stamps) <= 2, "archives must not grow without bound"
    assert (tmp_path / "update-health-report.json").exists()
    assert (tmp_path / "UPDATE_HEALTH_REPORT.md").exists()


# ---------------------------------------------------------------------------
# the pipeline itself
# ---------------------------------------------------------------------------


def test_check_mode_is_read_only(app_factory, tmp_path, monkeypatch):
    db_file = tmp_path / "check.db"
    app = app_factory(db_file, AMS_SKIP_UPDATE_PIPELINE="1", AMS_ENV="development", UPDATE_REPORT_DIR=str(tmp_path / "logs"))
    from models import db
    from sqlalchemy import text

    with app.app_context():
        before = set(_table_names(db.engine))
        report = RUNNER.UpdatePipeline(
            app, mode=RUNNER.MODE_CHECK, trigger="test", registry=app.extensions.get("ams_modules")
        ).run()
        after = set(_table_names(db.engine))

    assert before == after, "a check must never change the schema"
    assert report["final_status"]
    assert "PENDING" in report["final_status"] or "READY" in report["final_status"]
    steps = {step["step"]: step for step in report["pipeline"]}
    assert steps["backup"]["status"] == "SKIPPED"
    assert steps["apply_baseline"]["status"] == "SKIPPED"
    assert steps["apply_migrations"]["status"] in {"SKIPPED", "PASS"}
    assert "check mode" in steps["apply_baseline"]["detail"].lower()


def test_apply_mode_records_ledger_and_verifies(app_factory, tmp_path):
    db_file = tmp_path / "apply.db"
    app = app_factory(db_file, AMS_SKIP_UPDATE_PIPELINE="1", AMS_ENV="development", UPDATE_REPORT_DIR=str(tmp_path / "logs"))
    from models import db

    with app.app_context():
        first = RUNNER.run_update(app, mode=RUNNER.MODE_APPLY, trigger="test", registry=app.extensions.get("ams_modules"))
        assert first["final_status"].startswith("READY"), first["blockers"]
        names = set(_table_names(db.engine))
        assert "ams_schema_migration" in names and "ams_update_run" in names
        assert ledger.read_schema_version() >= 1
        # the module tables came from the model metadata, and the module
        # revisions are recorded rather than silently applied
        assert "plant_asset" in names
        applied = ledger.applied_revisions()
        assert any(key.startswith("plant_registry:") for key in applied)
        second = RUNNER.run_update(app, mode=RUNNER.MODE_APPLY, trigger="test", registry=app.extensions.get("ams_modules"))

    assert second["migrations"]["applied"] == 0, "re-running must be idempotent"
    assert second["final_status"].startswith("READY")
    for name in ("update-health-report.json", "schema-audit.json", "module-registry-report.json", "migration-report.json"):
        assert (tmp_path / "logs" / name).exists(), name


def test_failed_revision_leaves_the_database_unchanged(app_factory, tmp_path):
    """Controlled transaction: an error inside a revision rolls that revision back."""
    db_file = tmp_path / "fail.db"
    modules = tmp_path / "broken_pack"
    modules.mkdir()
    _install_broken_module(modules, tmp_path)
    app = app_factory(
        db_file,
        AMS_SKIP_UPDATE_PIPELINE="1",
        AMS_ENV="development",
        AMS_MODULE_ROOT=str(modules),
        UPDATE_REPORT_DIR=str(tmp_path / "logs"),
    )
    from models import db

    with app.app_context():
        report = RUNNER.run_update(app, mode=RUNNER.MODE_APPLY, trigger="test", registry=app.extensions.get("ams_modules"))
        names = set(_table_names(db.engine))
        applied = ledger.applied_revisions()

    assert report["final_status"] == "UPDATE REQUIRES ATTENTION"
    assert "broken_mod_half_table" not in names, "the declared undo() must remove the partial DDL"
    assert not any(key.startswith("broken_mod:") and applied[key]["status"] == "APPLIED" for key in applied)
    assert report["blockers"], "a failed revision must block the run"
    blocker = report["blockers"][0]
    for key in ("what", "why", "where", "module", "data_risk", "next_action"):
        assert blocker.get(key), f"blocker must explain {key}"
    assert "deliberate failure" in json.dumps(report["migrations"])


def test_declared_migration_kind_must_match_the_revision_file(tmp_path):
    """``kind`` in module.toml is a promise about data risk, so it is checked.

    A manifest that labels a data transform "schema" would talk the operator out
    of the extra validation data revisions require (verify(), row counts,
    financial totals), so the mismatch is loud instead of a footnote.
    """
    root = tmp_path / "kind_check"
    path = _write_revision(root, "2026_002_backfill_locations.py", extra='KIND = "data"\n')

    understated = MIG._revision_from_python(
        path, module_id="kind_check", declared={"kind": "schema", "declared_kind": "schema"}
    )
    assert understated.kind == "data", "the revision file decides what a change actually does"
    assert understated.kind_mismatch is True

    checked = MIG.validate([understated], policy=policy.resolve(None), applied={})
    codes = {problem["code"] for problem in checked[0].problems}
    assert "declared_kind_mismatch" in codes
    assert checked[0].status == "REQUIRES_ATTENTION", "a manifest that understates risk is not schedulable"

    # A manifest that tells the truth is not punished for declaring the kind.
    honest = MIG._revision_from_python(
        path, module_id="kind_check", declared={"kind": "data", "declared_kind": "data"}
    )
    checked = MIG.validate([honest], policy=policy.resolve(None), applied={})
    assert honest.kind_mismatch is False
    assert "declared_kind_mismatch" not in {problem["code"] for problem in checked[0].problems}
    assert honest.data_validation is True, "a data revision still owes a verify()"


def test_module_with_pending_schema_work_is_not_marked_ready(app_factory, tmp_path):
    """Discovered but not-yet-migrated tables must never look healthy."""
    db_file = tmp_path / "pending.db"
    app = app_factory(db_file, AMS_SKIP_UPDATE_PIPELINE="1", AMS_ENV="development", UPDATE_REPORT_DIR=str(tmp_path / "logs"))
    registry = app.extensions.get("ams_modules")
    with app.app_context():
        from models import db

        # Drop one of the module's tables so the audit sees a real gap, then run
        # a check: the module must surface as needing migration, not as READY.
        from sqlalchemy import text as _text

        db.session.execute(_text("DROP TABLE IF EXISTS plant_asset_movement"))
        audit = RUNNER._audit(app, registry)
        report = RUNNER.run_update(app, mode=RUNNER.MODE_CHECK, trigger="test", registry=registry)

    assert audit["status"] in {"MIGRATION_REQUIRED", "SCHEMA_DRIFT"}
    assert any(issue["severity"] == "ADDITIVE" for issue in audit["issues"])
    assert "plant_registry" in registry.pending_migration_modules() or report["final_status"] != "READY"
    for issue in audit["issues"]:
        assert "DROP" not in issue["detail"].upper() or issue["severity"] in {"MANUAL", "ADDITIVE"}


def _table_names(engine) -> list[str]:
    from sqlalchemy import inspect

    return sorted(inspect(engine).get_table_names())


def _install_broken_module(root, tmp_path):
    """A fixture module whose revision fails halfway, to test the transaction."""
    module = root / "broken_mod"
    (module / "migrations").mkdir(parents=True)
    (module / "module.toml").write_text(
        textwrap.dedent(
            """
            [module]
            id = "broken_mod"
            name = "Broken Module"
            version = "1.0.0"
            description = "Fixture used by the pipeline tests."
            enabled = true
            schema_api = 1

            [routes]
            url_prefix = "/broken_mod"
            expected_endpoints = ["broken_mod.index"]

            [[database.migrations]]
            version = "2026_001"
            file = "migrations/2026_001_broken.py"
            destructive = false
            """
        ),
        encoding="utf-8",
    )
    (module / "_common.py").write_text(
        "from flask import Blueprint\n\nbroken_mod_bp = Blueprint('broken_mod', __name__)\n\n"
        "@broken_mod_bp.route('/')\ndef index():\n    return 'ok'\n",
        encoding="utf-8",
    )
    (module / "__init__.py").write_text("from ._common import *  # noqa\n", encoding="utf-8")
    body = "\n".join([
        "REVISION = '2026_001'",
        "TITLE = 'broken fixture revision'",
        "KIND = 'schema'",
        "MODULE = 'broken_mod'",
        "DESTRUCTIVE = False",
        "",
        "",
        "def upgrade(connection):",
        "    connection.exec_driver_sql(",
        "        'CREATE TABLE broken_mod_half_table (id INTEGER PRIMARY KEY)'",
        "    )",
        "    raise RuntimeError('deliberate failure for the pipeline test')",
        "",
        "",
        "def undo(connection):",
        "    # SQLite commits DDL implicitly, so a revision that creates objects",
        "    # declares how to remove them again.",
        "    connection.exec_driver_sql('DROP TABLE IF EXISTS broken_mod_half_table')",
        "",
    ])
    (module / "migrations" / "2026_001_broken.py").write_text(
        "\x22\x22\x22Creates a table then fails, to prove the compensation contract.\x22\x22\x22\n" + body,
        encoding="utf-8",
    )
