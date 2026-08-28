"""Navigation built from *validated* module metadata, plus drift detection.

The existing sidebar in ``templates/layout.html`` stays authoritative for the
core ERP (that is deliberate — it is tuned, grouped and permission-gated by
hand).  What this module adds is:

1. **Module-declared navigation**, appended in the right group and order,
   permission-filtered through the application's own ``user_can`` rules.
2. **Navigation integrity checks** for the *whole* sidebar: every endpoint the
   layout references (hard-coded or module-declared) must still resolve after
   registration, ids and paths must be unique, and no sensitive item may be
   visible without a declared permission.

That second part is what catches the failure mode the ERP has been hit by
before: a route is renamed, ``url_for()`` in the shared layout explodes, and
every page after login turns into an HTTP 500.
"""
from __future__ import annotations

import re
from pathlib import Path

LAYOUT_TEMPLATE = "layout.html"
_URL_FOR_RE = re.compile(r"url_for\(\s*['\"]([A-Za-z0-9_.-]+)['\"]")
_GROUP_ORDER = ("core", "inventory", "transactions", "finance", "ops", "masters", "reports", "system")
SENSITIVE_HINTS = ("settings", "wipe", "admin", "migration", "import_export", "backup", "restore")
#: blueprints whose routes are administrative: navigation pointing at them must
#: declare a permission, and the pipeline blocks the update if it does not.
SENSITIVE_BLUEPRINTS = ("admin", "system", "deploy", "maintenance", "settings")


def validate_navigation(app, registry) -> list[dict]:
    """Return a list of navigation problems for module *and* core navigation."""
    problems: list[dict] = []
    seen_ids: dict[str, str] = {}
    seen_paths: dict[str, str] = {}
    view_functions = getattr(app, "view_functions", {})

    items = registry.navigation(app) if registry is not None else []
    nav_parents = {str(item.get("id")) for item in items} | {""} | set(_GROUP_ORDER)

    for item in items:
        nav_id = str(item.get("id") or "")
        module = str(item.get("module") or "")
        if nav_id in seen_ids:
            problems.append(
                {
                    "code": "duplicate_nav_id",
                    "module": module,
                    "detail": f"navigation id '{nav_id}' is declared by '{seen_ids[nav_id]}' and '{module}'",
                }
            )
        seen_ids[nav_id] = module
        endpoint = str(item.get("endpoint") or "")
        if endpoint and endpoint not in view_functions:
            problems.append(
                {
                    "code": "missing_route",
                    "module": module,
                    "detail": f"navigation item '{nav_id}' points at endpoint '{endpoint}', which is not registered",
                    "hint": "register the route, or remove the navigation entry",
                }
            )
        parent = str(item.get("parent") or "")
        if parent and parent not in nav_parents:
            problems.append(
                {
                    "code": "unknown_parent",
                    "module": module,
                    "detail": f"navigation item '{nav_id}' has unknown parent '{parent}'",
                }
            )
        prefix = str(item.get("active_prefix") or "")
        if prefix in seen_paths and seen_paths[prefix] != module:
            problems.append(
                {
                    "code": "duplicate_nav_path",
                    "module": module,
                    "detail": f"path/prefix '{prefix}' is used by '{seen_paths[prefix]}' and '{module}'",
                    "hint": "two sidebar entries pointing at one page is a UX bug; keep one",
                }
            )
        if prefix:
            seen_paths.setdefault(prefix, module)
        if not item.get("permission") and any(hint in nav_id.lower() or hint in endpoint.lower() for hint in SENSITIVE_HINTS):
            # An item whose *endpoint* belongs to an administrative blueprint is
            # a hard error: exposing it to every signed-in role is not a style
            # problem, it is an authorisation problem.  A merely suspicious name
            # stays a warning so a module called ``plant_admin_tools`` cannot be
            # blocked by a substring match.
            owner = endpoint.split(".", 1)[0] if "." in endpoint else endpoint
            severity = "error" if owner in SENSITIVE_BLUEPRINTS else "warning"
            problems.append(
                {
                    "code": "unprotected_sensitive_nav",
                    "module": module,
                    "severity": severity,
                    "detail": f"'{nav_id}' looks administrative but declares no permission; it would be visible to every signed-in role",
                    "hint": "set navigation.items[].permission to a can_* permission",
                }
            )

    for problem in core_layout_problems(app, view_functions):
        problems.append(problem)
    return problems


def core_layout_problems(app, view_functions=None) -> list[dict]:
    """Every ``url_for('...')`` in the shared layout must resolve."""
    view_functions = view_functions if view_functions is not None else getattr(app, "view_functions", {})
    problems: list[dict] = []
    folder = Path(app.template_folder)
    template = folder / LAYOUT_TEMPLATE if folder.is_absolute() else Path(app.root_path) / folder / LAYOUT_TEMPLATE
    if not template.is_file():
        return problems
    try:
        source = template.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return problems
    for endpoint in sorted(set(_URL_FOR_RE.findall(source))):
        if endpoint in {"static"}:
            continue
        if endpoint in view_functions:
            continue
        # ``url_for('x')`` in the layout is rendered for every page, so a stale
        # endpoint name is a 500 everywhere, not just on one screen.
        problems.append(
            {
                "code": "missing_route_in_layout",
                "module": "core",
                "severity": "error",
                "detail": f"templates/{LAYOUT_TEMPLATE} links to endpoint '{endpoint}' which is not registered",
                "hint": "restore the route or update the sidebar link",
            }
        )
    return problems


def grouped_navigation(app, registry, *, user_permissions=None) -> dict:
    """Module navigation grouped by parent, ready for the sidebar template."""
    items = registry.navigation(app, user_permissions=user_permissions) if registry else []
    visible = [item for item in items if item.get("resolvable") and item.get("granted", True)]
    groups: dict[str, list[dict]] = {name: [] for name in _GROUP_ORDER}
    for item in visible:
        groups.setdefault(item.get("parent") or "core", []).append(item)
    for entries in groups.values():
        entries.sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))
    return {"groups": groups, "flat": visible, "hidden": [i for i in items if i not in visible]}


def resolve_path(app, endpoint: str, **values) -> str:
    """Best-effort URL for a nav endpoint, never raising into a page render."""
    from flask import url_for

    try:
        return url_for(endpoint, **values)
    except Exception:
        return "#"
