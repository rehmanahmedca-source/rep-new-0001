"""Controlled module discovery for AMS (see ``docs/MODULE_CONTRACT.md``).

Public surface::

    from app.services.module_system import registry_for, ModuleRegistry
    from app.services.module_system.health import run_module_health
    from app.services.module_system.navigation import validate_navigation

This package deliberately owns *no* route, no table and no template: it only
reads manifests, validates them, mounts what is valid and reports the result.
"""
from __future__ import annotations

from app.services.module_system.contract import (  # noqa: F401
    MANIFEST_NAME,
    SCHEMA_API,
    ManifestError,
    ManifestProblem,
    MigrationRef,
    ModuleSpec,
    NavItem,
    discover_manifest_paths,
    read_manifest,
    version_satisfies,
)
from app.services.module_system.registry import (  # noqa: F401
    CORE_BLUEPRINTS,
    REGISTRY_KEY,
    ModuleRegistry,
    RegistrationResult,
    attach,
    get_registry,
)


def registry_for(app, module_root, *, app_version: str = "1.0.0", discover: bool = True) -> ModuleRegistry:
    """Return the app's registry, creating (and optionally scanning) it once."""
    registry = app.extensions.get(REGISTRY_KEY) if getattr(app, "extensions", None) else None
    if registry is None:
        registry = attach(app, module_root, app_version=app_version)
    if discover and not registry.discovered:
        registry.discover()
    return registry


__all__ = [
    "CORE_BLUEPRINTS",
    "MANIFEST_NAME",
    "ModuleRegistry",
    "ModuleSpec",
    "ManifestError",
    "ManifestProblem",
    "MigrationRef",
    "NavItem",
    "REGISTRY_KEY",
    "RegistrationResult",
    "SCHEMA_API",
    "attach",
    "discover_manifest_paths",
    "get_registry",
    "read_manifest",
    "registry_for",
    "version_satisfies",
]
