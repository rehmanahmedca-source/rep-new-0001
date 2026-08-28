"""The AMS module contract: a *declarative*, validated module manifest.

Why a manifest file instead of ``MODULE_CONFIG`` in the module's own code
------------------------------------------------------------------------
``blueprints/<name>.py`` used to carry a ``MODULE_CONFIG`` dict.  Reading that
means **importing** (and therefore executing) whatever a directory happens to
contain.  Discovery must not execute untrusted code, so the contract is now a
passive ``module.toml`` file read with :mod:`tomllib`: discovery can validate a
module completely before a single line of its code runs.

Contract location
-----------------
``<module_root>/<module_dir>/module.toml``  — package modules (preferred)
``<module_root>/<name>.py`` with ``MODULE_CONFIG`` — legacy single-file form,
still honoured, converted into the same shape by :func:`legacy_config_to_raw`.

Minimum valid manifest::

    [module]
    id      = "plant_registry"          # [a-z][a-z0-9_]{2,40}
    name    = "Plant & Equipment Register"
    version = "1.0.0"
    enabled = true

Everything else is optional; each optional block is validated only when
present, and every problem is reported with a field path and a fix hint.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MANIFEST_NAME = "module.toml"
SCHEMA_API = 1  # bump when the contract itself changes incompatibly

_MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?([.-][0-9A-Za-z.-]+)?$")
_ENDPOINT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ICON_RE = re.compile(r"^bi-[a-z0-9-]+$")
_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

#: Statuses the registry may hold.  These names are part of the report format.
STATUS_DISCOVERED = "DISCOVERED"
STATUS_VALID = "VALID"
STATUS_REGISTERED = "REGISTERED"
STATUS_DISABLED = "DISABLED"
STATUS_FAILED_VALIDATION = "FAILED_VALIDATION"
STATUS_MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
STATUS_ROUTE_CONFLICT = "ROUTE_CONFLICT"
STATUS_MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
STATUS_FAILED_HEALTH = "FAILED_HEALTH"
STATUS_READY = "READY"

#: Ordering used by the report so the worst state always sorts to the top.
STATUS_SEVERITY = (
    STATUS_FAILED_VALIDATION,
    STATUS_MISSING_DEPENDENCY,
    STATUS_ROUTE_CONFLICT,
    STATUS_MIGRATION_REQUIRED,
    STATUS_FAILED_HEALTH,
    STATUS_DISCOVERED,
    STATUS_REGISTERED,
    STATUS_VALID,
    STATUS_READY,
    STATUS_DISABLED,
)


class ManifestProblem:
    """One precise validation failure: what, where, why, and what to do."""

    __slots__ = ("code", "field", "message", "hint", "severity")

    def __init__(
        self,
        code: str,
        field: str,
        message: str,
        hint: str = "",
        severity: str = "error",
    ) -> None:
        self.code = code
        self.field = field
        self.message = message
        self.hint = hint
        self.severity = severity

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "field": self.field,
            "message": self.message,
            "hint": self.hint,
            "severity": self.severity,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ManifestProblem {self.code} {self.field}: {self.message}>"


class ManifestError(ValueError):
    """Raised when a manifest cannot be read at all (malformed TOML, unsafe path)."""


@dataclass
class NavItem:
    id: str
    label: str
    endpoint: str
    icon: str = "bi-link"
    parent: str = ""
    order: int = 100
    permission: str = ""
    active_prefix: str = ""
    methods: tuple[str, ...] = ("GET",)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "endpoint": self.endpoint,
            "icon": self.icon,
            "parent": self.parent,
            "order": self.order,
            "permission": self.permission,
            "active_prefix": self.active_prefix or _endpoint_to_prefix_guess(self.endpoint),
            "methods": list(self.methods),
        }


@dataclass
class MigrationRef:
    version: str
    slug: str
    file: str
    #: declared intent: "" (derive from the section), "schema", "data" or "index"
    kind: str = ""
    destructive: bool = False
    requires_data_validation: bool = False
    checksum: str = ""
    absolute_path: str = ""

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "kind": self.kind,
            "slug": self.slug,
            "file": self.file,
            "destructive": self.destructive,
            "requires_data_validation": self.requires_data_validation,
            "checksum": self.checksum,
        }


@dataclass
class ModuleSpec:
    """A fully validated module manifest, ready to be registered."""

    module_id: str
    name: str
    version: str
    description: str
    enabled: bool
    root: str
    package: str
    manifest_path: str
    module_file: str = ""
    blueprint_variable: str = ""
    url_prefix: str = ""
    order: int = 100
    depends_on: tuple[str, ...] = ()
    expected_endpoints: tuple[str, ...] = ()
    navigation: tuple[NavItem, ...] = ()
    permission_required: str = ""
    permission_defaults: dict = field(default_factory=dict)
    tables: tuple[str, ...] = ()
    models_import: str = ""
    migrations: tuple[MigrationRef, ...] = ()
    data_migrations: tuple[MigrationRef, ...] = ()
    health_checks: tuple[str, ...] = ()
    test_paths: tuple[str, ...] = ()
    features: dict = field(default_factory=dict)
    schema_api: int = SCHEMA_API
    min_ams_version: str = ""
    status: str = STATUS_DISCOVERED
    problems: list[ManifestProblem] = field(default_factory=list)
    source: str = "toml"

    @property
    def ok(self) -> bool:
        return not [p for p in self.problems if p.severity == "error"]

    def errors(self) -> list[ManifestProblem]:
        return [p for p in self.problems if p.severity == "error"]

    def warnings(self) -> list[ManifestProblem]:
        return [p for p in self.problems if p.severity == "warning"]

    def as_dict(self) -> dict:
        return {
            "id": self.module_id,
            "module_id": self.module_id,
            "source": self.source,
            "root": self.root,
            "module_file": self.module_file,
            "blueprint_variable": self.blueprint_variable,
            "min_ams_version": self.min_ams_version,
            "ok": self.ok,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "enabled": self.enabled,
            "package": self.package,
            "manifest": self.manifest_path,
            "url_prefix": self.url_prefix,
            "order": self.order,
            "status": self.status,
            "schema_api": self.schema_api,
            "depends_on": list(self.depends_on),
            "routes": {"expected_endpoints": list(self.expected_endpoints)},
            "navigation": [n.as_dict() for n in self.navigation],
            "permissions": {
                "required": self.permission_required,
                "defaults": dict(self.permission_defaults),
            },
            "database": {
                "tables": list(self.tables),
                "models_import": self.models_import,
                "migrations": [m.as_dict() for m in self.migrations],
                "data_migrations": [m.as_dict() for m in self.data_migrations],
            },
            "health_checks": list(self.health_checks),
            "tests": list(self.test_paths),
            "features": dict(self.features),
            "problems": [p.as_dict() for p in self.problems],
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _endpoint_to_prefix_guess(endpoint: str) -> str:
    head = (endpoint or "").split(".", 1)[0]
    return f"/{head}" if head else ""


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return default


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    raise TypeError(f"expected a string or list of strings, got {type(value).__name__}")


def _as_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _version_key(raw: str) -> tuple[int, ...]:
    core = re.split(r"[.-]", str(raw or "0"), maxsplit=1)[0]
    parts = core.split(".")
    out = []
    for part in parts[:3]:
        digits = re.match(r"^\d+", part)
        out.append(int(digits.group(0)) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def version_satisfies(actual: str, requirement: str) -> bool:
    """Tiny dependency specifier check: ``">=1.2"``, ``"==1.0.0"``, ``"=1"``."""
    if not requirement:
        return True
    req = requirement.strip()
    match = re.match(r"^(>=|<=|==|=|>|<|\^)?\s*(.+)$", req)
    if not match:
        return False
    op, raw = match.group(1) or ">=", match.group(2)
    have, want = _version_key(actual), _version_key(raw)
    if op in ("==", "="):
        return have[: len(want)] == want or have == want
    if op == ">=":
        return have >= want
    if op == "<=":
        return have <= want
    if op == ">":
        return have > want
    if op == "<":
        return have < want
    if op == "^":
        return have >= want and have[0] == want[0]
    return False


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(256 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def read_manifest(path: Path) -> dict:
    """Load ``module.toml``; raise :class:`ManifestError` on an unreadable file."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"manifest is not readable: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"manifest is not valid TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest top level must be a TOML table")
    return data


def legacy_config_to_raw(module_config: dict, *, module_file: Path, module_id_hint: str = "") -> dict:
    """Translate a legacy in-module ``MODULE_CONFIG`` dict into manifest shape.

    ``module_id_hint`` is the discovery name (the directory, or the ``.py``
    stem).  It wins over the file stem because legacy packages keep their
    config in ``_common.py``, where the stem would be ``_common``.
    """
    cfg = dict(module_config or {})
    module_id = str(cfg.get("id") or module_id_hint or module_file.stem).strip()
    raw: dict = {
        "module": {
            "id": module_id,
            "name": cfg.get("name") or module_id,
            "version": str(cfg.get("version") or "0.0.0"),
            "description": str(cfg.get("description") or ""),
            "enabled": _as_bool(cfg.get("enabled"), default=True),
            "schema_api": cfg.get("schema_api", SCHEMA_API),
            "depends_on": list(cfg.get("depends_on") or []),
        },
        "routes": {"url_prefix": cfg.get("url_prefix") or f"/{module_id}"},
    }
    if cfg.get("allowed_roles"):
        raw["permissions"] = {"allowed_roles": list(cfg["allowed_roles"])}
    if cfg.get("author"):
        raw["module"]["authors"] = [str(cfg["author"])]
    return raw


def _relative_or_abs(path: Path, base: Path) -> str:
    """Repo-relative display path when possible, absolute otherwise."""
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return str(path)


def _check_str(
    value: Any,
    *,
    name: str,
    problems: list[ManifestProblem],
    required: bool = True,
    pattern: re.Pattern | None = None,
    hint: str = "",
) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            problems.append(
                ManifestProblem("missing_field", name, "required field is missing or empty", hint or f"set {name}")
            )
        return ""
    text = str(value).strip()
    if pattern is not None and not pattern.match(text):
        problems.append(
            ManifestProblem("bad_format", name, f"'{text}' does not match {pattern.pattern}", hint)
        )
    return text


def _resolve_inside(root: Path, relative: str, problems: list[ManifestProblem], where: str) -> Path | None:
    """Resolve *relative* under *root*, rejecting escapes/symlink traversal."""
    if not relative:
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        problems.append(
            ManifestProblem(
                "path_escape",
                where,
                f"'{relative}' resolves outside the module directory ({candidate})",
                "migration/health paths must stay inside the module directory",
            )
        )
        return None
    return candidate


_MIGRATION_KINDS = ("schema", "data", "index")


def _migration_refs(
    entries: Any,
    *,
    module_root: Path,
    where: str,
    problems: list[ManifestProblem],
    require_existing: bool,
) -> tuple[MigrationRef, ...]:
    refs: list[MigrationRef] = []
    if entries in (None, [], ()):
        return ()
    if isinstance(entries, (dict,)):
        entries = [entries]
    if not isinstance(entries, (list, tuple)):
        problems.append(ManifestProblem("bad_type", where, "must be a list of tables"))
        return ()
    for index, entry in enumerate(entries):
        loc = f"{where}[{index}]"
        if not isinstance(entry, dict):
            problems.append(ManifestProblem("bad_type", loc, "each migration must be a table"))
            continue
        file_rel = _check_str(
            entry.get("file"),
            name=f"{loc}.file",
            problems=problems,
            hint="path to the migration file, relative to the module directory",
        )
        version = _check_str(
            entry.get("version"),
            name=f"{loc}.version",
            problems=problems,
            pattern=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$"),
            hint="unique ordered id, e.g. 2026_001 or 0001",
        )
        slug = str(entry.get("slug") or (Path(file_rel).stem if file_rel else "")).strip()
        path = _resolve_inside(module_root, file_rel, problems, loc)
        if path is not None and require_existing and not path.is_file():
            problems.append(
                ManifestProblem("missing_file", f"{loc}.file", f"migration file not found: {file_rel}")
            )
        declared_kind = str(entry.get("kind") or "").strip().lower()
        if declared_kind and declared_kind not in _MIGRATION_KINDS:
            problems.append(
                ManifestProblem(
                    "bad_migration_kind",
                    f"{loc}.kind",
                    f"'{declared_kind}' is not one of {', '.join(sorted(_MIGRATION_KINDS))}",
                    "declare the kind only to state intent; the revision file decides",
                )
            )
            declared_kind = ""
        refs.append(
            MigrationRef(
                version=version,
                kind=declared_kind,
                slug=slug,
                file=file_rel,
                destructive=_as_bool(entry.get("destructive"), default=False),
                requires_data_validation=_as_bool(entry.get("requires_data_validation"), default=False),
                checksum=sha256_file(path) if path is not None and path.is_file() else "",
                absolute_path=str(path) if path is not None else "",
            )
        )
    return tuple(refs)


def _nav_items(
    entries: Any,
    *,
    where: str,
    problems: list[ManifestProblem],
) -> tuple[NavItem, ...]:
    items: list[NavItem] = []
    if entries in (None, [], ()):
        return ()
    if not isinstance(entries, list):
        problems.append(ManifestProblem("bad_type", where, "navigation.items must be a list of tables"))
        return ()
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        loc = f"{where}[{index}]"
        if not isinstance(entry, dict):
            problems.append(ManifestProblem("bad_type", loc, "each navigation item must be a table"))
            continue
        nav_id = _check_str(
            entry.get("id"),
            name=f"{loc}.id",
            problems=problems,
            pattern=re.compile(r"^[a-z][a-z0-9_.-]{2,60}$"),
            hint="lower-case unique id, e.g. plant_registry.list",
        )
        if nav_id and nav_id in seen:
            problems.append(ManifestProblem("duplicate_nav_id", f"{loc}.id", f"duplicate navigation id '{nav_id}'"))
        seen.add(nav_id)
        label = _check_str(entry.get("label"), name=f"{loc}.label", problems=problems)
        endpoint = _check_str(
            entry.get("endpoint"),
            name=f"{loc}.endpoint",
            problems=problems,
            pattern=_ENDPOINT_RE,
            hint="flask endpoint name, e.g. plant_registry.index",
        )
        icon = str(entry.get("icon") or "bi-link").strip()
        if icon and not _ICON_RE.match(icon):
            problems.append(
                ManifestProblem("bad_icon", f"{loc}.icon", f"'{icon}' is not a bootstrap-icons class", "use e.g. bi-building")
            )
        permission = str(entry.get("permission") or "").strip()
        if permission and not _IDENT_RE.match(permission):
            problems.append(ManifestProblem("bad_permission", f"{loc}.permission", f"'{permission}' is not an identifier"))
        items.append(
            NavItem(
                id=nav_id or f"{loc}",
                label=label or nav_id or "Untitled",
                endpoint=endpoint,
                icon=icon,
                parent=str(entry.get("parent") or "").strip(),
                order=_as_int(entry.get("order"), default=100 + index * 10),
                permission=permission,
                active_prefix=str(entry.get("active_prefix") or "").strip(),
            )
        )
    return tuple(items)


#: The manifest keys the validator understands, per section.  A key outside
#: these sets is a warning rather than an error: a typo such as ``depnds_on``
#: would otherwise be read as "no dependencies" and silently succeed.
ALLOWED_KEYS: dict[str, tuple[str, ...]] = {
    "": ("module", "routes", "permissions", "navigation", "database", "health", "tests", "features"),
    "module": (
        "id",
        "name",
        "version",
        "description",
        "enabled",
        "schema_api",
        "requires_ams",
        "depends_on",
    ),
    "routes": ("url_prefix", "blueprint_variable", "order", "expected_endpoints"),
    "permissions": ("required", "defaults", "allowed_roles"),
    "navigation": ("items",),
    "database": ("tables", "models_import", "migrations", "data_migrations"),
    "health": ("checks",),
    "tests": ("paths",),
    # [features] is an explicit free-form namespace for module-owned switches.
    "features": None,
}


def _unknown_keys(raw: dict, problems: list["ManifestProblem"]) -> None:
    if not isinstance(raw, dict):
        return
    for section in ("", "module", "routes", "permissions", "navigation", "database", "health", "tests"):
        allowed = ALLOWED_KEYS.get(section)
        body = raw if not section else (raw.get(section) or {})
        if not isinstance(body, dict) or allowed is None:
            continue
        for key in body:
            if str(key) in allowed:
                continue
            where = f"{section}.{key}" if section else str(key)
            problems.append(
                ManifestProblem(
                    "unknown_key",
                    where,
                    f"'{key}' is not part of schema API {SCHEMA_API}",
                    "remove it, or move it under [features] if the module reads it itself; "
                    f"known here: {', '.join(sorted(allowed))}",
                    severity="warning",
                )
            )


def validate_manifest(
    raw: dict,
    *,
    module_root: Path,
    package: str,
    manifest_path: Path,
    known_modules: Iterable[str] = (),
    existing_prefixes: dict[str, str] | None = None,
    existing_endpoint_owners: dict[str, str] | None = None,
    registered_tables: Iterable[str] = (),
    app_version: str = "1.0.0",
) -> ModuleSpec:
    """Validate one parsed manifest against the live application context.

    Returns a :class:`ModuleSpec` whose ``problems`` list is authoritative —
    nothing here raises for a *bad but readable* manifest, because the registry
    must be able to report every module's failure, not just the first one.
    """
    problems: list[ManifestProblem] = []
    _unknown_keys(raw, problems)
    existing_prefixes = dict(existing_prefixes or {})
    existing_endpoint_owners = dict(existing_endpoint_owners or {})
    known = set(known_modules)
    tables_present = set(registered_tables or ())

    module = raw.get("module")
    if not isinstance(module, dict):
        raise ManifestError("manifest must contain a [module] table")

    module_id = _check_str(
        module.get("id"),
        name="module.id",
        problems=problems,
        pattern=_MODULE_ID_RE,
        hint="lower-case, starts with a letter, 3-40 chars, underscores only",
    )
    name = _check_str(module.get("name"), name="module.name", problems=problems) or module_id
    version = _check_str(
        module.get("version"),
        name="module.version",
        problems=problems,
        pattern=_VERSION_RE,
        hint="use a dotted version such as 1.0.0",
    )
    description = str(module.get("description") or "").strip()
    if not description:
        problems.append(
            ManifestProblem(
                "missing_field",
                "module.description",
                "description is empty",
                "one sentence explaining what the module does and who uses it",
                severity="warning",
            )
        )
    enabled = _as_bool(module.get("enabled"), default=True)

    schema_api = _as_int(module.get("schema_api"), default=SCHEMA_API)
    if schema_api != SCHEMA_API:
        problems.append(
            ManifestProblem(
                "unsupported_schema_api",
                "module.schema_api",
                f"module declares schema_api {schema_api}, this application implements {SCHEMA_API}",
                "upgrade the module to the current contract, or run an application that supports it",
            )
        )

    min_ams = str(module.get("requires_ams") or "").strip()
    if min_ams and not version_satisfies(app_version, min_ams):
        problems.append(
            ManifestProblem(
                "incompatible_ams",
                "module.requires_ams",
                f"module requires AMS {min_ams}; running {app_version}",
                "update the application or lower the requirement if it is a typo",
            )
        )

    try:
        depends_on = _as_tuple(module.get("depends_on"))
    except TypeError as exc:
        problems.append(ManifestProblem("bad_type", "module.depends_on", str(exc)))
        depends_on = ()
    if module_id and package:
        expected_dir = Path(package.split(".")[-1]).name
        if module_id != expected_dir:
            problems.append(
                ManifestProblem(
                    "id_directory_mismatch",
                    "module.id",
                    f"id '{module_id}' does not match the directory name '{expected_dir}'",
                    "the directory name is the module id; keep them identical",
                )
            )

    routes = raw.get("routes") or {}
    if not isinstance(routes, dict):
        problems.append(ManifestProblem("bad_type", "routes", "must be a table"))
        routes = {}
    url_prefix = str(routes.get("url_prefix") or f"/{module_id or ''}").strip()
    if url_prefix and not url_prefix.startswith("/"):
        problems.append(ManifestProblem("bad_prefix", "routes.url_prefix", f"'{url_prefix}' must start with '/'"))
        url_prefix = f"/{url_prefix}"
    if url_prefix in ("/", "") and module_id:
        url_prefix = f"/{module_id}"
    blueprint_variable = str(routes.get("blueprint_variable") or "").strip()
    order = _as_int(routes.get("order"), default=100)
    try:
        expected_endpoints = _as_tuple(routes.get("expected_endpoints"))
    except TypeError as exc:
        problems.append(ManifestProblem("bad_type", "routes.expected_endpoints", str(exc)))
        expected_endpoints = ()

    permissions = raw.get("permissions") or {}
    if not isinstance(permissions, dict):
        problems.append(ManifestProblem("bad_type", "permissions", "must be a table"))
        permissions = {}
    permission_required = str(permissions.get("required") or "").strip()
    defaults = permissions.get("defaults") or {}
    if not isinstance(defaults, dict):
        problems.append(ManifestProblem("bad_type", "permissions.defaults", "must be a table of bool"))
        defaults = {}
    permission_defaults = {str(k): _as_bool(v, default=False) for k, v in defaults.items()}
    if permission_required and not _IDENT_RE.match(permission_required):
        problems.append(
            ManifestProblem("bad_permission", "permissions.required", f"'{permission_required}' is not an identifier")
        )
    allowed_roles = [str(r) for r in (permissions.get("allowed_roles") or [])]
    for role in allowed_roles:
        if role not in {"admin", "root", "manager", "user", "accountant", "readonly"}:
            problems.append(
                ManifestProblem(
                    "unknown_role",
                    "permissions.allowed_roles",
                    f"role '{role}' is not one of the application roles",
                    "use admin/root/manager/user/accountant/readonly",
                    severity="warning",
                )
            )

    navigation_raw = raw.get("navigation") or {}
    if not isinstance(navigation_raw, dict):
        problems.append(ManifestProblem("bad_type", "navigation", "must be a table"))
        navigation_raw = {}
    navigation = _nav_items(navigation_raw.get("items"), where="navigation.items", problems=problems)
    nav_parents = {n.parent for n in navigation if n.parent}
    allowed_parents = {""} | {n.id for n in navigation} | {"core", "transactions", "finance", "masters", "reports", "system", "ops", "inventory"}
    for parent in nav_parents:
        if parent not in allowed_parents:
            problems.append(
                ManifestProblem(
                    "unknown_nav_parent",
                    "navigation.items.parent",
                    f"parent '{parent}' is neither a declared nav id nor a known core group",
                    "declare the parent group in this module, or use a known group name",
                )
            )

    database = raw.get("database") or {}
    if not isinstance(database, dict):
        problems.append(ManifestProblem("bad_type", "database", "must be a table"))
        database = {}
    try:
        tables = _as_tuple(database.get("tables"))
    except TypeError as exc:
        problems.append(ManifestProblem("bad_type", "database.tables", str(exc)))
        tables = ()
    for table in tables:
        if not _TABLE_RE.match(table):
            problems.append(ManifestProblem("bad_table_name", "database.tables", f"'{table}' is not a valid table name"))
        if table in tables_present:
            problems.append(
                ManifestProblem(
                    "foreign_table",
                    "database.tables",
                    f"table '{table}' is already declared by the application core; a module may not claim it",
                    "list only tables this module owns, or contribute a migration against the core table instead",
                )
            )
    models_import = str(database.get("models_import") or "").strip()
    if models_import and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$", models_import):
        problems.append(
            ManifestProblem("bad_import", "database.models_import", f"'{models_import}' is not an importable dotted path")
        )

    migrations = _migration_refs(
        (database.get("migrations") or []),
        module_root=module_root,
        where="database.migrations",
        problems=problems,
        require_existing=True,
    )
    data_migrations = _migration_refs(
        (database.get("data_migrations") or []),
        module_root=module_root,
        where="database.data_migrations",
        problems=problems,
        require_existing=True,
    )
    for ref in migrations:
        if ref.destructive:
            problems.append(
                ManifestProblem(
                    "destructive_migration",
                    f"database.migrations[{ref.version}].destructive",
                    "this migration declares itself destructive",
                    "allowed, but production policy requires an explicit backup and a verified rollback note",
                    severity="warning",
                )
            )
    versions = [m.version for m in migrations] + [m.version for m in data_migrations]
    if len(versions) != len(set(versions)):
        problems.append(
            ManifestProblem("duplicate_migration_version", "database.migrations", "migration versions must be unique inside a module")
        )

    health = raw.get("health") or {}
    if not isinstance(health, dict):
        problems.append(ManifestProblem("bad_type", "health", "must be a table"))
        health = {}
    try:
        health_checks = _as_tuple(health.get("checks"))
    except TypeError as exc:
        problems.append(ManifestProblem("bad_type", "health.checks", str(exc)))
        health_checks = ()
    for spec_path in health_checks:
        if ":" not in spec_path or not re.match(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$", spec_path):
            problems.append(
                ManifestProblem(
                    "bad_health_check",
                    "health.checks",
                    f"'{spec_path}' must look like 'package.module:function'",
                    "the callable takes (app) and returns a dict",
                )
            )

    tests = raw.get("tests") or {}
    if not isinstance(tests, dict):
        problems.append(ManifestProblem("bad_type", "tests", "must be a table"))
        tests = {}
    try:
        test_paths = _as_tuple(tests.get("paths"))
    except TypeError as exc:
        problems.append(ManifestProblem("bad_type", "tests.paths", str(exc)))
        test_paths = ()
    for rel in test_paths:
        candidate = (module_root / rel).resolve()
        try:
            candidate.relative_to(module_root.resolve())
        except ValueError:
            problems.append(ManifestProblem("path_escape", "tests.paths", f"'{rel}' escapes the module directory"))

    features_raw = raw.get("features") or {}
    if features_raw and not isinstance(features_raw, dict):
        problems.append(ManifestProblem("bad_type", "features", "must be a table of flag = bool"))
        features_raw = {}
    features = {str(k): _as_bool(v, default=False) for k, v in features_raw.items()}

    # Cross-module conflicts (prefix + endpoint ownership).
    if url_prefix and existing_prefixes.get(url_prefix) not in (None, module_id):
        problems.append(
            ManifestProblem(
                "route_prefix_conflict",
                "routes.url_prefix",
                f"'{url_prefix}' is already mounted by module '{existing_prefixes[url_prefix]}'",
                "pick a different url_prefix for this module",
            )
        )
    for endpoint in expected_endpoints:
        owner = existing_endpoint_owners.get(endpoint)
        if owner and owner != module_id:
            problems.append(
                ManifestProblem(
                    "endpoint_conflict",
                    "routes.expected_endpoints",
                    f"endpoint '{endpoint}' is already owned by '{owner}'",
                    "rename the view function so two modules never write the same endpoint",
                )
            )
    if not enabled:
        for problem in problems:
            problem.severity = "warning" if problem.code in {"missing_field", "bad_format"} else problem.severity

    return ModuleSpec(
        module_id=module_id or Path(package).name,
        name=name or module_id,
        version=version or "0.0.0",
        description=description,
        enabled=enabled,
        root=str(module_root),
        package=package,
        manifest_path=_relative_or_abs(manifest_path, module_root.parent.parent),
        blueprint_variable=blueprint_variable,
        url_prefix=url_prefix,
        order=order,
        depends_on=depends_on,
        expected_endpoints=expected_endpoints,
        navigation=navigation,
        permission_required=permission_required,
        permission_defaults=permission_defaults,
        tables=tables,
        models_import=models_import,
        migrations=migrations,
        data_migrations=data_migrations,
        health_checks=health_checks,
        test_paths=test_paths,
        features=features,
        schema_api=schema_api,
        min_ams_version=min_ams,
        problems=problems,
        status=STATUS_DISABLED if not enabled else (STATUS_VALID if not problems else STATUS_DISCOVERED),
    )


def discover_manifest_paths(module_root: str | os.PathLike, *, ignored: Iterable[str] = ()) -> list[Path]:
    """Return candidate ``module.toml`` paths under *module_root*, sorted.

    Only real directories inside *module_root* are considered: a symlink that
    points out of the tree is rejected, so a stray folder cannot smuggle a
    module in.
    """
    root = Path(module_root)
    if not root.is_dir():
        return []
    skip = set(ignored)
    found: list[Path] = []
    resolved_root = root.resolve()
    for child in sorted(root.iterdir()):
        if child.name.startswith("_") or child.name in skip or child.name == "__pycache__":
            continue
        if not child.is_dir():
            continue
        if child.is_symlink() and resolved_root not in child.resolve().parents:
            continue
        manifest = child / MANIFEST_NAME
        if manifest.is_file():
            found.append(manifest)
    return found
