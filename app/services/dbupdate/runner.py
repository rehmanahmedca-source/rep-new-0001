"""The controlled database/module update pipeline.

``run_update()`` is the only thing that changes this application's schema.  It
implements the twelve steps in order and refuses to claim success without
verification::

    detect -> compare -> approved migration? -> validate -> dependencies
           -> backup -> apply (transaction) -> verify schema -> integrity
           -> module tests -> regression -> mark successful

Modes
-----
``check``   read-only.  Nothing is written except report files.  **Default.**
``plan``    like ``check``, plus an optional *rehearsal* on a throw-away copy of
            the database (:func:`preview_on_copy`) to prove a migration works.
``apply``   the real thing: backup first, apply, verify, and only then record.

A step that fails produces a structured blocker: what failed, why, where, the
affected module, the affected database object, the data risk and the next
action — and dependent work stops instead of continuing on a broken base.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from app.services.dbupdate import integrity as INTEGRITY
from app.services.dbupdate import ledger as LEDGER
from app.services.dbupdate import legacy_steps as LEGACY
from app.services.dbupdate import migrations as MIG
from app.services.dbupdate import reports as REPORTS
from app.services.dbupdate.policy import POLICY_AUDIT, resolve

LOG = logging.getLogger("ams.dbupdate")

MODE_CHECK = "check"
MODE_PLAN = "plan"
MODE_APPLY = "apply"

class Step:
    """One pipeline step: outcome, timing and a failure explanation."""

    def __init__(self, name: str, label: str) -> None:
        self.name = name
        self.label = label
        self.status = "PASS"
        self.detail = ""
        self.started = time.time()
        self.data: dict = {}
        self.blocker: dict | None = None

    def fail(self, *, what: str, why: str = "", where: str = "", module: str = "", database_object: str = "", data_risk: str = "", next_action: str = "") -> None:
        self.status = "FAIL"
        self.detail = what
        self.blocker = {
            "step": self.name,
            "what": what,
            "why": why,
            "where": where,
            "module": module,
            "database_object": database_object,
            "data_risk": data_risk,
            "next_action": next_action,
        }

    def warn(self, detail: str, **data) -> None:
        if self.status == "PASS":
            self.status = "WARN"
        self.detail = detail or self.detail
        self.data.update(data)

    def as_dict(self) -> dict:
        return {
            "step": self.name,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "duration_ms": int((time.time() - self.started) * 1000),
            "data": self.data,
        }


def _split_statements(sql: str) -> list[str]:
    import sqlite3

    statements: list[str] = []
    buffer = ""
    for line in (sql or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("--") and not buffer.strip():
            continue
        buffer += line + "\n"
        while sqlite3.complete_statement(buffer):
            statement = buffer.strip().rstrip(";").strip()
            buffer = ""
            if statement:
                statements.append(statement)
            break
    if buffer.strip():
        tail = buffer.strip().rstrip(";").strip()
        if tail:
            statements.append(tail)
    return statements


def _affected_row_estimate(connection, sql: str) -> int:
    try:
        row = connection.execute(text("SELECT changes()")).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


class _Skip(Exception):
    """Internal: a step deliberately does nothing in this mode (not a failure)."""


class UpdatePipeline:
    """Carries the state of one pipeline run."""

    def __init__(self, app, *, mode: str = MODE_CHECK, trigger: str = "manual", registry=None, include_legacy: bool | None = None, with_tests: bool = False, only: list[str] | None = None) -> None:
        self.app = app
        self.mode = mode if mode in (MODE_CHECK, MODE_PLAN, MODE_APPLY) else MODE_CHECK
        self.trigger = trigger
        self.registry = registry if registry is not None else app.extensions.get("ams_modules")
        self.policy = resolve(app)
        self.with_tests = bool(with_tests)
        self.only = list(only) if only else None
        self.include_legacy = self.mode == MODE_APPLY if include_legacy is None else bool(include_legacy)
        self.steps: list[Step] = []
        self.blockers: list[dict] = []
        self.notes: list[str] = []
        self.backup: dict = {"status": "NOT_REQUIRED", "path": "", "sha256": ""}
        self.before_counts: dict[str, int] = {}
        self.revision_results: list[dict] = []
        self.schema_before: dict = {}
        self.schema_after: dict = {}
        self.plan: dict = {}
        self.audit: dict = {}
        self.requirements: dict = {}
        self.unapproved_drift: list = []
        self.module_health: dict = {}
        self.registry_summary: dict = {}
        self.revisions: list = []
        self.schema_version_before = 0
        self.schema_version_expected = 0
        self.started = datetime.now(timezone.utc)
        self.run_key = f"update-{self.started.strftime('%Y%m%dT%H%M%SZ')}-{self.mode}"
        self.applied_count = 0
        self.failed_count = 0
        self.stopped = False
        #: set by the ledger step; false when history cannot be recorded
        self.ledger_usable = True
        #: whether the *policy* permits the legacy additive chain, independent of
        #: the mode — so a read-only check reports what an apply would decide.
        self.legacy_permitted = (self.mode == MODE_APPLY) if include_legacy is None else bool(include_legacy)

    # -- helpers ----------------------------------------------------------
    def step(self, name: str, label: str) -> Step:
        step = Step(name, label)
        self.steps.append(step)
        return step

    def _block(self, step: Step) -> None:
        if step.blocker:
            self.blockers.append(step.blocker)
            self.stopped = True
            LOG.error("update pipeline stopped at '%s': %s", step.name, step.blocker.get("what"))

    @property
    def can_write(self) -> bool:
        return self.mode == MODE_APPLY

    # -- pipeline ---------------------------------------------------------
    #: (method, step label) — executed in order.  Every entry is guarded so a
    #: single internal error is reported as a failed step instead of escaping
    #: into application startup.
    _SEQUENCE = (
        ("_step_discover", "discover_modules", "Module discovery and validation"),
        ("_step_ledger", "migration_ledger", "Ensure the update ledger exists"),
        ("_step_detect_requirements", "detect_requirements", "Detect schema requirements"),
        ("_step_compare_schema", "compare_schema", "Audit expected vs actual schema"),
        ("_step_locate_approved_migrations", "approved_migrations", "Confirm every change has an approved revision"),
        ("_step_validate_migrations", "validate_migrations", "Validate revisions (lint, checksum, policy)"),
        ("_step_dependencies", "dependencies", "Check module and revision dependencies"),
        ("_step_backup", "backup", "Back up the database before risky operations"),
        ("_step_baseline", "apply_baseline", "Additive baseline: create_all + legacy ensure steps"),
        ("_step_apply_migrations", "apply_migrations", "Apply approved revisions"),
        ("_step_verify_schema", "verify_schema", "Verify the resulting schema"),
        ("_step_integrity", "data_integrity", "Run data integrity checks"),
        ("_step_module_health", "module_health", "Run module health checks"),
        ("_step_navigation", "navigation", "Validate navigation (module + shared layout)"),
        ("_step_module_tests", "module_tests", "Run the affected modules' tests"),
        ("_step_regression", "regression", "Verify existing data and functionality are intact"),
    )

    def run(self) -> dict:
        for method_name, step_name, label in self._SEQUENCE:
            try:
                getattr(self, method_name)()
            except Exception as exc:  # defensive: report, never propagate
                LOG.exception("update step '%s' raised", step_name)
                step = next((s for s in self.steps if s.name == step_name), None)
                if step is None:
                    step = self.step(step_name, label)
                step.fail(
                    what=f"internal error in the update subsystem: {type(exc).__name__}: {exc}",
                    why="this step could not complete; no further dependent work was attempted",
                    where=method_name,
                    data_risk="unknown: the step aborted before it could report",
                    next_action="re-run with AMS_DEBUG_TRACEBACK=1 and read instance/logs/errorlog.txt",
                )
                self._block(step)
                break
        return self._finish()

    def _step_discover(self) -> None:
        step = self.step("discover_modules", "Module discovery and validation")
        if self.registry is None:
            step.warn("no module registry attached to this app")
            self.registry_summary = {"discovered": 0, "registered": 0, "failed": 0, "disabled": 0}
            return
        specs = list(self.registry.specs.values())
        failed = [s for s in specs if s.status in {"FAILED_VALIDATION", "MISSING_DEPENDENCY", "ROUTE_CONFLICT"}]
        self.registry_summary = {
            "discovered": len(specs),
            "registered": len(self.registry.registrations),
            "failed": len(failed),
            "disabled": len([s for s in specs if s.status == "DISABLED"]),
            "statuses": self.registry.statuses(),
        }
        if failed:
            # A broken module must be reported loudly and precisely, but it must
            # not stop the ERP's own schema update: the module was never mounted,
            # so its damage is contained to that feature.
            step.warn(
                f"{len(failed)} module(s) failed validation: "
                + "; ".join(
                    f"{spec.module_id} -> {(spec.errors()[0].message if spec.errors() else spec.status)}"
                    for spec in failed[:5]
                ),
                modules=[spec.module_id for spec in failed],
                why="a failing module is kept inactive; every other module and the core ERP continue to work",
                next_action="fix each manifest, see module-registry-report.json modules[].problems",
            )
            self.notes.append(f"{len(failed)} module(s) failed validation and were not activated")

    def _step_ledger(self) -> None:
        step = self.step("migration_ledger", "Ensure the update ledger exists")
        from models import db

        try:
            with self.app.app_context():
                usable = LEDGER.ensure_ledger(allow_create=self.can_write)
                adopted = LEDGER.import_legacy_history(allow_write=self.can_write) if usable else 0
            self.ledger_usable = bool(usable)
            if not usable:
                step.status = "WARN"
                step.detail = (
                    "migration ledger does not exist; a check-only run does not create it, "
                    "so applied-revision state is unknown until 'dbupdate.py apply' runs"
                )
                step.data = {"ledger_present": False, "mode": self.mode}
                return
            if adopted:
                step.data["adopted_legacy_rows"] = adopted
                self.notes.append(f"adopted {adopted} row(s) from the legacy migration_history table")
        except Exception as exc:
            self.ledger_usable = False
            step.fail(
                what=f"migration ledger unavailable: {type(exc).__name__}: {exc}",
                why="applied revisions could not be recorded, so re-application protection is lost",
                where="ams_schema_migration",
                data_risk="high: a migration could be replayed",
                next_action="check disk space and file permissions on the database, then retry",
            )
            self._block(step)

    def _step_detect_requirements(self) -> None:
        step = self.step("detect_requirements", "Detect schema requirements")
        from models import db

        try:
            with self.app.app_context():
                from sqlalchemy import inspect as sa_inspect

                live = set(sa_inspect(db.engine).get_table_names())
        except Exception as exc:
            step.fail(
                what=f"cannot inspect the database: {type(exc).__name__}: {exc}",
                why="the schema comparison needs a live connection",
                where=str(db.engine.url.database),
                data_risk="unknown",
                next_action="verify APP_DB_PATH / instance database availability",
            )
            self._block(step)
            return
        expected = set(db.metadata.tables)
        missing_tables = sorted(expected - live)
        module_needs: dict[str, list[str]] = {}
        if self.registry is not None:
            for spec in self.registry.specs.values():
                if not spec.enabled:
                    continue
                gaps = sorted(set(spec.tables) - live)
                if gaps or spec.migrations or spec.data_migrations:
                    module_needs[spec.module_id] = gaps
        self.requirements = {
            "missing_tables": missing_tables,
            "modules_with_schema_requirements": sorted(module_needs),
            "module_tables_missing": module_needs,
            "fresh_database": not live,
        }
        step.data = self.requirements
        if missing_tables:
            step.detail = f"{len(missing_tables)} table(s) expected but absent"

    def _step_compare_schema(self) -> None:
        step = self.step("compare_schema", "Audit expected vs actual schema")
        from models import db

        try:
            with self.app.app_context():
                current = LEDGER.read_schema_version()
                self.schema_before = _audit(self.app, self.registry)
        except Exception as exc:
            step.fail(
                what=f"schema audit failed: {type(exc).__name__}: {exc}",
                where="schema audit",
                data_risk="unknown",
                next_action="run tools/dbupdate.py audit-schema for the traceback",
            )
            self._block(step)
            return
        self.plan = MIG.plan(self.revisions) if hasattr(self, "revisions") else {}
        expected_version = self.plan.get("total_revisions", current)
        self.schema_before["expected_schema_version"] = max(expected_version, current)
        step.data = {
            "status": self.schema_before.get("status"),
            "current_schema_version": self.schema_before.get("current_schema_version"),
            "expected_schema_version": self.schema_before.get("expected_schema_version"),
            "counts": self.schema_before.get("counts"),
        }
        step.status = "WARN" if self.schema_before.get("status") != "OK" else "PASS"
        self.schema_version_before = int(self.schema_before.get("current_schema_version") or 0)
        self.schema_version_expected = int(self.schema_before.get("expected_schema_version") or 0)

    def _step_locate_approved_migrations(self) -> None:
        step = self.step("approved_migrations", "Confirm every change has an approved revision")
        self.revisions = MIG.collect(self.app, registry=self.registry)
        with self.app.app_context():
            applied = LEDGER.applied_revisions()
        self.revisions = MIG.validate(self.revisions, policy=self.policy, applied=applied)
        self.plan = MIG.plan(self.revisions)
        pending = [r for r in self.revisions if r.status == "PENDING"]
        pending_tables = {table for r in pending for table in _tables_touched(r, self.registry)}
        additive = [i for i in self.schema_before.get("issues", []) if i["severity"] == "ADDITIVE"]
        unapproved = [i for i in additive if i["table"] not in pending_tables and i["kind"] != "missing_index"]
        # The legacy ensure-chain legitimately closes missing tables/columns in
        # auto/guarded mode, so unapproved drift is only fatal when that chain
        # will not run (audit/manual policies).
        legacy_will_run = self.legacy_permitted and self.policy.auto_apply
        step.data = {
            "total_revisions": len(self.revisions),
            "pending": [r.global_revision for r in pending],
            "additive_drift": len(additive),
            "drift_without_revision": len(unapproved),
            "legacy_baseline_available": legacy_will_run,
        }
        self.unapproved_drift = unapproved
        if unapproved and not legacy_will_run:
            step.fail(
                what=f"{len(unapproved)} schema change(s) exist in code with no revision",
                why="schema drift must be versioned; a startup ALTER is not an audit trail",
                where="app/migrations or the module's [database.migrations]",
                module=", ".join(sorted({i.get("owner", "core") for i in unapproved})),
                database_object=", ".join(sorted({i["object"] for i in unapproved})[:8]),
                data_risk="medium: the change would be applied inconsistently across environments",
                next_action="add a revision for each object listed in schema-audit.json, then re-run",
            )
            self._block(step)
        elif unapproved:
            step.warn(
                f"{len(unapproved)} drift object(s) have no revision; the legacy ensure-chain "
                "will close them additively (recorded as baseline)",
                unapproved=[i["object"] for i in unapproved][:20],
            )
        else:
            step.detail = f"{len(pending)} pending revision(s)"

    def _step_validate_migrations(self) -> None:
        step = self.step("validate_migrations", "Validate revisions (lint, checksum, policy)")
        blocked = [r for r in getattr(self, "revisions", []) if r.status in {"REQUIRES_ATTENTION", "MODIFIED"}]
        step.data = {
            "revisions": [r.as_dict() for r in getattr(self, "revisions", [])],
            "blocked": [r.global_revision for r in blocked],
        }
        if blocked:
            first = blocked[0]
            step.fail(
                what=f"{len(blocked)} revision(s) are not safe to apply",
                why="; ".join(f"{r.global_revision}: {r.problems[0]['message']}" for r in blocked if r.problems)[:900],
                where=first.path,
                module=first.module_id,
                database_object=",".join(sorted(_tables_touched(first, self.registry))) or "n/a",
                data_risk="high: applying an unvalidated revision to a live ledger",
                next_action="fix the revision (or add a new one for an already-applied change) — never edit applied history",
            )
            self._block(step)
        else:
            step.detail = f"{len(getattr(self, 'revisions', []))} revision(s) validated"

    def _step_dependencies(self) -> None:
        step = self.step("dependencies", "Check module and revision dependencies")
        problems: list[str] = []
        revisions = {r.global_revision: r for r in getattr(self, "revisions", [])}
        pending = [r for r in getattr(self, "revisions", []) if r.status == "PENDING"]
        for revision in pending:
            for dep in revision.depends_on:
                if dep in revisions and revisions[dep].status not in {"APPLIED", "PENDING"}:
                    problems.append(f"{revision.global_revision} depends on unready {dep}")
        if self.registry is not None:
            for spec in self.registry.specs.values():
                if spec.status == "MIGRATION_REQUIRED" or (spec.migrations and spec.status not in {"REGISTERED", "READY"}):
                    problems.append(f"module '{spec.module_id}' is {spec.status} but declares migrations")
        step.data = {"problems": problems}
        if problems:
            step.fail(
                what=f"{len(problems)} unresolved dependency requirement(s)",
                why="; ".join(problems[:6]),
                where="module manifest depends_on / revision DEPENDS_ON",
                data_risk="medium: applying out of order can leave half-migrated data",
                next_action="apply the dependency first; the pipeline stops dependent work deliberately",
            )
            self._block(step)

    def _step_backup(self) -> None:
        step = self.step("backup", "Back up the database before risky operations")
        pending = [r for r in getattr(self, "revisions", []) if r.status == "PENDING"]
        needs_work = bool(pending) or bool(getattr(self, "unapproved_drift", [])) or self.requirements.get("missing_tables")
        if self.mode != MODE_APPLY:
            step.status = "SKIPPED"
            step.detail = f"{self.mode} mode never writes; run with --apply to migrate"
            return
        if not needs_work:
            step.status = "SKIPPED"
            step.detail = "nothing to change, no backup required"
            return
        from app.services.maintenance import MaintenanceError, create_backup

        try:
            with self.app.app_context():
                payload = create_backup(self.app, reason=f"pre-update:{self.run_key}")
            self.backup = {
                "status": "PASS",
                "path": payload.get("path", ""),
                "sha256": payload.get("sha256", "") or (payload.get("database") or {}).get("sha256", ""),
                "manifest": payload.get("manifest", ""),
                "validated": bool(payload.get("validated", True)),
            }
            step.data = {"path": self.backup["path"], "size_bytes": payload.get("size_bytes")}
            if not self.backup["validated"]:
                raise MaintenanceError("backup was created but did not validate")
        except MaintenanceError as exc:
            step.fail(
                what=f"backup failed or is unverified: {exc}",
                why="the policy requires a verified backup before any schema change",
                where="app/services/maintenance.create_backup",
                data_risk="high: without a backup a bad migration is unrecoverable",
                next_action="free disk space / fix BACKUP_DIR permissions, or set AMS_REQUIRE_BACKUP_BEFORE_UPDATE=0 in development",
            )
            self.backup["status"] = "FAIL"
            if self.policy.is_production or self.policy.require_backup:
                self._block(step)
            else:
                step.status = "WARN"
                self.notes.append("backup failed but the policy allows continuing outside production")
        except Exception as exc:
            step.fail(
                what=f"backup raised {type(exc).__name__}: {exc}",
                where="app/services/maintenance.create_backup",
                data_risk="high",
                next_action="inspect instance/storage/backups and the error log",
            )
            self.backup["status"] = "FAIL"
            self._block(step)

    def _step_baseline(self) -> None:
        """Run (or skip) the legacy ensure-chain, and always capture the baseline."""
        step = self.step("apply_baseline", "Additive baseline: create_all + legacy ensure steps")
        with self.app.app_context():
            self.before_counts = INTEGRITY.snapshot_counts()
        if not self.can_write:
            step.status = "SKIPPED"
            step.detail = f"{self.mode} mode: baseline not modified"
            return
        if not (self.include_legacy and self.policy.auto_apply):
            step.status = "SKIPPED"
            step.detail = (
                f"policy '{self.policy.policy}' does not apply schema changes at startup; "
                "run tools/dbupdate.py apply"
            )
            step.data = {"policy": self.policy.as_dict()}
            return
        mode = LEGACY.MODE_STEPS if self.only else (LEGACY.MODE_CHAIN)
        with self.app.app_context():
            result = LEGACY.run(self.app, mode=mode, only=self.only)
        step.data = {
            "executed": result.get("executed"),
            "failures": result.get("failures"),
            "duration_ms": result.get("duration_ms"),
            "steps": result.get("steps"),
        }
        if result.get("failures"):
            step.warn("legacy ensure-step(s) reported failures: " + ", ".join(result["failures"]))
            LEDGER.record(
                revision="legacy:bootstrap_failures",
                module_id="core",
                version="legacy",
                slug="bootstrap_chain",
                filename="app/services/schema.py",
                kind="legacy_step",
                checksum="",
                status=LEDGER.STATUS_FAILED,
                app_version=str(self.app.config.get("APP_VERSION") or ""),
                report={"failures": result["failures"], "steps": result.get("steps")},
                error="one or more ensure steps raised; see report",
            )
        else:
            LEDGER.record(
                revision="legacy:bootstrap",
                module_id="core",
                version="legacy",
                slug="bootstrap_chain",
                filename="app/services/schema.py",
                kind="legacy_step",
                checksum="",
                status=LEDGER.STATUS_APPLIED,
                duration_ms=result.get("duration_ms"),
                app_version=str(self.app.config.get("APP_VERSION") or ""),
                schema_version_before=self.schema_version_before,
                schema_version_after=self.schema_version_before,
                backup_path=self.backup.get("path") or None,
                backup_sha256=self.backup.get("sha256") or None,
                report={"executed": result.get("executed"), "steps": result.get("steps")},
            )
            step.detail = f"legacy chain executed ({result.get('executed')})"

    def _step_apply_migrations(self) -> None:
        step = self.step("apply_migrations", "Apply approved revisions")
        pending = [r for r in getattr(self, "revisions", []) if r.status == "PENDING"]
        if not pending:
            step.status = "SKIPPED"
            step.detail = "no pending revisions"
            return
        if not self.can_write:
            step.status = "SKIPPED"
            step.detail = f"{self.mode} mode: {len(pending)} revision(s) would be applied by --apply"
            step.data = {"would_apply": [r.global_revision for r in pending]}
            return
        if self.stopped:
            step.status = "SKIPPED"
            step.detail = "an earlier critical step failed; dependent work was not attempted"
            return
        from models import db

        for revision in pending:
            result = self._apply_one(revision, db)
            self.revision_results.append(result)
            if result["status"] == "APPLIED":
                revision.status = "APPLIED"
                self.applied_count += 1
            else:
                self.failed_count += 1
                step.fail(
                    what=f"revision {revision.global_revision} did not complete",
                    why=result.get("error", "verification failed"),
                    where=revision.path,
                    module=revision.module_id,
                    database_object=",".join(sorted(_tables_touched(revision, self.registry))) or "n/a",
                    data_risk=result.get("data_risk", "unknown: the transaction was rolled back, so no data changed"),
                    next_action=result.get("next_action", "fix the revision and re-run; pending history was not advanced"),
                )
                self._block(step)
                break
        step.data = {
            "applied": self.applied_count,
            "failed": self.failed_count,
            "results": self.revision_results,
        }
        if not self.stopped and self.applied_count:
            step.detail = f"{self.applied_count} revision(s) applied"

    def _apply_one(self, revision: MIG.Revision, db) -> dict:
        started = time.time()
        module = None
        try:
            module = MIG.load_revision_module(Path(revision.path)) if revision.path.endswith(".py") else None
        except Exception as exc:
            return {
                "revision": revision.global_revision,
                "kind": revision.kind,
                "status": "FAILED",
                "error": f"revision could not be loaded: {exc}",
                "next_action": "fix the syntax/import error in the revision file",
            }
        connection = db.engine.connect()
        transaction = None
        affected = 0
        try:
            transaction = connection.begin()
            upgrade_report: dict = {}
            if revision.sql:
                for statement in _split_statements(revision.sql):
                    connection.execute(text(statement))
                affected = _affected_row_estimate(connection, revision.sql)
            elif module is not None and hasattr(module, "upgrade"):
                returned = module.upgrade(connection)
                if isinstance(returned, dict):
                    upgrade_report = returned
                affected = int(returned.get("rows_updated") or 0) if isinstance(returned, dict) else 0
            if module is not None and hasattr(module, "verify"):
                module.verify(connection)
            if revision.kind == "data" and not (module is not None and hasattr(module, "verify")):
                raise RuntimeError("data revision has no verify(); refusing to commit an unverified data change")
            transaction.commit()
        except Exception as exc:
            try:
                if transaction is not None and transaction.is_active:
                    transaction.rollback()
            except Exception:
                pass
            # SQLite (pysqlite) commits implicitly around DDL, so a failed
            # ``CREATE TABLE``/``CREATE INDEX`` cannot always be undone by
            # ROLLBACK.  A revision that creates objects may therefore declare
            # ``undo(connection)``; if it does, the compensation runs here and is
            # reported.  If it does not, the run says so instead of pretending
            # the database is back where it started.
            compensated = False
            if module is not None and hasattr(module, "undo"):
                try:
                    with db.engine.begin() as undo_connection:
                        module.undo(undo_connection)
                    compensated = True
                except Exception:
                    LOG.exception(
                        "revision %s declared undo() but compensation failed",
                        revision.global_revision,
                    )
            LEDGER.record(
                revision=revision.global_revision,
                module_id=revision.module_id,
                version=revision.revision,
                slug=revision.title,
                filename=Path(revision.path).name,
                kind=revision.kind,
                checksum=revision.checksum,
                status=LEDGER.STATUS_FAILED,
                duration_ms=int((time.time() - started) * 1000),
                app_version=str(self.app.config.get("APP_VERSION") or ""),
                schema_version_before=self.schema_version_before,
                backup_path=self.backup.get("path") or None,
                backup_sha256=self.backup.get("sha256") or None,
                error=f"{type(exc).__name__}: {exc}",
                report={
                    "rolled_back": True,
                    "compensated": compensated,
                    "mode": self.mode,
                    "restoration_required": not compensated,
                    "backup": self.backup.get("path", ""),
                },
            )
            LOG.exception("migration %s failed and was rolled back", revision.global_revision)
            return {
                "revision": revision.global_revision,
                "kind": revision.kind,
                "status": "FAILED",
                "error": f"{type(exc).__name__}: {exc}",
                "rolled_back": True,
                "compensated": compensated,
                "restoration_required": not compensated,
                "next_action": (
                    "the revision was rolled back; correct it and re-run tools/dbupdate.py apply"
                    if compensated
                    else "SQLite committed DDL before the failure: restore the backup "
                    f"'{self.backup.get('path') or '(none recorded)'}' or add undo() to the revision, "
                    "then re-run tools/dbupdate.py apply"
                ),
            }
        finally:
            try:
                connection.close()
            except Exception:
                pass
        schema_version = self._next_schema_version()
        LEDGER.record(
            revision=revision.global_revision,
            module_id=revision.module_id,
            version=revision.revision,
            slug=revision.title,
            filename=Path(revision.path).name,
            kind=revision.kind,
            checksum=revision.checksum,
            status=LEDGER.STATUS_APPLIED,
            duration_ms=int((time.time() - started) * 1000),
            app_version=str(self.app.config.get("APP_VERSION") or ""),
            schema_version_before=self.schema_version_before,
            schema_version_after=schema_version,
            backup_path=self.backup.get("path") or None,
            backup_sha256=self.backup.get("sha256") or None,
            affected_rows=affected,
            report={
                "mode": self.mode,
                "verify": bool(module is not None and hasattr(module, "verify")),
                "upgrade_report": upgrade_report,
                "exception_count": (upgrade_report or {}).get("exceptions", 0),
            },
        )
        return {
            "revision": revision.global_revision,
            "kind": revision.kind,
            "status": "APPLIED",
            "duration_ms": int((time.time() - started) * 1000),
            "affected_rows": affected,
            "rolled_back": False,
        }

    def _next_schema_version(self) -> int:
        from models import db

        applied = len([r for r in getattr(self, "revisions", []) if r.status in {"APPLIED", "PENDING"}])
        with self.app.app_context():
            return LEDGER.write_schema_version(max(applied, 1), db.session)

    def _step_verify_schema(self) -> None:
        step = self.step("verify_schema", "Verify the resulting schema")
        from models import db

        with self.app.app_context():
            self.schema_after = _audit(self.app, self.registry)
        remaining_additive = [i for i in self.schema_after.get("issues", []) if i["severity"] == "ADDITIVE"]
        step.data = {
            "status": self.schema_after.get("status"),
            "remaining_additive_drift": [i["object"] for i in remaining_additive][:20],
            "current_schema_version": self.schema_after.get("current_schema_version"),
            "expected_schema_version": self.schema_after.get("expected_schema_version"),
        }
        if remaining_additive and self.can_write:
            step.fail(
                what=f"{len(remaining_additive)} expected object(s) are still missing after the update",
                why="a step reported success without delivering its schema change "
                "(this is exactly what the old 'except: pass' chain hid)",
                where=", ".join(sorted({i["object"] for i in remaining_additive})[:6]),
                module=", ".join(sorted({str(i.get("owner", "core")) for i in remaining_additive})),
                database_object=", ".join(sorted({i["object"] for i in remaining_additive})[:6]),
                data_risk="medium: reads that assume the new column will fail at runtime",
                next_action="run tools/dbupdate.py audit-schema and fix the migration or the ensure-step",
            )
            self._block(step)
        elif remaining_additive:
            step.status = "WARN"
            step.detail = f"{len(remaining_additive)} object(s) pending (report-only mode)"
        elif self.schema_after.get("counts", {}).get("manual"):
            step.warn(
                f"{self.schema_after['counts']['manual']} manual-only difference(s) remain "
                "(type changes / missing NOT NULL or FK constraints)"
            )
        else:
            step.status = "PASS"
            step.detail = "expected schema matches the database"

    def _step_integrity(self) -> None:
        step = self.step("data_integrity", "Run data integrity checks")
        from models import db

        with self.app.app_context():
            try:
                from sqlalchemy import inspect as _inspect

                table_count = len(_inspect(db.engine).get_table_names())
            except Exception:
                table_count = 0
            if table_count < 5:
                # A first bootstrap has no data to check yet; saying so beats a
                # wall of "no such table" warnings that teaches operators to
                # ignore the report.
                step.status = "SKIPPED"
                step.detail = (
                    f"database holds {table_count} table(s): integrity checks run once the "
                    "schema exists (this pass creates it)"
                )
                return
            payload = INTEGRITY.run_integrity(self.app, deep=self.mode == MODE_APPLY or self.policy.policy != POLICY_AUDIT)
        step.data = payload
        if payload.get("status") == "ERROR":
            # A check that could not run is not a check that passed.
            step.warn(
                "integrity layer(s) could not complete: "
                + ", ".join(payload.get("error_layers") or ["unknown"]),
                error_layers=payload.get("error_layers") or [],
            )
            self.notes.append(
                "data integrity could not be fully verified ("
                + ", ".join(payload.get("error_layers") or [])
                + "); run python tools/consistency_report.py to see why"
            )
        if payload.get("status") == "FAIL":
            failed_layers = payload.get("failed_layers") or []
            step.fail(
                what=f"data integrity check failed in {', '.join(failed_layers)}",
                why=_integrity_reason(payload),
                where="post-update integrity",
                data_risk="high: financial/inventory/ledger consistency is affected",
                next_action="run python tools/consistency_report.py --json, repair with tools/repair_controlled/*, then re-run",
            )
            if self.can_write:
                self._block(step)
            else:
                step.status = "WARN"
                self.notes.append("integrity problems found in a read-only run; --apply would stop on them")

    def _step_module_health(self) -> None:
        step = self.step("module_health", "Run module health checks")
        if self.registry is None:
            step.status = "SKIPPED"
            return
        from app.services.module_system.health import run_module_health

        try:
            with self.app.app_context():
                payload = run_module_health(self.app, self.registry)
            step.data = {"status": payload.get("status"), "failed": payload.get("failed"), "checked": payload.get("checked")}
            self.module_health = payload
            if payload.get("status") == "FAIL":
                offenders = {mid: r for mid, r in payload["modules"].items() if r["status"] == "FAIL"}
                step.warn(
                    "health check failure(s): "
                    + "; ".join(f"{mid} -> {','.join(r['failed'])}" for mid, r in list(offenders.items())[:4])
                )
        except Exception as exc:
            LOG.exception("module health checks raised")
            step.warn(f"health checks could not complete: {type(exc).__name__}: {exc}")

    def _step_navigation(self) -> None:
        step = self.step("navigation", "Validate navigation (module + shared layout)")
        if self.registry is None:
            step.status = "SKIPPED"
            return
        from app.services.module_system.navigation import validate_navigation

        try:
            problems = validate_navigation(self.app, self.registry)
        except Exception as exc:  # pragma: no cover - defensive
            step.warn(f"navigation validation could not run: {type(exc).__name__}: {exc}")
            return
        errors = [item for item in problems if item.get("severity", "error") == "error"]
        step.data = {"problems": problems}
        if errors:
            step.fail(
                what=f"{len(errors)} navigation problem(s)",
                why="; ".join(f"{item.get('code')}: {item.get('detail')}" for item in errors[:5]),
                where="module manifests [navigation] / templates/layout.html",
                module=", ".join(sorted({str(item.get("module", "")) for item in errors})),
                data_risk="none to data; every page can 500 if the shared layout links a dead endpoint",
                next_action="register the missing route or fix the link",
            )
            self._block(step)
        elif problems:
            step.warn("; ".join(str(item.get("detail")) for item in problems[:3]))
        else:
            step.detail = "all navigation targets resolve"

    def _step_module_tests(self) -> None:
        step = self.step("module_tests", "Run the affected modules' tests")
        if not self.with_tests:
            step.status = "SKIPPED"
            step.detail = "not requested (use --with-tests)"
            return
        paths = sorted({str(Path(self.app.root_path).parent / rel) for rel in self._declared_test_paths()})
        if not paths:
            step.status = "SKIPPED"
            step.detail = "no module declares test paths"
            return
        payload = _run_pytest(paths, cwd=Path(self.app.root_path).parent)
        step.data = payload
        if payload.get("status") == "FAIL":
            step.fail(
                what=f"module test suite failed ({payload.get('summary', '')})",
                why="the update must not be deployed when its own module tests fail",
                where="pytest " + " ".join(paths),
                data_risk="medium",
                next_action="fix the failing tests or the module, then re-run",
            )
            self._block(step)

    def _declared_test_paths(self) -> list[str]:
        if self.registry is None:
            return []
        return [rel for spec in self.registry.specs.values() if spec.enabled for rel in spec.test_paths]

    def _step_regression(self) -> None:
        step = self.step("regression", "Verify existing data and functionality are intact")
        from models import db

        with self.app.app_context():
            after = INTEGRITY.snapshot_counts()
        comparison = INTEGRITY.compare_counts(self.before_counts, after)
        smoke = self._smoke_check()
        step.data = {"row_counts": comparison, "route_smoke": smoke}
        if comparison.get("status") == "FAIL":
            lost = comparison.get("row_losses") or []
            step.fail(
                what=f"row loss detected in {len(lost)} table(s): "
                + ", ".join(f"{entry['table']} ({entry['before']}→{entry['after']})" for entry in lost[:5]),
                why="an update must never reduce business data unless a migration declared it",
                where="row-count guard (before vs after)",
                data_risk="critical: business records are missing",
                next_action=f"restore the backup {self.backup.get('path') or '(see instance/storage/backups)'} "
                "and investigate; do not re-run the migration",
            )
            self._block(step)
        elif smoke.get("status") == "FAIL":
            step.fail(
                what="post-update smoke check failed: " + smoke.get("detail", ""),
                why="the application must still answer its own routes after an update",
                where=", ".join(smoke.get("failed") or [])[:400],
                data_risk="none detected beyond availability",
                next_action="inspect the failing route's template/service before deploying",
            )
            self._block(step)
        else:
            step.detail = f"{comparison.get('tables_checked', 0)} table(s) unchanged or grown; smoke ok"

    def _smoke_check(self) -> dict:
        """Prove the app still answers after the update: health, login, and every
        module-declared navigation target must at least build and respond."""
        outcome = {"status": "PASS", "detail": "", "checked": 0, "failed": [], "unbuilt": []}
        try:
            from flask import url_for

            with self.app.test_client() as client:
                for path in ("/health", "/login"):
                    response = client.get(path)
                    outcome["checked"] += 1
                    if response.status_code >= 500:
                        outcome["failed"].append(f"{path} -> HTTP {response.status_code}")
                root = client.get("/")
                outcome["checked"] += 1
                # Anonymous must be redirected to login, never 500.
                if root.status_code >= 500:
                    outcome["failed"].append(f"/ -> HTTP {root.status_code}")

            if self.registry is not None:
                with self.app.test_request_context("/"):
                    for item in self.registry.navigation(self.app):
                        endpoint = item.get("endpoint")
                        if not endpoint:
                            continue
                        try:
                            url_for(endpoint)
                            outcome["checked"] += 1
                        except Exception:
                            outcome["unbuilt"].append(f"{item.get('module')}:{endpoint}")
        except Exception as exc:
            outcome["status"] = "WARN"
            outcome["detail"] = f"smoke check unavailable: {type(exc).__name__}: {exc}"
            return outcome
        if outcome["failed"]:
            outcome["status"] = "FAIL"
            outcome["detail"] = "; ".join(outcome["failed"])
        elif outcome["unbuilt"]:
            outcome["status"] = "WARN"
            outcome["detail"] = f"{len(outcome['unbuilt'])} navigation endpoint(s) cannot build a URL"
        else:
            outcome["detail"] = f"{outcome['checked']} request/route probes ok"
        return outcome

    def _checks_summary(self) -> dict:
        """Condense the steps into the Phase-14 report's check lines."""
        by_name = {step.name: step for step in self.steps}

        def status_of(name: str, fallback: str = "NOT_RUN") -> str:
            step = by_name.get(name)
            return step.status if step is not None else fallback

        def detail_of(name: str, key: str):
            step = by_name.get(name)
            return (step.data.get(key) if step is not None else None)

        schema_step = by_name.get("verify_schema")
        integrity_step = by_name.get("data_integrity")
        regression_step = by_name.get("regression")
        return {
            "schema_validation": {
                "status": (schema_step.data.get("status") if schema_step else None) or status_of("verify_schema"),
                "detail": (schema_step.detail if schema_step else ""),
                "remaining_drift": (schema_step.data.get("remaining_additive_drift") if schema_step else []) or [],
            },
            "data_integrity": {
                "status": (integrity_step.data.get("status") if integrity_step else None) or status_of("data_integrity"),
                "detail": _integrity_reason(integrity_step.data) if integrity_step and integrity_step.data else "",
                "layers": list(((integrity_step.data or {}).get("layers") or {}).keys()) if integrity_step else [],
            },
            "regression": {
                "status": "PASS"
                if regression_step and regression_step.status in ("PASS", "SKIPPED")
                else status_of("regression"),
                "row_counts": (regression_step.data.get("row_counts") if regression_step else {}) or {},
                "route_smoke": (regression_step.data.get("route_smoke") if regression_step else {}) or {},
            },
            "module_health": {
                "status": status_of("module_health"),
                "detail": (by_name.get("module_health").detail if by_name.get("module_health") else ""),
            },
            "navigation": {
                "status": status_of("navigation"),
                "detail": (by_name.get("navigation").detail if by_name.get("navigation") else ""),
            },
            "module_tests": {
                "status": status_of("module_tests"),
                "detail": (by_name.get("module_tests").detail if by_name.get("module_tests") else ""),
            },
        }

    def _finish(self) -> dict:
        finished = datetime.now(timezone.utc)
        try:
            if not self.can_write:
                installs_payload = {"status": "SKIPPED", "detail": "check-only run does not write install state"}
                raise _Skip()
            from app.services.dbupdate import installs

            installs_payload = installs.record_installs(
                self.app, self.registry, health=getattr(self, "module_health", None)
            )
        except _Skip:
            pass
        except Exception:
            LOG.debug("module installs could not be tracked", exc_info=True)
            installs_payload = {"status": "SKIPPED"}
        hard_failures = [s for s in self.steps if s.status == "FAIL" and s.blocker and s.blocker.get("severity") != "warning"]
        try:
            from app.services.dbupdate import migrations as _mig

            self.plan = _mig.plan(self.revisions) or self.plan
        except Exception:
            pass
        checks = self._checks_summary()
        pending_after = self.schema_after.get("status") in {"MIGRATION_REQUIRED", "SCHEMA_DRIFT"}
        if hard_failures:
            final = "UPDATE REQUIRES ATTENTION"
        elif pending_after and self.mode == MODE_APPLY:
            final = "UPDATE REQUIRES ATTENTION"
        elif self.plan.get("pending") and self.mode != MODE_APPLY:
            final = "PENDING UPDATES"
        elif self.mode == MODE_APPLY:
            final = "READY"
        else:
            final = "READY (nothing to do)" if not pending_after else "READY WITH PENDING SCHEMA WORK"
        report = {
            "run_key": self.run_key,
            "generated_at": finished.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "trigger": self.trigger,
            "mode": self.mode,
            "environment": self.policy.environment,
            "policy": self.policy.policy,
            "policy_detail": self.policy.as_dict(),
            "app_version": str(self.app.config.get("APP_VERSION") or ""),
            "schema_version_current": int(self.schema_after.get("current_schema_version") or self.schema_version_before or 0),
            "schema_version_expected": int(self.schema_after.get("expected_schema_version") or self.schema_version_expected or 0),
            "modules": getattr(self, "registry_summary", {}),
            "module_registry": self.registry.report(self.app) if self.registry is not None else {},
            "migrations": {
                **self.plan,
                "applied": self.applied_count,
                "failed": self.failed_count,
                "results": self.revision_results,
            },
            "schema_audit": self.schema_after or self.schema_before,
            "checks": checks,
            "data_migration_status": _data_migration_status(self.revision_results),
            "backup": self.backup,
            "final_status": final,
            "blockers": self.blockers,
            "install_tracking": installs_payload,
            "notes": list(self.notes) + list(self.policy.notes),
            "pipeline": [s.as_dict() for s in self.steps],
            "started_at": self.started.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "duration_ms": int((finished - self.started).total_seconds() * 1000),
            "database": str(_db_name(self.app)),
            "full_report": None,
        }
        # Regenerate the generated module documentation after a real change, so
        # `docs/MODULE_REGISTRY.md` can never describe a stale schema.  A
        # documentation failure is recorded and never blocks an update.
        if self.policy.regenerate_docs and self.can_write and self.applied_count:
            try:
                from app.services.dbupdate import generate_docs

                report["docs"] = generate_docs(self.app, registry=self.registry, write=True)
            except Exception as exc:  # pragma: no cover - defensive
                LOG.warning("module docs could not be regenerated: %s", exc)
                report["docs"] = {"status": "SKIPPED", "detail": f"{type(exc).__name__}: {exc}"}
        report["full_report"] = dict(report)
        report["full_report"]["module_registry"] = report["module_registry"]
        written = REPORTS.write(self.app, report, archive=self.mode != MODE_CHECK)
        report["report_files"] = written.get("files", {})
        report["full_report"]["report_files"] = report["report_files"]
        try:
            if not self.ledger_usable:
                report["ledger_note"] = (
                    "this run was not recorded: the update ledger is not present and a "
                    "check-only run never creates it"
                )
                raise _Skip()
            with self.app.app_context():
                LEDGER.begin_run(
                    run_key=self.run_key,
                    environment=self.policy.environment,
                    policy=self.policy.policy,
                    mode=self.mode,
                )
                LEDGER.finish_run(run_key=self.run_key, report=report)
        except _Skip:
            pass
        except Exception:
            LOG.warning("update run could not be persisted in the ledger", exc_info=True)
        self.report = report
        return report


def _audit(app, registry) -> dict:
    from app.services.dbupdate import schema_audit

    from models import db

    applied = LEDGER.applied_revisions()
    revisions = MIG.collect(app, registry=registry)
    expected_revision = len(revisions)
    payload = schema_audit.audit(db.engine, registry=registry, expected_revision=expected_revision)
    payload["applied_revisions"] = len(applied)
    payload["known_revisions"] = expected_revision
    return payload


def _tables_touched(revision: MIG.Revision, registry=None) -> set[str]:
    names: set[str] = set()
    import re

    for statement in _split_statements(revision.sql):
        for match in re.finditer(
            r"(?:create\s+table(?:\s+if\s+not\s+exists)?|alter\s+table|create\s+(?:unique\s+)?index(?:\s+if\s+not\s+exists)?\s+\S+\s+on)\s+([A-Za-z_][A-Za-z0-9_]*)",
            statement,
            re.IGNORECASE,
        ):
            names.add(match.group(1).lower())
    if registry is not None:
        spec = registry.specs.get(revision.module_id)
        if spec is not None:
            names.update(table.lower() for table in spec.tables)
    return names


def _integrity_reason(payload: dict) -> str:
    layers = payload.get("layers") or {}
    parts: list[str] = []
    sqlite = layers.get("sqlite") or {}
    if sqlite.get("status") == "FAIL":
        parts.append(f"sqlite: {sqlite.get('integrity_check')}; FK violations {sqlite.get('foreign_key_violations')}")
    preflight = layers.get("preflight") or {}
    if preflight.get("blocks"):
        parts.append("preflight blockers: " + "; ".join(str(b.get("id")) for b in preflight["blocks"][:4]))
    consistency = layers.get("consistency") or {}
    if consistency.get("failing"):
        parts.append("consistency: " + ", ".join(str(f.get("check")) for f in consistency["failing"][:4]))
    return " | ".join(parts) or "one or more integrity layers reported FAIL"


def _data_migration_status(results: list[dict]) -> str:
    data = [r for r in results if r.get("kind") == "data"]
    if not data:
        return "NO DATA REVISIONS"
    failed = [r for r in data if r.get("status") == "FAILED"]
    if failed:
        return "FAILED (rolled back)"
    return f"APPLIED {len(data)} with verification"


def _run_pytest(paths: list[str], cwd: Path, timeout: int = 600) -> dict:
    command = [sys.executable, "-m", "pytest", "-q", *paths]
    try:
        completed = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "FAIL", "summary": f"timed out after {timeout}s", "command": command}
    except OSError as exc:
        return {"status": "ERROR", "summary": str(exc), "command": command}
    tail = "\n".join((completed.stdout or "").splitlines()[-25:])
    return {
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "summary": tail[-2000:],
        "command": command,
        "returncode": completed.returncode,
    }


def _db_name(app) -> str:
    try:
        return Path(str(app.config.get("APP_DB_PATH") or "")).name
    except Exception:
        return ""


def preview_on_copy(app, *, registry=None, timeout: int = 600) -> dict:
    """Rehearse the pending updates against a *copy* of the live database.

    The copy is made with SQLite's online backup API (so it is consistent even
    mid-write), then a separate interpreter imports the app against it under the
    development policy.  Nothing about the real database changes; the point is
    to see whether the pending revisions actually succeed, and what the schema
    looks like afterwards.
    """
    import sqlite3
    import tempfile

    source = Path(str(app.config.get("APP_DB_PATH") or ""))
    if not source.is_file():
        return {"status": "SKIPPED", "detail": f"no database file to copy at {source}"}
    temp_dir = Path(tempfile.mkdtemp(prefix="ams_preview_"))
    copy_path = temp_dir / "preview.db"
    try:
        with sqlite3.connect(source) as src, sqlite3.connect(copy_path) as dst:
            src.backup(dst)
    except sqlite3.Error as exc:
        shutil_rmtree(temp_dir)
        return {"status": "ERROR", "detail": f"could not copy the database: {exc}"}

    script = (
        "import json,os,sys;"
        "sys.path.insert(0, os.getcwd());"
        "from app import create_app;"
        "a=create_app({'AMS_UPDATE_POLICY':'auto','AMS_ENV':'development','TESTING':False,"
        "'BACKUP_EMBEDDED_SCHEDULER':False});"
        "print(json.dumps({'bootstrap_error': a.config.get('AMS_UPDATE_FINAL_STATUS','')}))"
    )
    env = {
        "APP_DB_PATH": str(copy_path),
        "DB_HEALTH_SNAPSHOT_PATH": str(temp_dir / "health_snapshot.json"),
        "AMS_ENV": "development",
        "AMS_UPDATE_POLICY": "auto",
        "ALLOW_EMPTY_DB": "1",
        "SQLITE_JOURNAL_MODE": "DELETE",
        "BACKUP_EMBEDDED_SCHEDULER": "0",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(temp_dir),
    }
    import os

    full_env = {**os.environ, **env}
    started = time.time()
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(app.root_path).parent),
            env=full_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        status = "PASS" if completed.returncode == 0 else "FAIL"
        detail = (completed.stdout or "").strip().splitlines()[-1:] or [""]
        probe = _audit_copy(copy_path)
    except subprocess.TimeoutExpired:
        status, detail, probe = "FAIL", [f"rehearsal timed out after {timeout}s"], {}
    except Exception as exc:  # pragma: no cover - defensive
        status, detail, probe = "ERROR", [f"{type(exc).__name__}: {exc}"], {}
    finally:
        shutil_rmtree(temp_dir)
    return {
        "status": status,
        "detail": " ".join(detail)[:2000],
        "duration_ms": int((time.time() - started) * 1000),
        "resulting_schema": probe,
        "note": "applied against a throw-away copy; the real database was not modified",
    }


def _audit_copy(path: Path) -> dict:
    """Read-only structural summary of the rehearsed copy."""
    import sqlite3

    if not path.is_file():
        return {}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            try:
                integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
            except sqlite3.Error as exc:
                integrity = f"error: {exc}"
            try:
                fk = len(con.execute("PRAGMA foreign_key_check").fetchall())
            except sqlite3.Error:
                fk = -1
            version = int(con.execute("PRAGMA user_version").fetchone()[0] or 0)
            ledger_rows = []
            if LEDGER.MIGRATION_TABLE in tables:
                ledger_rows = [
                    {"revision": row[0], "status": row[1]}
                    for row in con.execute(f"SELECT revision, status FROM {LEDGER.MIGRATION_TABLE} ORDER BY id")
                ]
        return {
            "tables": len(tables),
            "integrity_check": integrity,
            "foreign_key_violations": fk,
            "user_version": version,
            "ledger": ledger_rows,
        }
    except sqlite3.Error as exc:
        return {"error": str(exc)}


def shutil_rmtree(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # pragma: no cover
        pass


def run_update(app, **kwargs) -> dict:
    return UpdatePipeline(app, **kwargs).run()
