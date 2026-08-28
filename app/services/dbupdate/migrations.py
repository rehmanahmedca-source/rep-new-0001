"""Migration discovery, validation and planning for the AMS update pipeline.

A revision is one file, applied at most once, forever recorded in the ledger.
Two kinds of source are collected into one ordered set:

``app/migrations/*.py``      core revisions (schema or data) for the ERP itself
``app/migrations/*.sql``     legacy raw-SQL revisions, already understood by the
                             older ``migration_history`` ledger (adopted once)
``<module>/migrations/*.py`` revisions a module ships against its own tables,
                             declared in ``module.toml``

Contract of a Python revision::

    REVISION   = "2026_001"            # unique, sorts lexically
    TITLE      = "create plant_asset"  # shown in reports
    KIND       = "schema"              # "schema" | "data"
    DESTRUCTIVE = False                # forces manual review + explicit policy
    DEPENDS_ON = ("2026_000",)         # optional ordering constraints
    SQL        = "CREATE TABLE ..."     # optional pure-SQL change
    def upgrade(connection): ...        # optional python (takes an sa Connection)
    def verify(connection): ...         # optional; raise -> migration failed

Exactly one of ``SQL`` / ``upgrade`` must exist.  ``verify`` is mandatory for
data revisions, which is what keeps Phase 8 honest.
"""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("ams.dbupdate.migrations")

REVISION_SUFFIXES = (".py", ".sql")
_DESTRUCTIVE_RE = re.compile(
    r"\b(drop\s+table|drop\s+column|truncate\s+table|delete\s+from|drop\s+index|alter\s+table\s+\w+\s+drop)\b",
    re.IGNORECASE,
)
_UPDATE_NO_WHERE_RE = re.compile(r"\bupdate\s+\w+\s+set\b(?![^;]*\bwhere\b)", re.IGNORECASE | re.DOTALL)
_FILENAME_REVISION_RE = re.compile(r"^(?P<revision>\d{4,20})[_-].*$")
_ALLOWED_PREFIX_RE = re.compile(r"^\s*(create|alter|insert|update|with|select|pragma|comment|\-\-|/\*)", re.IGNORECASE)


@dataclass
class Revision:
    revision: str
    title: str
    module_id: str
    kind: str
    path: str
    checksum: str
    destructive: bool = False
    depends_on: tuple[str, ...] = ()
    sql: str = ""
    has_python: bool = False
    has_verify: bool = False
    data_validation: bool = False
    problems: list[dict] = field(default_factory=list)
    status: str = "PENDING"  # PENDING | APPLIED | MODIFIED | REQUIRES_ATTENTION
    applied_checksum: str = ""

    @property
    def global_revision(self) -> str:
        return self.revision if self.module_id in ("", "core") else f"{self.module_id}:{self.revision}"

    def as_dict(self) -> dict:
        return {
            "revision": self.global_revision,
            "title": self.title,
            "module": self.module_id,
            "kind": self.kind,
            "file": self.path,
            "checksum": self.checksum,
            "destructive": self.destructive,
            "depends_on": list(self.depends_on),
            "status": self.status,
            "applied_checksum": self.applied_checksum,
            "problems": list(self.problems),
            "validated": not self.problems,
        }


def _checksum(path: Path) -> str:
    try:
        data = path.read_bytes().replace(b"\r\n", b"\n")
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def load_revision_module(path: Path):
    """Import one revision file by path (repo-trusted, isolated module name)."""
    spec = importlib.util.spec_from_file_location(f"ams_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load migration module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lint_sql(sql: str, *, destructive_allowed: bool) -> list[dict]:
    """Static safety review of a revision's SQL.

    This is a guard rail, not a parser: it exists so that "someone wrote a DROP
    into a startup migration" is caught before the database is touched.
    """
    problems: list[dict] = []
    if not (sql or "").strip():
        problems.append({"code": "empty_sql", "message": "revision declares SQL but it is empty"})
        return problems
    hit = _DESTRUCTIVE_RE.search(sql or "")
    if hit and not destructive_allowed:
        problems.append(
            {
                "code": "destructive_statement",
                "message": f"destructive statement blocked: '{hit.group(0).strip()}'",
                "hint": "mark the revision DESTRUCTIVE = True and run it under "
                "AMS_ALLOW_DESTRUCTIVE_MIGRATIONS=1 with a verified backup",
            }
        )
    if _UPDATE_NO_WHERE_RE.search(sql or ""):
        problems.append(
            {
                "code": "unbounded_update",
                "message": "UPDATE without a WHERE clause would rewrite every row",
                "hint": "add a WHERE clause, or batch the update inside upgrade() with row-count assertions",
            }
        )
    # sqlite3's own statement boundary check catches most unterminated scripts.
    buffer = ""
    for line in (sql or "").splitlines():
        if line.strip().startswith("--"):
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            buffer = ""
    if buffer.strip() and not sqlite3.complete_statement(buffer if buffer.strip().endswith(";") else buffer + ";"):
        problems.append({"code": "unterminated_statement", "message": "a statement is not terminated with ';'"})
    return problems


def _revision_from_python(path: Path, *, module_id: str, declared: dict | None = None) -> Revision:
    declared = declared or {}
    module = load_revision_module(path)
    revision = str(getattr(module, "REVISION", "") or declared.get("version") or "").strip()
    if not revision:
        match = _FILENAME_REVISION_RE.match(path.stem)
        revision = match.group("revision") if match else ""
    title = str(getattr(module, "TITLE", "") or declared.get("slug") or path.stem)
    kind = str(getattr(module, "KIND", "") or declared.get("kind") or "schema").strip().lower()
    if kind not in ("schema", "data"):
        kind = "schema"
    sql = str(getattr(module, "SQL", "") or "")
    upgrade = getattr(module, "upgrade", None)
    verify = getattr(module, "verify", None)
    problems: list[dict] = []
    if not callable(upgrade) and not sql.strip():
        problems.append(
            {"code": "no_change", "message": "revision provides neither SQL nor upgrade(connection)", "file": str(path)}
        )
    if callable(upgrade) and sql.strip():
        problems.append(
            {"code": "ambiguous_change", "message": "revision provides both SQL and upgrade(); pick one"}
        )
    depends_on = tuple(str(d) for d in (getattr(module, "DEPENDS_ON", ()) or ()))
    return Revision(
        revision=revision,
        title=title,
        module_id=str(getattr(module, "MODULE", "") or module_id or "core"),
        kind=kind,
        path=str(path),
        checksum=_checksum(path),
        destructive=bool(getattr(module, "DESTRUCTIVE", declared.get("destructive", False))),
        depends_on=depends_on,
        sql=sql,
        has_python=callable(upgrade),
        has_verify=callable(verify),
        data_validation=bool(getattr(module, "DATA_VALIDATION", kind == "data")),
        problems=problems,
    )


def _revision_from_sql(path: Path, *, module_id: str, revision_hint: str = "") -> Revision:
    try:
        sql = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Revision(
            revision=revision_hint or path.stem,
            title=path.stem,
            module_id=module_id,
            kind="schema",
            path=str(path),
            checksum="",
            problems=[{"code": "unreadable", "message": str(exc)}],
        )
    match = _FILENAME_REVISION_RE.match(path.stem)
    revision = revision_hint or (match.group("revision") if match else path.stem)
    return Revision(
        revision=str(revision),
        title=path.stem,
        module_id=module_id,
        kind="schema",
        path=str(path),
        checksum=_checksum(path),
        sql=sql,
        depends_on=(),
    )


def collect(app=None, *, registry=None, migrations_dir: str | os.PathLike | None = None) -> list[Revision]:
    """Every revision known to this installation, sorted into apply order."""
    root = Path(migrations_dir) if migrations_dir else _default_migrations_dir(app)
    revisions: list[Revision] = []
    if root.is_dir():
        for path in sorted(root.glob("*.*")):
            if path.name.startswith("_") or path.suffix not in REVISION_SUFFIXES:
                continue
            if path.suffix == ".sql":
                revisions.append(_revision_from_sql(path, module_id="core"))
                continue
            try:
                revisions.append(_revision_from_python(path, module_id="core"))
            except Exception as exc:
                LOG.error("migration file '%s' cannot be loaded: %s", path, exc)
                revisions.append(
                    Revision(
                        revision=path.stem,
                        title=path.stem,
                        module_id="core",
                        kind="schema",
                        path=str(path),
                        checksum=_checksum(path),
                        problems=[{"code": "load_failed", "message": f"{type(exc).__name__}: {exc}"}],
                    )
                )
    if registry is not None:
        for spec in registry.specs.values():
            for ref in list(spec.migrations) + list(spec.data_migrations):
                path = Path(ref.absolute_path or (Path(spec.root) / ref.file))
                if not path.is_file():
                    revisions.append(
                        Revision(
                            revision=ref.version,
                            title=ref.slug,
                            module_id=spec.module_id,
                            kind="data" if ref in spec.data_migrations else "schema",
                            path=str(path),
                            checksum=ref.checksum,
                            destructive=ref.destructive,
                            problems=[{"code": "missing_migration_file", "message": f"declared migration is absent: {ref.file}"}],
                        )
                    )
                    continue
                declared = {
                    "version": ref.version,
                    "slug": ref.slug,
                    "destructive": ref.destructive,
                    "kind": "data" if ref in spec.data_migrations else "schema",
                }
                try:
                    if path.suffix == ".sql":
                        revisions.append(_revision_from_sql(path, module_id=spec.module_id, revision_hint=ref.version))
                    else:
                        revisions.append(_revision_from_python(path, module_id=spec.module_id, declared=declared))
                except Exception as exc:
                    revisions.append(
                        Revision(
                            revision=ref.version,
                            title=ref.slug,
                            module_id=spec.module_id,
                            kind="schema",
                            path=str(path),
                            checksum=_checksum(path),
                            destructive=ref.destructive,
                            problems=[{"code": "load_failed", "message": f"{type(exc).__name__}: {exc}"}],
                        )
                    )
    return sort_revisions(revisions)


def sort_revisions(revisions: list[Revision]) -> list[Revision]:
    """Deterministic order: revision key first, dependency edges respected."""
    ordered = sorted(revisions, key=lambda r: (r.module_id, r.revision, r.title))
    by_revision = {r.global_revision: r for r in ordered}
    for revision in by_revision.values():
        for dep in revision.depends_on:
            if dep not in by_revision and f"{revision.module_id}:{dep}" not in by_revision:
                revision.problems.append(
                    {
                        "code": "missing_dependency",
                        "message": f"DEPENDS_ON '{dep}' is not a known revision",
                        "hint": "add the dependency or drop the constraint",
                    }
                )

    resolved: list[Revision] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(revision: Revision) -> None:
        key = revision.global_revision
        if key in seen or key in visiting:
            if key in visiting:
                revision.problems.append({"code": "dependency_cycle", "message": f"cycle at '{key}'"})
            return
        visiting.add(key)
        for dep in revision.depends_on:
            for candidate in (dep, f"{revision.module_id}:{dep}"):
                target = by_revision.get(candidate)
                if target is not None and target is not revision:
                    visit(target)
                    break
        visiting.discard(key)
        seen.add(key)
        resolved.append(revision)

    for revision in ordered:
        visit(revision)
    return resolved


def validate(revisions: list[Revision], *, policy, applied: dict[str, dict] | None = None) -> list[Revision]:
    """Attach lint + ledger-state problems to each revision and set its status."""
    applied = applied or {}
    for revision in revisions:
        if revision.sql:
            revision.problems.extend(
                lint_sql(revision.sql, destructive_allowed=bool(policy.allow_destructive) and revision.destructive)
            )
        if revision.kind == "data" and not revision.has_verify:
            revision.problems.append(
                {
                    "code": "data_migration_unverified",
                    "message": "a data revision must define verify(connection) to prove the transform",
                    "hint": "assert row counts and financial/inventory totals inside verify()",
                }
            )
        if not revision.revision:
            revision.problems.append({"code": "missing_revision", "message": "no REVISION (and the filename has none)"})
        if revision.destructive and not policy.allow_destructive:
            revision.problems.append(
                {
                    "code": "destructive_not_allowed",
                    "message": "revision is destructive and the policy forbids destructive changes here",
                    "hint": "run it manually with AMS_ALLOW_DESTRUCTIVE_MIGRATIONS=1 after a verified backup",
                }
            )
        record = applied.get(revision.global_revision)
        if record:
            revision.status = "APPLIED"
            revision.applied_checksum = record.get("checksum") or ""
            if record.get("checksum") and revision.checksum and record["checksum"] != revision.checksum:
                revision.status = "MODIFIED"
                revision.problems.append(
                    {
                        "code": "applied_revision_modified",
                        "message": "this file changed after it was applied "
                        f"(ledger {record['checksum'][:12]}…, file {revision.checksum[:12]}…)",
                        "hint": "never edit history: add a new revision that repairs the change",
                    }
                )
        else:
            revision.status = "PENDING"
        blocking = [p for p in revision.problems if p.get("code") != "destructive_statement_allowed"]
        if blocking and revision.status == "PENDING":
            revision.status = "REQUIRES_ATTENTION"
    return revisions


def plan(revisions: list[Revision]) -> dict:
    pending = [r for r in revisions if r.status == "PENDING"]
    attention = [r for r in revisions if r.status in {"REQUIRES_ATTENTION", "MODIFIED"}]
    applied = [r for r in revisions if r.status == "APPLIED"]
    return {
        "total_revisions": len(revisions),
        "pending": len(pending),
        "applied": len(applied),
        "requires_attention": len(attention),
        "pending_revisions": [r.global_revision for r in pending],
        "next_revision": pending[0].global_revision if pending else "",
        "highest_applied": applied[-1].global_revision if applied else "",
        "schema_version_after": len(revisions),
        "modules": sorted({r.module_id for r in pending}),
        "status": "ATTENTION" if attention else ("MIGRATION_REQUIRED" if pending else "OK"),
    }


def _default_migrations_dir(app=None) -> Path:
    if app is not None:
        configured = app.config.get("MIGRATIONS_DIR")
        if configured:
            return Path(configured)
        return Path(app.root_path) / "migrations"
    return Path(__file__).resolve().parents[2] / "app" / "migrations"
