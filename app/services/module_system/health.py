"""Per-module health checks, run at startup and by ``tools/dbupdate.py``.

Every module gets the same baseline set of checks (its blueprint is mounted,
its declared routes exist, its declared tables are in the database, its
declared test files exist, its navigation resolves) and then any module-owned
callable it declared in ``[health] checks = ["package:function"]``.

A check never guesses at business rules: financial/inventory/ledger truth is
owned by ``app.services.dbupdate.integrity`` which reuses the existing
consistency tools, so nothing here can contradict the accounting model.
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path

LOG = logging.getLogger("ams.modules.health")


def _ok(name: str, detail: str = "", **extra) -> dict:
    return {"name": name, "status": "PASS", "detail": detail, **extra}


def _fail(name: str, detail: str, **extra) -> dict:
    return {"name": name, "status": "FAIL", "detail": detail, **extra}


def _warn(name: str, detail: str, **extra) -> dict:
    return {"name": name, "status": "WARN", "detail": detail, **extra}


def _info(name: str, detail: str, **extra) -> dict:
    """A note, not a warning: pre-manifest modules predate this contract."""
    return {"name": name, "status": "INFO", "detail": detail, **extra}


def generic_checks(app, spec) -> list[dict]:
    checks: list[dict] = []
    registry = app.extensions.get("ams_modules")
    mounted = registry.registrations.get(spec.module_id) if registry else None

    if not spec.enabled:
        checks.append(_ok("module:disabled", "module is switched off; no health requirement applies"))
        return checks

    try:
        module = importlib.import_module(spec.package)
        checks.append(_ok("module:importable", spec.package))
    except Exception as exc:
        checks.append(_fail("module:importable", f"{type(exc).__name__}: {exc}"))
        return checks

    if mounted is None or not mounted.blueprints:
        checks.append(_fail("module:blueprint_mounted", f"no blueprint registered for '{spec.module_id}'"))
    else:
        checks.append(_ok("module:blueprint_mounted", ", ".join(mounted.blueprints)))

    missing = [endpoint for endpoint in spec.expected_endpoints if endpoint not in app.view_functions]
    if missing:
        checks.append(_fail("module:routes", "declared endpoints not registered: " + ", ".join(missing)))
    else:
        checks.append(_ok("module:routes", f"{len(spec.expected_endpoints)} declared endpoint(s)"))

    if spec.tables:
        try:
            from models import db

            with app.app_context():
                from sqlalchemy import inspect as sa_inspect

                existing = set(sa_inspect(db.engine).get_table_names())
            absent = sorted(set(spec.tables) - existing)
            if absent:
                checks.append(
                    _fail(
                        "module:tables",
                        "declared tables missing from the database: " + ", ".join(absent),
                        pending_schema_change=True,
                    )
                )
            else:
                checks.append(_ok("module:tables", ", ".join(sorted(spec.tables))))
        except Exception as exc:
            checks.append(_fail("module:tables", f"could not inspect the database: {exc}"))

    for nav in spec.navigation:
        if nav.endpoint and nav.endpoint not in app.view_functions:
            checks.append(_fail("module:navigation", f"'{nav.id}' -> unresolvable endpoint '{nav.endpoint}'"))
    else:
        if spec.navigation:
            checks.append(_ok("module:navigation", f"{len(spec.navigation)} item(s)"))

    repo_root = Path(app.root_path).parent
    missing_tests = [rel for rel in spec.test_paths if not (repo_root / rel).is_file()]
    if spec.test_paths:
        if missing_tests:
            checks.append(_warn("module:tests", "declared test files not found: " + ", ".join(missing_tests)))
        else:
            checks.append(_ok("module:tests", f"{len(spec.test_paths)} file(s)"))
    elif spec.source == "toml":
        checks.append(
            _warn(
                "module:tests",
                "a manifest module must ship tests",
                advice="declare tests.paths and add the file",
            )
        )
    else:
        checks.append(
            _info(
                "module:tests",
                "legacy module without declared tests (it predates the module contract)",
            )
        )

    from app.services.constants import USER_PERMISSION_DEFAULTS

    new_permissions = [
        permission
        for permission in {spec.permission_required, *spec.permission_defaults}
        if permission and permission not in USER_PERMISSION_DEFAULTS
    ]
    if new_permissions:
        checks.append(
            _warn(
                "module:permissions",
                "permission(s) not in USER_PERMISSION_DEFAULTS: " + ", ".join(sorted(new_permissions)),
                advice="existing users cannot be granted these yet; add defaults in app/services/constants.py",
            )
        )
    else:
        checks.append(_ok("module:permissions", "declared permissions are known to the application"))
    return checks


def run_module_health(app, registry, *, module_ids=None) -> dict:
    """Execute baseline + module-owned checks for every enabled module."""
    results: dict[str, dict] = {}
    if registry is None:
        return {"modules": {}, "status": "PASS", "checked": 0, "failed": 0}
    specs = [spec for spec in registry.specs.values() if module_ids is None or spec.module_id in module_ids]
    for spec in specs:
        if spec.status == "DISABLED":
            results[spec.module_id] = {"status": "SKIPPED", "checks": [_ok("module:disabled", "not enabled")]}
            continue
        checks = generic_checks(app, spec)
        for target in spec.health_checks:
            checks.append(_run_callable(app, target, spec))
        failed = [c for c in checks if c["status"] == "FAIL"]
        results[spec.module_id] = {
            "status": "FAIL" if failed else ("WARN" if any(c["status"] == "WARN" for c in checks) else "PASS"),
            "checks": checks,
            "failed": [c["name"] for c in failed],
        }
    total_failed = sum(1 for r in results.values() if r["status"] == "FAIL")
    return {
        "status": "FAIL" if total_failed else "PASS",
        "checked": len(results),
        "failed": total_failed,
        "modules": results,
    }


def _run_callable(app, target: str, spec) -> dict:
    name = f"module:{target}"
    try:
        module_path, _, func_name = target.partition(":")
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)
    except Exception as exc:
        return _fail(name, f"health check '{target}' could not be loaded: {type(exc).__name__}: {exc}")
    try:
        payload = func(app)
    except Exception as exc:
        LOG.exception("health check %s raised", target)
        return _fail(name, f"raised {type(exc).__name__}: {exc}")
    if not isinstance(payload, dict):
        return _fail(name, "health check must return a dict, got " + type(payload).__name__)
    status = str(payload.get("status") or "PASS").upper()
    return {
        "name": name,
        "status": status if status in {"PASS", "FAIL", "WARN"} else "WARN",
        "detail": str(payload.get("detail") or payload.get("message") or ""),
        "module": spec.module_id,
        "data": {k: v for k, v in payload.items() if k not in {"status", "detail", "message"}},
    }
