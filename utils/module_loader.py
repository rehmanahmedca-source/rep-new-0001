"""Discover, validate and register AMS modules from a directory.

This is the single entry point the application factory calls, and it is still
callable exactly as before (``load_modules(app, blueprint_dir=...)`` and
``get_modules_info(blueprint_dir)``).  What changed is what happens inside:
instead of importing every file it finds and hoping, it now drives the
validated module contract in :mod:`app.services.module_system`.

Rules kept from the previous loader
-----------------------------------
* a module whose metadata says ``enabled = false`` is never mounted;
* a blueprint already registered by the factory is never re-mounted;
* one broken module must never abort application startup — but it is recorded
  in the registry with the reason, and the factory reports it loudly.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_MODULE_ROOT_DEFAULT = "blueprints"


def _enabled_override(app, module_id: str) -> bool | None:
    """Allow an operator to switch a module off without editing its manifest.

    ``AMS_DISABLED_MODULES`` / ``AMS_ENABLED_MODULES`` are comma separated.  An
    explicit ``AMS_ENABLED_MODULES`` entry re-enables a module that was shipped
    disabled, which is what a controlled rollout needs; anything not listed in
    either stays as the manifest declared it.
    """
    disabled = {
        part.strip()
        for part in str(
            app.config.get("AMS_DISABLED_MODULES") or os.environ.get("AMS_DISABLED_MODULES", "")
        ).split(",")
        if part.strip()
    }
    enabled = {
        part.strip()
        for part in str(
            app.config.get("AMS_ENABLED_MODULES") or os.environ.get("AMS_ENABLED_MODULES", "")
        ).split(",")
        if part.strip()
    }
    if module_id in enabled:
        return True
    if module_id in disabled:
        return False
    return None


def _ensure_importable(blueprint_path: Path) -> str:
    parent = str(blueprint_path.resolve().parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return blueprint_path.name


def load_modules(app, blueprint_dir: str | os.PathLike = _MODULE_ROOT_DEFAULT):
    """Validate + mount every module under *blueprint_dir*; return the registry.

    ``app.services.module_system`` is imported lazily on purpose: the application
    factory imports this module, so a top-level import would be circular.
    """
    from app.services.module_system import ModuleRegistry, registry_for
    blueprint_path = Path(blueprint_dir)
    if getattr(app, "_modules_loaded", False):
        registry = app.extensions.get("ams_modules")
        return registry if registry is not None else _bare_registry(blueprint_path)
    if not blueprint_path.exists():
        app._modules_loaded = True
        app._module_summary = {"registered": [], "skipped": [], "failed": []}
        return _bare_registry(blueprint_path)

    package_root = _ensure_importable(blueprint_path)
    registry = registry_for(app, blueprint_path, app_version=str(app.config.get("APP_VERSION") or "1.0.0"), discover=False)

    specs = registry.discover(force=True)
    for spec in specs:
        override = _enabled_override(app, spec.module_id)
        if override is not None and override != spec.enabled:
            spec.enabled = override
            spec.status = "DISABLED" if not override else spec.status
            logger.info("module '%s' %s by operator override", spec.module_id, "disabled" if not override else "enabled")
    registry._resolve_dependencies()

    summary = registry.register(app)
    app._module_summary = summary
    app._modules_loaded = True

    for entry in summary["failed"]:
        # Loud, but non-fatal: the ERP must keep serving the modules it has.
        logger.error(
            "module '%s' was NOT registered: %s", entry.get("module"), entry.get("error") or "see module registry"
        )
    for orphan in registry.orphans:
        logger.error("module candidate rejected during discovery: %s (%s)", orphan.get("path"), orphan.get("reason"))
    disabled = [spec.module_id for spec in specs if spec.status == "DISABLED"]
    if disabled:
        logger.info("modules disabled by manifest/override: %s", ", ".join(sorted(disabled)))
    logger.info(
        "module discovery: %d discovered, %d registered, %d failed validation, %d disabled",
        len(specs),
        len(summary["registered"]),
        len(summary["failed"]),
        len(disabled),
    )
    return registry


def _bare_registry(blueprint_dir):
    from app.services.module_system import ModuleRegistry

    return ModuleRegistry(blueprint_dir)


def get_modules_info(blueprint_dir: str | os.PathLike = _MODULE_ROOT_DEFAULT):
    """Legacy helper: ``[(module_name, [blueprint_name, ...]), ...]``."""
    from app.services.module_system import ModuleRegistry
    path = Path(blueprint_dir)
    if not path.is_dir():
        return []
    parent = str(path.resolve().parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    registry = ModuleRegistry(path)
    specs = registry.discover()
    info = []
    for spec in sorted(specs, key=lambda s: s.module_id):
        if spec.status == "DISABLED" or not spec.enabled:
            continue
        names = _blueprint_names_without_import(path, spec.module_id)
        if names:
            info.append((spec.module_id, names))
    return info


def _blueprint_names_without_import(root: Path, module_id: str) -> list[str]:
    """Blueprint names for a module, derived from the package name.

    The legacy callers of :func:`get_modules_info` only display names, so the
    registry's own naming rule (blueprint name == module id, as declared in the
    manifest) is enough; reading a declared variable from the manifest is used
    when the module ships a ``module.toml``.
    """
    manifest = root / module_id / "module.toml"
    if manifest.is_file():
        try:
            from app.services.module_system import read_manifest

            raw = read_manifest(manifest)
            declared = str(((raw.get("routes") or {}).get("blueprint_variable") or "")).strip()
            if declared:
                return [module_id]
        except Exception:
            pass
    if (root / f"{module_id}.py").is_file() or (root / module_id).is_dir():
        return [module_id]
    return []


def _iter_module_names(blueprint_dir: str | os.PathLike):  # pragma: no cover - compat shim
    """Kept for any external tool that still imports this helper."""
    path = Path(blueprint_dir)
    names = set()
    if path.is_dir():
        for file in path.glob("*.py"):
            if not file.name.startswith("_"):
                names.add(file.stem)
        for child in path.iterdir():
            if child.is_dir() and not child.name.startswith("_") and (child / "__init__.py").exists():
                names.add(child.name)
    return sorted(names)
