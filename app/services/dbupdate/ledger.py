"""The schema/data migration ledger: what ran, when, why, and against what.

Two tables, created idempotently by :func:`ensure_ledger` (they cannot rely on
``db.create_all`` because the ledger has to exist *before* the first migration
it records):

``ams_schema_migration``
    One row per revision attempt — APPLIED / FAILED / SKIPPED, with checksum,
    backup reference, row impact and the verification report.  Unique on the
    global ``revision`` so the same change can never be applied twice.

``ams_update_run``
    One row per update *pipeline run*: environment, policy, module counts,
    migration counts, integrity/regression outcome and the final verdict.

Both are append-only from the application's point of view: nothing in this
module issues UPDATE or DELETE against historical rows.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

LOG = logging.getLogger("ams.dbupdate.ledger")

MIGRATION_TABLE = "ams_schema_migration"
RUN_TABLE = "ams_update_run"

STATUS_APPLIED = "APPLIED"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"
STATUS_PLANNED = "PLANNED"

_LEDGER_DDL = f"""
CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision VARCHAR(64) NOT NULL UNIQUE,
    module_id VARCHAR(64) NOT NULL DEFAULT 'core',
    version VARCHAR(48) NOT NULL,
    slug VARCHAR(160) NOT NULL,
    filename VARCHAR(255) NOT NULL,
    kind VARCHAR(24) NOT NULL DEFAULT 'schema',
    checksum VARCHAR(64) NOT NULL DEFAULT '',
    status VARCHAR(16) NOT NULL,
    attempted_at VARCHAR(40) NOT NULL,
    completed_at VARCHAR(40),
    duration_ms INTEGER,
    app_version VARCHAR(32) DEFAULT '',
    schema_version_before INTEGER DEFAULT 0,
    schema_version_after INTEGER DEFAULT 0,
    backup_path VARCHAR(400),
    backup_sha256 VARCHAR(64),
    affected_rows INTEGER DEFAULT 0,
    report_json TEXT DEFAULT '{{}}',
    error TEXT DEFAULT ''
)
"""

_LEDGER_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS ix_{MIGRATION_TABLE}_module ON {MIGRATION_TABLE} (module_id)",
    f"CREATE INDEX IF NOT EXISTS ix_{MIGRATION_TABLE}_status ON {MIGRATION_TABLE} (status)",
    f"CREATE INDEX IF NOT EXISTS ix_{MIGRATION_TABLE}_attempted ON {MIGRATION_TABLE} (attempted_at)",
)

_RUN_DDL = f"""
CREATE TABLE IF NOT EXISTS {RUN_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key VARCHAR(48) NOT NULL UNIQUE,
    started_at VARCHAR(40) NOT NULL,
    finished_at VARCHAR(40),
    environment VARCHAR(24) NOT NULL DEFAULT '',
    policy VARCHAR(24) NOT NULL DEFAULT '',
    mode VARCHAR(24) NOT NULL DEFAULT 'check',
    final_status VARCHAR(24) NOT NULL DEFAULT 'RUNNING',
    modules_discovered INTEGER DEFAULT 0,
    modules_registered INTEGER DEFAULT 0,
    modules_failed INTEGER DEFAULT 0,
    migrations_detected INTEGER DEFAULT 0,
    migrations_applied INTEGER DEFAULT 0,
    migrations_failed INTEGER DEFAULT 0,
    backup_path VARCHAR(400),
    backup_sha256 VARCHAR(64),
    schema_version_before INTEGER DEFAULT 0,
    schema_version_after INTEGER DEFAULT 0,
    schema_validation VARCHAR(24) DEFAULT '',
    integrity_status VARCHAR(24) DEFAULT '',
    regression_status VARCHAR(24) DEFAULT '',
    report_json TEXT DEFAULT '{{}}',
    notes TEXT DEFAULT ''
)
"""

_COLUMNS = (
    "revision",
    "module_id",
    "version",
    "slug",
    "filename",
    "kind",
    "checksum",
    "status",
    "attempted_at",
    "completed_at",
    "duration_ms",
    "app_version",
    "schema_version_before",
    "schema_version_after",
    "backup_path",
    "backup_sha256",
    "affected_rows",
    "report_json",
    "error",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _scalar_result(value):
    return value


def exists(session=None) -> bool:
    """Is the migration ledger already present? (read-only question)"""
    from models import db

    scope = session or db.session
    try:
        row = scope.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name = :name"),
            {"name": MIGRATION_TABLE},
        ).first()
        return bool(row)
    except Exception:
        return False


def ensure_ledger(session=None, *, allow_create: bool = True) -> bool:
    """Create the ledger tables/indexes if absent. Safe to call every boot.

    Returns whether the ledger is usable.  ``allow_create=False`` is what a
    check-only run uses: a read-only command must not issue DDL.
    """
    from models import db

    scope = session or db.session
    if not allow_create:
        return exists(scope)
    try:
        scope.execute(text(_LEDGER_DDL))
        for statement in _LEDGER_INDEXES:
            scope.execute(text(statement))
        scope.execute(text(_RUN_DDL))
        scope.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{RUN_TABLE}_started ON {RUN_TABLE} (started_at)"))
        scope.commit()
    except Exception:
        scope.rollback()
        LOG.exception("migration ledger could not be created; update history will not be recorded")
        raise
    return True


def import_legacy_history(session=None, *, allow_write: bool = True) -> int:
    """Adopt rows from the older ``migration_history`` ledger (once).

    ``blueprints/import_export`` recorded applied ``.sql`` files in a table with
    only ``filename``/``applied_at``.  Those changes really happened, so they
    must be marked applied here — otherwise the new runner would try to replay
    them against a live database.
    """
    from models import db

    scope = session or db.session
    try:
        exists = scope.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='migration_history'")
        ).first()
        if not exists:
            return 0
        rows = scope.execute(text("SELECT filename, applied_at FROM migration_history")).fetchall()
        if not allow_write or not rows:
            return 0
        adopted = 0
        for filename, applied_at in rows:
            name = str(filename or "").strip()
            if not name:
                continue
            revision = f"legacy:{name}"
            already = scope.execute(
                text(f"SELECT id FROM {MIGRATION_TABLE} WHERE revision = :revision"), {"revision": revision}
            ).first()
            if already:
                continue
            scope.execute(
                text(
                    f"INSERT INTO {MIGRATION_TABLE} (revision, module_id, version, slug, filename, kind,"
                    " checksum, status, attempted_at, completed_at, report_json, error)"
                    " VALUES (:revision, 'core', :version, :slug, :filename, 'schema', '',"
                    " 'APPLIED', :attempted, :attempted, :report, '')"
                ),
                {
                    "revision": revision,
                    "version": name,
                    "slug": name,
                    "filename": name,
                    "attempted": str(applied_at or "unknown (imported from migration_history)"),
                    "report": json.dumps(
                        {"imported_from": "migration_history", "note": "recorded by the legacy app-upgrade runner"},
                        ensure_ascii=False,
                    ),
                },
            )
            adopted += 1
        scope.commit()
        if adopted:
            LOG.info("adopted %d legacy migration_history row(s) into the update ledger", adopted)
        return adopted
    except Exception:
        scope.rollback()
        LOG.warning("legacy migration_history import skipped", exc_info=True)
        return 0


def applied_revisions(session=None) -> dict[str, dict]:
    """Every revision that has already been applied, by revision id."""
    from models import db

    scope = session or db.session
    try:
        rows = scope.execute(
            text(
                f"SELECT revision, module_id, version, checksum, status, completed_at, kind"
                f" FROM {MIGRATION_TABLE} WHERE status = '{STATUS_APPLIED}'"
            )
        ).fetchall()
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        out[row[0]] = {
            "revision": row[0],
            "module_id": row[1],
            "version": row[2],
            "checksum": row[3] or "",
            "status": row[4],
            "completed_at": row[5],
            "kind": row[6],
        }
    return out


def record(
    *,
    revision: str,
    module_id: str,
    version: str,
    slug: str,
    filename: str,
    kind: str,
    checksum: str,
    status: str,
    duration_ms: int | None = None,
    app_version: str = "",
    schema_version_before: int = 0,
    schema_version_after: int = 0,
    backup_path: str | None = None,
    backup_sha256: str | None = None,
    affected_rows: int = 0,
    report: dict | None = None,
    error: str = "",
    session=None,
) -> None:
    """Insert or refresh one revision row. Never touches other revisions."""
    from models import db

    scope = session or db.session
    now = _now()
    payload = {
        "revision": revision,
        "module_id": module_id,
        "version": version,
        "slug": slug,
        "filename": filename,
        "kind": kind,
        "checksum": checksum,
        "status": status,
        "attempted_at": now,
        "completed_at": now if status == STATUS_APPLIED else None,
        "duration_ms": duration_ms,
        "app_version": app_version,
        "schema_version_before": schema_version_before,
        "schema_version_after": schema_version_after,
        "backup_path": backup_path,
        "backup_sha256": backup_sha256,
        "affected_rows": affected_rows,
        "report_json": json.dumps(report or {}, ensure_ascii=False, default=str),
        "error": (error or "")[:4000],
    }
    try:
        existing = scope.execute(
            text(f"SELECT id, status FROM {MIGRATION_TABLE} WHERE revision = :revision"),
            {"revision": revision},
        ).first()
        if existing:
            assignments = ", ".join(f"{column} = :{column}" for column in _COLUMNS)
            scope.execute(text(f"UPDATE {MIGRATION_TABLE} SET {assignments} WHERE id = :id"), {**payload, "id": existing[0]})
        else:
            columns = ", ".join(_COLUMNS)
            placeholders = ", ".join(f":{column}" for column in _COLUMNS)
            scope.execute(text(f"INSERT INTO {MIGRATION_TABLE} ({columns}) VALUES ({placeholders})"), payload)
        scope.commit()
    except Exception:
        scope.rollback()
        LOG.exception("could not record migration '%s'", revision)
        raise


def history(limit: int = 50, session=None) -> list[dict]:
    from models import db

    scope = session or db.session
    try:
        rows = scope.execute(
            text(
                f"SELECT revision, module_id, version, kind, status, completed_at, duration_ms,"
                f" backup_path, affected_rows, schema_version_before, schema_version_after, error"
                f" FROM {MIGRATION_TABLE} ORDER BY id DESC LIMIT :limit"
            ),
            {"limit": int(limit)},
        ).mappings().all()
        return [dict(row) for row in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# pipeline runs
# ---------------------------------------------------------------------------

def begin_run(
    *,
    run_key: str,
    environment: str,
    policy: str,
    mode: str,
    session=None,
) -> None:
    from models import db

    scope = session or db.session
    try:
        scope.execute(
            text(
                f"INSERT OR IGNORE INTO {RUN_TABLE} (run_key, started_at, environment, policy, mode, final_status)"
                " VALUES (:run_key, :started_at, :environment, :policy, :mode, 'RUNNING')"
            ),
            {
                "run_key": run_key,
                "started_at": _now(),
                "environment": environment,
                "policy": policy,
                "mode": mode,
            },
        )
        scope.commit()
    except Exception:
        scope.rollback()
        LOG.warning("update run %s could not be opened", run_key, exc_info=True)


def finish_run(*, run_key: str, report: dict, session=None) -> None:
    from models import db

    scope = session or db.session
    backup = report.get("backup") if isinstance(report.get("backup"), dict) else {}
    migrations = report.get("migrations") if isinstance(report.get("migrations"), dict) else {}
    modules = report.get("modules") if isinstance(report.get("modules"), dict) else {}
    columns = {
        "run_key": run_key,
        "finished_at": _now(),
        "environment": report.get("environment", ""),
        "policy": report.get("policy", ""),
        "mode": report.get("mode", "check"),
        "final_status": report.get("final_status", "UNKNOWN"),
        "modules_discovered": int(modules.get("discovered") or report.get("modules_discovered") or 0),
        "modules_registered": int(modules.get("registered") or report.get("modules_registered") or 0),
        "modules_failed": int(modules.get("failed") or report.get("modules_failed") or 0),
        "migrations_detected": int(migrations.get("total_revisions") or report.get("migrations_detected") or 0),
        "migrations_applied": int(migrations.get("applied") or report.get("migrations_applied") or 0),
        "migrations_failed": int(migrations.get("failed") or report.get("migrations_failed") or 0),
        "backup_path": str(backup.get("path") or "")[:400],
        "backup_sha256": str(backup.get("sha256") or "")[:64],
        "schema_version_before": int(report.get("schema_version_before") or 0),
        "schema_version_after": int(report.get("schema_version_after") or 0),
        "schema_validation": str(((report.get("checks") or {}).get("schema_validation") or {}).get("status", "")),
        "integrity_status": str(((report.get("checks") or {}).get("data_integrity") or {}).get("status", "")),
        "regression_status": str(((report.get("checks") or {}).get("regression") or {}).get("status", "")),
        "report_json": json.dumps(report, ensure_ascii=False, default=str)[:2_000_000],
        "notes": "; ".join(str(note) for note in (report.get("notes") or []))[:4000],
    }
    try:
        updates = ", ".join(f"{key} = :{key}" for key in columns if key != "run_key")
        scope.execute(text(f"UPDATE {RUN_TABLE} SET {updates} WHERE run_key = :run_key"), columns)
        scope.commit()
    except Exception:
        scope.rollback()
        LOG.exception("update run %s could not be closed", run_key)


def recent_runs(limit: int = 10, session=None) -> list[dict]:
    from models import db

    scope = session or db.session
    try:
        rows = scope.execute(
            text(
                f"SELECT run_key, started_at, finished_at, environment, policy, mode, final_status,"
                f" migrations_applied, migrations_failed, backup_path, schema_version_after,"
                f" schema_validation, integrity_status, regression_status"
                f" FROM {RUN_TABLE} ORDER BY id DESC LIMIT :limit"
            ),
            {"limit": int(limit)},
        ).mappings().all()
        return [dict(row) for row in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# schema version (PRAGMA user_version mirror)
# ---------------------------------------------------------------------------

def read_schema_version(session=None) -> int:
    from models import db

    scope = session or db.session
    try:
        row = scope.execute(text("PRAGMA user_version")).first()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def write_schema_version(value: int, session=None) -> int:
    from models import db

    scope = session or db.session
    try:
        scope.execute(text(f"PRAGMA user_version={int(value)}"))
        scope.commit()
        return int(value)
    except Exception:
        scope.rollback()
        return read_schema_version(scope)
