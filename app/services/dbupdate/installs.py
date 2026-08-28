"""Module install tracking: which module, which version, which state, in the DB.

The registry is authoritative while the process runs; this table is what makes
the same facts visible *between* runs and to SQL tooling — so "which module
version required this schema" is answerable months later, and an operator can
see a module that appeared, disappeared or failed without reading log files.

Append-only from the application's side: rows are inserted once per module and
updated in place (never deleted), and the previous version is kept in
``previous_version`` for traceability.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

LOG = logging.getLogger("ams.dbupdate.installs")

TABLE = "ams_module_install"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(160) NOT NULL DEFAULT '',
    version VARCHAR(32) NOT NULL DEFAULT '',
    previous_version VARCHAR(32) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT '',
    url_prefix VARCHAR(160) DEFAULT '',
    depends_on TEXT DEFAULT '[]',
    tables TEXT DEFAULT '[]',
    migrations TEXT DEFAULT '[]',
    problems TEXT DEFAULT '[]',
    last_health VARCHAR(24) DEFAULT '',
    installed_at VARCHAR(40),
    updated_at VARCHAR(40),
    install_count INTEGER NOT NULL DEFAULT 1
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure(session=None) -> None:
    from models import db

    scope = session or db.session
    try:
        scope.execute(text(_DDL))
        scope.commit()
    except Exception:
        scope.rollback()
        raise


def record_installs(app, registry, *, health: dict | None = None) -> dict:
    """Upsert one row per known module.  Returns ``{inserted, updated}``."""
    if registry is None:
        return {"inserted": 0, "updated": 0}
    from models import db

    inserted = updated = 0
    now = _now()
    try:
        with app.app_context():
            ensure()
            existing = {
                row[0]: {"version": row[1], "id": row[2]}
                for row in db.session.execute(text(f"SELECT module_id, version, id FROM {TABLE}")).fetchall()
            }
            for spec in registry.specs.values():
                payload = {
                    "module_id": spec.module_id,
                    "name": spec.name[:160],
                    "version": spec.version[:32],
                    "status": spec.status[:32],
                    "url_prefix": (spec.url_prefix or "")[:160],
                    "depends_on": json.dumps(list(spec.depends_on)),
                    "tables": json.dumps(list(spec.tables)),
                    "migrations": json.dumps(
                        [f"{spec.module_id}:{ref.version}" for ref in list(spec.migrations) + list(spec.data_migrations)]
                    ),
                    "problems": json.dumps([problem.as_dict() for problem in spec.problems][:20]),
                    "last_health": ((health or {}).get("modules", {}).get(spec.module_id, {}) or {}).get("status", "")[:24],
                    "updated_at": now,
                }
                if spec.module_id in existing:
                    row = existing[spec.module_id]
                    payload["previous_version"] = row["version"] or ""
                    db.session.execute(
                        text(
                            f"UPDATE {TABLE} SET name=:name, version=:version, previous_version=:previous_version,"
                            " status=:status, url_prefix=:url_prefix, depends_on=:depends_on, tables=:tables,"
                            " migrations=:migrations, problems=:problems, last_health=:last_health,"
                            " updated_at=:updated_at, install_count=install_count+1 WHERE module_id=:module_id"
                        ),
                        payload,
                    )
                    updated += 1
                else:
                    payload["installed_at"] = now
                    db.session.execute(
                        text(
                            f"INSERT INTO {TABLE} (module_id, name, version, previous_version, status, url_prefix,"
                            " depends_on, tables, migrations, problems, last_health, installed_at, updated_at)"
                            " VALUES (:module_id, :name, :version, '', :status, :url_prefix, :depends_on, :tables,"
                            " :migrations, :problems, :last_health, :installed_at, :updated_at)"
                        ),
                        payload,
                    )
                    inserted += 1
            db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        # Losing the install log must never take the ERP down.
        LOG.warning("module install tracking could not be written", exc_info=True)
        return {"inserted": 0, "updated": 0, "status": "SKIPPED"}
    return {"inserted": inserted, "updated": updated}


def list_installs(session=None) -> list[dict]:
    from models import db

    scope = session or db.session
    try:
        rows = scope.execute(
            text(
                f"SELECT module_id, name, version, previous_version, status, url_prefix, depends_on,"
                f" tables, migrations, problems, last_health, installed_at, updated_at, install_count"
                f" FROM {TABLE} ORDER BY module_id"
            )
        ).mappings().all()
    except Exception:
        return []
    out = []
    for row in rows:
        item = dict(row)
        for key in ("depends_on", "tables", "migrations", "problems"):
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except (TypeError, ValueError):
                item[key] = []
        out.append(item)
    return out
