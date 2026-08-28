"""Three-way schema audit: what the models expect, what the ledger claims, and
what the database actually contains.

The distinction matters.  ``db.create_all()`` can only ever say "this table is
missing"; it cannot tell you that a column is absent, that a column changed
type, that an index was never created, or that a migration was applied to one
environment and not another.  This module compares all three sources and
classifies every difference:

  ADDITIVE      a table/column/index a migration can safely create
  MANUAL        a type change / rename SQLite cannot do in place
  DESTRUCTIVE   code dropped a column or table (data is reported, never removed)
  OK            nothing to do

It is read-only: it opens no write transaction and issues no DDL.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

LOG = logging.getLogger("ams.dbupdate.audit")

SAFETY_ADDITIVE = "ADDITIVE"
SAFETY_MANUAL = "MANUAL"
SAFETY_DESTRUCTIVE = "DESTRUCTIVE"
SAFETY_OK = "OK"

_TEXTUAL = ("VARCHAR", "CHAR", "TEXT", "CLOB", "STRING")
_NUMERIC = ("INT", "BIGINT", "SMALLINT", "INTEGER", "NUMERIC", "DECIMAL", "REAL", "FLOAT", "DOUBLE")


def _affinity(declared: str) -> str:
    declared = (declared or "").upper()
    if any(token in declared for token in _TEXTUAL):
        return "TEXT"
    if "BLOB" in declared or declared == "":
        return "BLOB"
    if "REAL" in declared or "FLOA" in declared or "DOUB" in declared:
        return "REAL"
    if any(token in declared for token in _NUMERIC):
        return "NUMERIC" if ("NUM" in declared or "DEC" in declared) else "INTEGER"
    return "BLOB"


def _column_signature(coltype: Any, nullable: bool) -> dict:
    return {
        "declared": str(coltype),
        "affinity": _affinity(str(coltype)),
        "nullable": bool(nullable),
    }


def _actual_columns(inspector, table_name: str) -> dict[str, dict]:
    try:
        rows = inspector.get_columns(table_name)
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        out[str(row["name"])] = {
            "declared": str(row.get("type") or ""),
            "affinity": _affinity(str(row.get("type") or "")),
            "nullable": bool(row.get("nullable", True)),
            "default": row.get("default"),
        }
    return out


def inspect_database(bind=None) -> dict:
    """Raw structural snapshot of the SQLite database behind *bind*."""
    from models import db

    engine = bind or db.engine
    inspector = sa_inspect(engine)
    tables = set(inspector.get_table_names())
    snapshot: dict[str, dict] = {}
    for name in sorted(tables):
        columns = _actual_columns(inspector, name)
        try:
            indexes = inspector.get_indexes(name) or []
        except Exception:
            indexes = []
        try:
            fks = inspector.get_foreign_keys(name) or []
        except Exception:
            fks = []
        try:
            pk = inspector.get_pk_constraint(name) or {}
        except Exception:
            pk = {}
        snapshot[name] = {
            "columns": columns,
            "indexes": {str(idx.get("name")): idx for idx in indexes if idx.get("name")},
            "foreign_keys": [
                {
                    "constrained": [str(c) for c in (fk.get("constrained_columns") or [])],
                    "referred_table": str(fk.get("referred_table") or ""),
                    "referred_columns": [str(c) for c in (fk.get("referred_columns") or [])],
                }
                for fk in fks
            ],
            "primary_key": [str(c) for c in ((pk or {}).get("constrained_columns") or [])],
            "unique_constraints": [
                str(u.get("name") or ",".join(u.get("column_names") or []))
                for u in (inspector.get_unique_constraints(name) or [])
            ],
        }
    return {"tables": snapshot, "table_names": sorted(tables)}


def audit(bind=None, *, registry=None, expected_revision: int | None = None) -> dict:
    """Compare ORM metadata against the live schema and classify each drift."""
    from models import db

    engine = bind or db.engine
    metadata = db.metadata
    actual = inspect_database(engine)
    live_tables = set(actual["table_names"])
    issues: list[dict] = []

    module_tables = _module_owned_tables(registry)

    for table_name, table in metadata.tables.items():
        declared_columns = {col.name: col for col in table.columns}
        if table_name not in live_tables:
            issues.append(
                {
                    "kind": "missing_table",
                    "severity": SAFETY_ADDITIVE if not table_name.startswith("ams_") else SAFETY_ADDITIVE,
                    "table": table_name,
                    "object": table_name,
                    "detail": "declared by the application models but absent from the database",
                    "owner": module_tables.get(table_name, "core"),
                    "fix": "create it through a migration (db.create_all covers a fresh database only)",
                }
            )
            continue
        snapshot = actual["tables"][table_name]
        live_columns = snapshot["columns"]
        for column_name, column in declared_columns.items():
            expected = _column_signature(column.type, column.nullable)
            live = live_columns.get(column_name)
            if live is None:
                issues.append(
                    {
                        "kind": "missing_column",
                        "severity": SAFETY_ADDITIVE,
                        "table": table_name,
                        "object": f"{table_name}.{column_name}",
                        "detail": f"expected {expected['declared']}"
                        f"{' NOT NULL' if not expected['nullable'] else ''} and the column does not exist",
                        "owner": module_tables.get(table_name, "core"),
                        "fix": f"ALTER TABLE {table_name} ADD COLUMN {column_name} {expected['declared']}"
                        + ("" if expected["nullable"] else " /* backfill before NOT NULL */"),
                    }
                )
                continue
            if live["affinity"] != expected["affinity"]:
                issues.append(
                    {
                        "kind": "changed_column_type",
                        "severity": SAFETY_MANUAL,
                        "table": table_name,
                        "object": f"{table_name}.{column_name}",
                        "detail": f"database affinity {live['affinity']} ({live['declared']}) vs model "
                        f"{expected['affinity']} ({expected['declared']})",
                        "owner": module_tables.get(table_name, "core"),
                        "fix": "SQLite cannot ALTER COLUMN: rebuild the table in a reviewed migration, or "
                        "accept the difference if the affinities store the same values",
                    }
                )
            if not expected["nullable"] and live["nullable"]:
                issues.append(
                    {
                        "kind": "missing_not_null",
                        "severity": SAFETY_MANUAL,
                        "table": table_name,
                        "object": f"{table_name}.{column_name}",
                        "detail": "model requires a value but the column is nullable (older additive upgrade)",
                        "owner": module_tables.get(table_name, "core"),
                        "fix": "informational: SQLite cannot add NOT NULL in place; validate in code instead",
                    }
                )
        for column_name in sorted(set(live_columns) - set(declared_columns)):
            issues.append(
                {
                    "kind": "orphan_column",
                    "severity": SAFETY_DESTRUCTIVE,
                    "table": table_name,
                    "object": f"{table_name}.{column_name}",
                    "detail": "the database has this column but no model declares it any more",
                    "owner": module_tables.get(table_name, "core"),
                    "fix": "leave the column in place (its data is historical) or write a reviewed "
                    "table-rebuild migration; nothing here will drop it",
                }
            )
        # Indexes declared on the model (index=True or explicit Index()).
        expected_index_names = {idx.name for idx in table.indexes if idx.name}
        for column in table.columns:
            if column.index and column.name:
                expected_index_names.add(f"ix_{table_name}_{column.name}")
        for index_name in sorted(n for n in expected_index_names if n):
            if index_name not in snapshot["indexes"]:
                issues.append(
                    {
                        "kind": "missing_index",
                        "severity": SAFETY_ADDITIVE,
                        "table": table_name,
                        "object": index_name,
                        "detail": "index declared by the model is not present in the database",
                        "owner": module_tables.get(table_name, "core"),
                        "fix": f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} (...)",
                    }
                )
        # Foreign keys from the model.
        live_fk_pairs = {(tuple(fk["constrained"]), fk["referred_table"]) for fk in snapshot["foreign_keys"]}
        for constraint in table.foreign_key_constraints:
            # ForeignKeyConstraint.elements are the ForeignKey objects, whose
            # .parent is the child column and .column the referenced column.
            constrained = tuple(fk.parent.name for fk in constraint.elements)
            referred_tables = {
                fk.column.table.name for fk in constraint.elements if fk.column is not None
            }
            referred = sorted(referred_tables)[0] if referred_tables else (
                constraint.referred_table.name if constraint.referred_table is not None else ""
            )
            if (constrained, referred) not in live_fk_pairs:
                issues.append(
                    {
                        "kind": "missing_foreign_key",
                        "severity": SAFETY_MANUAL,
                        "table": table_name,
                        "object": f"{table_name}.{'_'.join(constrained)} -> {referred}",
                        "detail": "relationship declared in the model has no FK constraint in the database "
                        "(typical for columns added by ALTER TABLE)",
                        "owner": module_tables.get(table_name, "core"),
                        "fix": "enforced by application queries today; a table rebuild is the only way to "
                        "add it in SQLite — recorded so it is not forgotten",
                    }
                )

    for table_name in sorted(live_tables - set(metadata.tables)):
        issues.append(
            {
                "kind": "unmanaged_table",
                "severity": SAFETY_OK,
                "table": table_name,
                "object": table_name,
                "detail": "present in the database but not declared by any model",
                "owner": module_tables.get(table_name, "core"),
                "fix": "usually a legacy table or a module model that is not imported at startup; "
                "no action taken automatically",
            }
        )

    actionable = [i for i in issues if i["severity"] != SAFETY_OK]
    manual = [i for i in issues if i["severity"] == SAFETY_MANUAL]
    destructive = [i for i in issues if i["severity"] == SAFETY_DESTRUCTIVE]
    from app.services.dbupdate import ledger

    current_version = ledger.read_schema_version()
    expected_version = int(expected_revision) if expected_revision is not None else current_version
    if current_version < expected_version:
        status = "MIGRATION_REQUIRED"
    elif actionable:
        status = "SCHEMA_DRIFT"
    else:
        status = "OK"
    return {
        "status": status,
        "expected_schema_version": max(expected_version, current_version),
        "current_schema_version": current_version,
        "tables_expected": len(metadata.tables),
        "tables_present": len(live_tables),
        "counts": {
            "additive": len([i for i in issues if i["severity"] == SAFETY_ADDITIVE]),
            "manual": len(manual),
            "destructive": len(destructive),
            "informational": len([i for i in issues if i["severity"] == SAFETY_OK]),
        },
        "pending_by_severity": {
            SAFETY_ADDITIVE: sorted({i["object"] for i in issues if i["severity"] == SAFETY_ADDITIVE}),
            SAFETY_MANUAL: sorted({i["object"] for i in manual}),
            SAFETY_DESTRUCTIVE: sorted({i["object"] for i in destructive}),
        },
        "issues": issues,
        "summary": {
            "issue_count": len(actionable),
            "manual_required": bool(manual),
            "data_at_risk": bool(destructive),
        },
    }


def _module_owned_tables(registry) -> dict[str, str]:
    """``{table: module_id}`` for every table a module declared as its own."""
    owned: dict[str, str] = {}
    if registry is None:
        return owned
    for spec in registry.specs.values():
        for table in spec.tables:
            owned[table] = spec.module_id
    return owned


def quick_summary(bind=None) -> dict:
    """Cheap counts for logs/health: tables, columns, indexes, FK violations."""
    from models import db

    engine = bind or db.engine
    with engine.connect() as connection:
        tables = int(
            connection.execute(
                text("SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            ).scalar()
            or 0
        )
        indexes = int(
            connection.execute(
                text("SELECT count(*) FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
            ).scalar()
            or 0
        )
        try:
            fk_violations = len(connection.execute(text("PRAGMA foreign_key_check")).fetchall())
        except Exception:
            fk_violations = -1
        try:
            integrity = str(connection.execute(text("PRAGMA integrity_check")).fetchone()[0])
        except Exception:
            integrity = "unavailable"
    return {
        "tables": tables,
        "indexes": indexes,
        "foreign_key_violations": fk_violations,
        "integrity_check": integrity,
        "database": str(engine.url.database),
    }
