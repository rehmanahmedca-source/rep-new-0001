"""The Module Registry: controlled discovery, validation and registration.

One object per Flask application (``app.extensions["ams_modules"]``).  It owns
the whole lifecycle described in ``docs/MODULE_CONTRACT.md``::

    discover  -> validate -> resolve dependencies -> register blueprints
              -> collect navigation -> (dbupdate) migrations -> health -> READY

Design rules that must not be eroded
------------------------------------
* **Discovery never executes a module.** Manifests are read as data
  (``module.toml``, or an AST literal read of a legacy ``MODULE_CONFIG``).
  A module's code is imported only after its manifest validates and is
  enabled.
* **Nothing is silently skipped.** Every module ends in an explicit status;
  a broken module keeps the precise problem list that explains it, and the
  application reports it instead of pretending it is not there.
* **Registration is additive.** A module that conflicts with an existing
  route prefix / endpoint is refused; the existing ERP keeps working.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from flask import Blueprint

from app.services.module_system import contract as C
from app.services.module_system.contract import (
    ManifestError,
    ManifestProblem,
    ModuleSpec,
    STATUS_DISABLED,
    STATUS_FAILED_VALIDATION,
    STATUS_MISSING_DEPENDENCY,
    STATUS_READY,
    STATUS_REGISTERED,
    STATUS_ROUTE_CONFLICT,
    STATUS_VALID,
)

LOG = logging.getLogger("ams.modules")
REGISTRY_KEY = "ams_modules"
#: Modules that the application factory registers itself.  A discovered module
#: may not take one of these names or prefixes, and they are never reported as
#: "new" just because a manifest was added for them.
CORE_BLUEPRINTS = (
    "core",
    "auth",
    "sales",
    "masters",
    "ledgers",
    "ops",
    "reports",
    "api",
    "system",
    "misc",
    "legacy_migration",
)


@dataclass
class RegistrationResult:
    module_id: str
    blueprints: list[str] = field(default_factory=list)
    endpoints: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    #: blueprints the application factory had already registered for this module
    reused: list[str] = field(default_factory=list)
    #: non-fatal mount observations (prefix overlap with a core blueprint, ...)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "module": self.module_id,
            "blueprints": self.blueprints,
            "reused": list(self.reused),
            "warnings": list(self.warnings),
            "endpoints": list(self.endpoints),
            "rules": list(self.rules),
            "error": self.error,
        }


_ABSENT = object()
_UNPARSABLE = object()


def _literal_from_ast(source: str, name: str):
    """Return the literal value assigned to module-level *name*.

    ``_ABSENT`` when the name is never assigned, ``_UNPARSABLE`` when it is
    assigned something the registry cannot read without executing the module
    (a call, an f-string, a computed dict).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _ABSENT
    found = _ABSENT
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                try:
                    found = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    found = _UNPARSABLE
    return found


class ModuleRegistry:
    """Holds every discovered module plus the reason it is in that state."""

    def __init__(self, module_root: str | os.PathLike, app_version: str = "1.0.0") -> None:
        #: url prefix -> module id, for the modules this registry mounted.  Used
        #: to detect two modules claiming one prefix (a core blueprint sharing a
        #: prefix is only a warning: the ERP has always layered routes).
        self.prefix_owners: dict[str, str] = {}
        self.module_root = Path(module_root).resolve()
        self.app_version = app_version
        self.specs: dict[str, ModuleSpec] = {}
        self.orphans: list[dict] = []  # manifest-less / unreadable candidates
        self.registrations: dict[str, RegistrationResult] = {}
        self.discovered = False
        self.registered = False
        self.core_tables: set[str] = set()
        self._nav_cache: list[dict] | None = None

    # -- discovery ---------------------------------------------------------
    def _core_table_names(self) -> set[str]:
        try:
            from models import db

            return set(db.metadata.tables)
        except Exception:  # pragma: no cover - models always importable
            return set()

    def _candidates(self) -> list[tuple[str, Path, Path]]:
        """Return ``(package_name, module_dir_or_file, manifest_path)`` triples."""
        out: list[tuple[str, Path, Path]] = []
        root = self.module_root
        if not root.is_dir():
            return out
        skip = {"__pycache__"}
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if child.name.startswith("__") or child.name in skip:
                continue
            if child.is_dir():
                if child.name.startswith("_"):
                    continue
                if child.is_symlink() and root not in child.resolve().parents:
                    self.orphans.append(
                        {
                            "path": str(child),
                            "status": STATUS_FAILED_VALIDATION,
                            "reason": "module directory is a symlink pointing outside the module root",
                        }
                    )
                    continue
                manifest = child / C.MANIFEST_NAME
                if manifest.is_file():
                    out.append((child.name, child, manifest))
                else:
                    out.append((child.name, child, manifest))  # missing manifest -> legacy path
                continue
            if child.suffix != ".py":
                continue
            out.append((child.stem, child, child.with_name(f"{child.stem}.toml")))
        return out

    def _legacy_probe(self, package: str, location: Path) -> tuple[dict | None, Path, str]:
        """Read a legacy ``MODULE_CONFIG`` without importing the module.

        Returns ``(raw_manifest_or_None, source_file, outcome)`` where outcome
        is one of ``manifest`` / ``opaque`` / ``none``.
        """
        sources = [location / "__init__.py", location / "_common.py"] if location.is_dir() else [location]
        first = sources[0] if sources else location
        for source in sources:
            if not source.is_file():
                continue
            try:
                text = source.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            config = _literal_from_ast(text, "MODULE_CONFIG")
            if config is _UNPARSABLE:
                return None, source, "opaque"
            if isinstance(config, dict):
                return C.legacy_config_to_raw(config, module_file=source, module_id_hint=package), source, "manifest"
        return None, first, "none"

    def _record(self, spec: ModuleSpec) -> ModuleSpec:
        existing = self.specs.get(spec.module_id)
        if existing is not None and existing.package != spec.package:
            spec.problems.append(
                ManifestProblem(
                    "duplicate_module_id",
                    "module.id",
                    f"module id '{spec.module_id}' is claimed by '{existing.package}' and '{spec.package}'",
                    "module ids must be unique across the application",
                )
            )
            spec.status = STATUS_FAILED_VALIDATION
        self.specs[spec.module_id] = spec
        return spec

    def discover(self, *, force: bool = False) -> list[ModuleSpec]:
        if self.discovered and not force:
            return list(self.specs.values())
        self.core_tables = self._core_table_names()
        known = {
            name
            for name in self._package_names()
        } | set(CORE_BLUEPRINTS)

        for package, location, manifest in self._candidates():
            raw: dict | None = None
            source = manifest
            origin = "toml"
            is_manifest = manifest.name == C.MANIFEST_NAME and manifest.is_file()
            if is_manifest:
                try:
                    raw = C.read_manifest(manifest)
                except ManifestError as exc:
                    self.orphans.append(
                        {
                            "path": str(manifest),
                            "status": STATUS_FAILED_VALIDATION,
                            "reason": str(exc),
                        }
                    )
                    continue
            else:
                raw, source, outcome = self._legacy_probe(package, location)
                origin = "legacy"
                if outcome == "opaque":
                    # A MODULE_CONFIG exists but is not a literal the registry
                    # can read safely.  Importing the module to find out would
                    # execute code before validation, so it is reported instead.
                    self.orphans.append(
                        {
                            "path": str(source),
                            "status": STATUS_FAILED_VALIDATION,
                            "reason": "MODULE_CONFIG is present but is not a literal dict, and the "
                            "registry does not execute module code to read metadata",
                            "hint": f"move the metadata into {package}/{'module.toml' if location.is_dir() else package + '.toml'}",
                        }
                    )
                    continue
                if raw is None:
                    # No manifest and no MODULE_CONFIG: still discover it (this
                    # is how the legacy packs work today) with a default prefix.
                    raw = {
                        "module": {"id": package, "name": package, "version": "0.0.0", "enabled": True},
                        "routes": {"url_prefix": f"/{package}"},
                    }
                    origin = "implicit"
            declared_prefix = str((raw.get("routes") or {}).get("url_prefix") or f"/{package}").strip()
            try:
                spec = C.validate_manifest(
                    raw,
                    module_root=location if location.is_dir() else location.parent,
                    package=f"{self.module_root.name}.{package}",
                    manifest_path=source,
                    known_modules=known,
                    existing_prefixes=dict(self.prefix_owners),
                    existing_endpoint_owners={},
                    registered_tables=self.core_tables - set(_declared_tables(raw)),
                    app_version=self.app_version,
                )
            except ManifestError as exc:
                self.orphans.append(
                    {
                        "path": str(source),
                        "status": STATUS_FAILED_VALIDATION,
                        "reason": str(exc),
                    }
                )
                continue
            spec.source = origin
            if declared_prefix:
                self.prefix_owners.setdefault(declared_prefix.rstrip("/") or declared_prefix, spec.module_id)
            self._record(spec)

        self.discovered = True
        self._resolve_dependencies()
        return list(self.specs.values())

    def _package_names(self) -> set[str]:
        root = self.module_root
        names: set[str] = set()
        if not root.is_dir():
            return names
        for child in root.iterdir():
            if child.name.startswith("_") or child.name == "__pycache__":
                continue
            if child.is_dir() or child.suffix == ".py":
                names.add(child.stem)
        return names

    # -- dependency graph --------------------------------------------------
    def resolve_dependencies(self) -> None:
        """Public entry to the dependency pass (see :meth:`_resolve_dependencies`)."""
        self._resolve_dependencies()

    def _resolve_dependencies(self) -> None:
        specs = self.specs
        cycles = _find_cycles(specs)
        for module_id, spec in specs.items():
            if not spec.enabled:
                spec.status = STATUS_DISABLED
                continue
            missing = [dep for dep in spec.depends_on if dep not in specs]
            blocked = [
                dep
                for dep in spec.depends_on
                if dep in specs and specs[dep].status in {STATUS_FAILED_VALIDATION, STATUS_DISABLED}
            ]
            for dep in missing:
                spec.problems.append(
                    ManifestProblem(
                        "missing_dependency",
                        "module.depends_on",
                        f"required module '{dep}' is not installed",
                        f"install the '{dep}' module or remove the dependency",
                    )
                )
            for dep in blocked:
                spec.problems.append(
                    ManifestProblem(
                        "dependency_failed",
                        "module.depends_on",
                        f"required module '{dep}' is {specs[dep].status}",
                        "fix the dependency first; it is the root cause",
                    )
                )
            if module_id in cycles:
                spec.problems.append(
                    ManifestProblem(
                        "dependency_cycle",
                        "module.depends_on",
                        f"dependency cycle: {' -> '.join(cycles[module_id])}",
                        "break the cycle by extracting the shared code into one of the modules",
                    )
                )
            if missing or blocked or module_id in cycles:
                spec.status = STATUS_MISSING_DEPENDENCY
                continue
            if spec.errors():
                spec.status = STATUS_FAILED_VALIDATION
                continue
            spec.status = STATUS_VALID

    # -- registration ------------------------------------------------------
    def _blueprints_of(self, module_id: str, spec: ModuleSpec) -> list[Blueprint]:
        package = spec.package
        module = importlib.import_module(package)
        if spec.models_import:
            try:
                importlib.import_module(spec.models_import)
            except Exception as exc:  # surfaces as module:import_failed below
                raise RuntimeError(f"models_import '{spec.models_import}' failed: {exc}") from exc
        named = getattr(module, spec.blueprint_variable, None) if spec.blueprint_variable else None
        if isinstance(named, Blueprint):
            return [named]
        found = [obj for _, obj in inspect.getmembers(module, lambda o: isinstance(o, Blueprint))]
        # ``from ._common import *`` re-exports a blueprint that may also be
        # reachable under more than one attribute name; dedupe on identity.
        unique: list[Blueprint] = []
        seen: set[int] = set()
        for bp in found:
            if id(bp) in seen:
                continue
            seen.add(id(bp))
            unique.append(bp)
        return unique

    def register(self, app) -> dict:
        """Import and mount every VALID module, in dependency order."""
        if self.registered:
            return {"registered": list(self.registrations)}
        if not self.discovered:
            self.discover()
        summary = {"registered": [], "skipped": [], "failed": []}
        order = sorted(
            (s for s in self.specs.values() if s.status == STATUS_VALID),
            key=lambda s: (s.order, s.module_id),
        )
        for wave in _topological_waves(self.specs, order):
            for spec in wave:
                if app is None:
                    break
                try:
                    result = self._mount(app, spec)
                except Exception as exc:
                    spec.status = STATUS_FAILED_VALIDATION
                    spec.problems.append(
                        ManifestProblem("import_failed", "module", f"{type(exc).__name__}: {exc}", severity="error")
                    )
                    summary["failed"].append({"module": spec.module_id, "error": f"{type(exc).__name__}: {exc}"})
                    LOG.exception("module '%s' failed to import", spec.module_id)
                    continue
                self.registrations[spec.module_id] = result
                if result.error:
                    spec.status = STATUS_ROUTE_CONFLICT if "conflict" in result.error else STATUS_FAILED_VALIDATION
                    spec.problems.append(ManifestProblem("registration_failed", "module", result.error))
                    summary["failed"].append({"module": spec.module_id, "error": result.error})
                    continue
                spec.status = STATUS_REGISTERED
                if spec.url_prefix:
                    self.prefix_owners[(spec.url_prefix or "").rstrip("/") or spec.url_prefix] = spec.module_id
                for note in result.warnings:
                    spec.problems.append(
                        C.ManifestProblem("prefix_overlap", "routes.url_prefix", note, severity="warning")
                    )
                summary["registered"].append(spec.module_id)
        for spec in self.specs.values():
            if spec.status != STATUS_VALID:
                summary["skipped"].append({"module": spec.module_id, "status": spec.status})
        self.registered = True
        return summary

    def _mount(self, app, spec: ModuleSpec) -> RegistrationResult:
        result = RegistrationResult(module_id=spec.module_id)
        before_rules = {r.rule for r in app.url_map.iter_rules()}
        found = self._blueprints_of(spec.module_id, spec)
        if not found:
            # Never register an "empty" module: a directory the registry found but
            # which exposes no Blueprint is a discovery/structure mistake, and it
            # has to say so instead of showing up as a loaded module.
            result.error = (
                f"package '{spec.package}' exposes no Flask Blueprint "
                f"(expected '{spec.blueprint_variable or spec.module_id + '_bp'}' or any Blueprint object)"
            )
            return result
        for bp in found:
            if bp.name in app.blueprints:
                result.reused.append(bp.name)
                continue
            prefix = spec.url_prefix or f"/{bp.name}"
            other_module = self.prefix_owners.get((prefix or "").rstrip("/") or prefix)
            if other_module and other_module != spec.module_id:
                result.error = (
                    f"route conflict: url prefix '{prefix}' is claimed by module '{other_module}'"
                )
                return result
            overlap = _prefix_owner(app, prefix)
            if overlap and overlap not in (spec.module_id, bp.name):
                # Not fatal: the ERP deliberately layers some routes (for example
                # a helper blueprint under /admin).  It is recorded so a reviewer
                # can see that two owners serve one prefix.
                result.warnings.append(
                    f"url prefix '{prefix}' also serves blueprint '{overlap}'"
                )
            try:
                app.register_blueprint(bp, url_prefix=prefix)
            except AssertionError as exc:
                result.error = f"route conflict while registering '{bp.name}': {exc}"
                return result
            result.blueprints.append(bp.name)
        # Verify declared endpoints actually exist now.
        for endpoint in spec.expected_endpoints:
            if endpoint not in app.view_functions:
                result.error = (
                    f"declared endpoint '{endpoint}' is not registered by this module; "
                    "update routes.expected_endpoints or fix the view name"
                )
                return result
            result.endpoints.append(endpoint)
        new_rules = sorted({r.rule for r in app.url_map.iter_rules()} - before_rules)
        result.rules = new_rules
        return result

    # -- navigation -------------------------------------------------------
    def navigation(self, app=None, *, user_permissions=None) -> list[dict]:
        """Module-declared sidebar items, validated against the live url map."""
        items: list[dict] = []
        for spec in self.specs.values():
            if not spec.enabled or not spec.navigation:
                continue
            for nav in spec.navigation:
                entry = nav.as_dict()
                entry.update(
                    {
                        "module": spec.module_id,
                        "module_version": spec.version,
                        "registered": spec.status in {STATUS_REGISTERED, STATUS_READY, STATUS_VALID}
                        and bool(spec.module_id in self.registrations),
                        "resolvable": True,
                        "visible_to": "authenticated",
                    }
                )
                if nav.permission:
                    entry["visible_to"] = "permissioned"
                    if user_permissions is not None and not _permission_granted(nav.permission, user_permissions):
                        entry["granted"] = False
                    else:
                        entry["granted"] = True
                else:
                    entry["granted"] = True
                if app is not None and nav.endpoint:
                    entry["resolvable"] = nav.endpoint in getattr(app, "view_functions", {})
                items.append(entry)
        items.sort(key=lambda item: (int(item.get("order") or 0), str(item.get("id") or "")))
        return items

    # -- health -----------------------------------------------------------
    def health(self, app=None, *, module_ids=None) -> dict:
        """Per-module health: routes, tables, permissions and declared checks.

        Kept on the registry because three callers need the same answer — the
        update pipeline, ``/admin/modules`` and ``tools/dbupdate.py tests`` — and
        a second implementation would drift.
        """
        from app.services.module_system.health import run_module_health

        scope = app if app is not None else getattr(self, "app", None)
        if scope is None:
            return {"status": "SKIPPED", "detail": "no application to check against", "modules": {}}
        return run_module_health(scope, self, module_ids=module_ids)

    # -- reporting --------------------------------------------------------
    def statuses(self) -> dict[str, list[str]]:
        buckets: dict[str, list[str]] = {}
        for spec in self.specs.values():
            buckets.setdefault(spec.status, []).append(spec.module_id)
        for status in C.STATUS_SEVERITY:
            buckets.setdefault(status, [])
        return {k: sorted(v) for k, v in sorted(buckets.items(), key=lambda kv: _severity_index(kv[0]))}

    def report(self, app=None) -> dict:
        buckets = self.statuses()
        failed = [
            {
                "module": spec.module_id,
                "status": spec.status,
                "problems": [p.as_dict() for p in spec.problems if p.severity == "error"],
            }
            for spec in sorted(self.specs.values(), key=lambda s: s.module_id)
            if spec.status
            in {
                STATUS_FAILED_VALIDATION,
                STATUS_MISSING_DEPENDENCY,
                STATUS_ROUTE_CONFLICT,
            }
        ]
        return {
            "module_root": str(self.module_root),
            "contract_schema_api": C.SCHEMA_API,
            "app_version": self.app_version,
            "discovered": len(self.specs),
            "registered": len(self.registrations),
            "statuses": buckets,
            "ready_count": len([s for s in self.specs.values() if s.status == STATUS_READY]),
            "registered_count": len(self.registrations),
            "pending": sorted(self.pending_migration_modules()),
            "disabled": sorted(
                s.module_id for s in self.specs.values() if s.status in (STATUS_DISABLED,)
            ),
            "policy": {
                "auto_apply": bool(getattr(self, "auto_apply", True)),
                "allow_destructive": bool(getattr(self, "allow_destructive", False)),
            },
            "modules": [spec.as_dict() for spec in sorted(self.specs.values(), key=lambda s: s.module_id)],
            "orphans": self.orphans,
            "failed": failed,
            "navigation": self.navigation(app),
            "migration_plan": self.migration_plan(app),
            "core_blueprints": list(CORE_BLUEPRINTS),
        }

    def migration_plan(self, app=None) -> dict:
        """Pending revisions per module, derived from the ledger (never mutated here)."""
        summary: dict = {"total_revisions": 0, "pending": 0, "applied": 0, "by_module": {}}
        try:
            from app.services.dbupdate import ledger
            from app.services.dbupdate import migrations as MIG
        except Exception:
            return summary
        try:
            scope = app or getattr(self, "app", None)
            revisions = MIG.collect(scope, registry=self) if scope is not None else []
            applied: dict = {}
            if scope is not None:
                with scope.app_context():
                    ledger.ensure_ledger(allow_create=False)
                    applied = ledger.applied_revisions()
            for revision in revisions:
                summary["total_revisions"] += 1
                if revision.global_revision in applied:
                    summary["applied"] += 1
                else:
                    summary["pending"] += 1
                summary["by_module"].setdefault(revision.module_id, []).append(
                    {
                        "revision": revision.global_revision,
                        "title": revision.title,
                        "kind": revision.kind,
                        "status": "APPLIED" if revision.global_revision in applied else "PENDING",
                        "destructive": bool(revision.destructive),
                    }
                )
        except Exception:
            LOG.debug("migration_plan unavailable", exc_info=True)
        return summary

    def ready(self) -> bool:
        return not [
            spec
            for spec in self.specs.values()
            if spec.status
            in {STATUS_FAILED_VALIDATION, STATUS_MISSING_DEPENDENCY, STATUS_ROUTE_CONFLICT}
        ]

    def pending_migration_modules(self) -> list[str]:
        return sorted(
            spec.module_id
            for spec in self.specs.values()
            if spec.enabled and (spec.migrations or spec.data_migrations)
        )

    def mark_ready(self, module_ids) -> None:
        for module_id in module_ids:
            spec = self.specs.get(module_id)
            if spec is not None and spec.status == STATUS_REGISTERED:
                spec.status = STATUS_READY


def _declared_tables(raw: dict) -> list[str]:
    return list(((raw.get("database") or {}).get("tables") or []))


def _severity_index(status: str) -> int:
    try:
        return C.STATUS_SEVERITY.index(status)
    except ValueError:
        return len(C.STATUS_SEVERITY)


def _prefix_owner(app, prefix: str) -> str:
    """Which blueprint already serves URLs under *prefix*?

    ``Blueprint.url_prefix`` is not retained after registration on current
    Flask, so ownership must be derived from the live url map — otherwise two
    modules can be mounted on the same prefix and nothing reports it.
    """
    wanted = (prefix or "").rstrip("/")
    if not wanted:
        return ""
    for rule in app.url_map.iter_rules():
        endpoint = str(getattr(rule, "endpoint", "") or "")
        owner = endpoint.split(".", 1)[0] if "." in endpoint else endpoint
        if not owner or owner == "static":
            continue
        path = str(rule.rule).rstrip("/")
        if path == wanted or path.startswith(wanted + "/") or path.startswith(wanted + "<"):
            return owner
    return ""


def _permission_granted(permission: str, user_permissions) -> bool:
    if isinstance(user_permissions, (set, list, tuple)):
        return permission in set(user_permissions)
    return bool(getattr(user_permissions, permission, False))


def _find_cycles(specs: dict[str, ModuleSpec]) -> dict[str, list[str]]:
    """Return ``{module_id: cycle_path}`` for every module inside a cycle."""
    graph = {mid: [d for d in spec.depends_on if d in specs] for mid, spec in specs.items()}
    state: dict[str, int] = {}
    path: list[str] = []
    found: dict[str, list[str]] = {}

    def visit(node: str) -> None:
        if state.get(node) == 1:
            start = path.index(node) if node in path else 0
            cycle = path[start:] + [node]
            for member in set(cycle):
                found.setdefault(member, cycle)
            return
        if state.get(node) == 2:
            return
        state[node] = 1
        path.append(node)
        for dep in graph.get(node, []):
            visit(dep)
        path.pop()
        state[node] = 2

    for node in graph:
        visit(node)
    return found


def _topological_waves(specs: dict[str, ModuleSpec], ready: list[ModuleSpec]) -> list[list[ModuleSpec]]:
    """Group modules so every dependency is mounted before its dependents."""
    remaining = {spec.module_id for spec in ready}
    waves: list[list[ModuleSpec]] = []
    while remaining:
        wave = [
            spec
            for mid, spec in ((s.module_id, s) for s in ready)
            if mid in remaining and all(dep not in remaining for dep in spec.depends_on)
        ]
        if not wave:  # defensive: cycles are already reported during validation
            waves.append([specs[mid] for mid in sorted(remaining) if mid in specs])
            break
        waves.append(wave)
        remaining -= {spec.module_id for spec in wave}
    return waves


def get_registry(app=None) -> ModuleRegistry | None:
    if app is None:
        try:
            from flask import current_app

            app = current_app._get_current_object()  # type: ignore[attr-defined]
        except Exception:
            return None
    if app is None:
        return None
    return app.extensions.get(REGISTRY_KEY) if getattr(app, "extensions", None) else None


def attach(app, module_root: str | os.PathLike, *, app_version: str = "1.0.0") -> ModuleRegistry:
    registry = ModuleRegistry(module_root, app_version=app_version)
    app.extensions.setdefault(REGISTRY_KEY, registry)
    return registry
