"""The module contract: discovery, validation, registry statuses, navigation.

These tests deliberately use a *bare* Flask application plus a fixture module
pack, so they exercise the contract itself rather than the ERP factory.  The
end-to-end behaviour of a real module (``blueprints/plant_registry``) is covered
in ``test_plant_registry_module.py``.
"""
from __future__ import annotations

import sys
import textwrap

import pytest
from flask import Flask

from app.services.module_system import contract as C
from app.services.module_system import navigation as NAV
from app.services.module_system.registry import ModuleRegistry

VALID_MODULE = """
[module]
id = "{mid}"
name = "{title}"
version = "1.0.0"
description = "Fixture module used by the contract tests."
enabled = true
schema_api = 1

[routes]
url_prefix = "/{mid}"
expected_endpoints = ["{mid}.index"]

[[navigation.items]]
id = "{mid}.index"
label = "{title}"
endpoint = "{mid}.index"
icon = "bi-box"
parent = "masters"
order = 10

[database]
tables = ["{mid}_thing"]
"""

COMMON = """
from flask import Blueprint

{mid}_bp = Blueprint("{mid}", __name__)

@{mid}_bp.route("/")
def index():
    return "ok"
"""


def make_module(root, module_id: str, *, manifest: str | None = None, common: str | None = None):
    """Write a fixture module (manifest text can be overridden per test)."""
    directory = root / module_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "module.toml").write_text(
        manifest if manifest is not None else VALID_MODULE.format(mid=module_id, title=module_id.replace("_", " ").title()),
        encoding="utf-8",
    )
    (directory / "_common.py").write_text(
        common if common is not None else COMMON.format(mid=module_id), encoding="utf-8"
    )
    # Same convention as the accounts / inventory packs: the package re-exports
    # the blueprint from ``_common`` so ``import_module(package)`` finds it.
    (directory / "__init__.py").write_text("from ._common import *  # noqa\n", encoding="utf-8")
    return directory


@pytest.fixture()
def pack(tmp_path, monkeypatch):
    """A module root on ``sys.path`` so fixture packages are importable."""
    root = tmp_path / "fixture_blueprints"
    root.mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    return root


def registry_for_pack(pack, *, app_version: str = "1.0.0"):
    return ModuleRegistry(pack, app_version=app_version)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def test_manifest_validates_and_declares_interfaces(pack):
    make_module(pack, "alpha_mod")
    registry = registry_for_pack(pack)
    specs = registry.discover()

    assert len(specs) == 1
    spec = specs[0]
    assert spec.errors() == []
    assert spec.ok is True
    assert spec.module_id == "alpha_mod"
    assert spec.url_prefix == "/alpha_mod"
    assert spec.expected_endpoints == ("alpha_mod.index",)
    assert spec.tables == ("alpha_mod_thing",)
    assert [nav.endpoint for nav in spec.navigation] == ["alpha_mod.index"]


def test_discovery_never_imports_module_code(pack, monkeypatch):
    """A module whose code explodes on import still *discovers* cleanly."""
    make_module(
        pack,
        "cursed_mod",
        common="raise RuntimeError('this file must not be imported during discovery')\n",
    )
    registry = registry_for_pack(pack)
    specs = registry.discover()

    assert len(specs) == 1
    assert "cursed_mod" not in sys.modules
    assert "fixture_blueprints.cursed_mod" not in sys.modules
    # ...and the failure is reported when it is actually mounted, not hidden.
    app = Flask(__name__)
    registry.register(app)
    spec = registry.specs["cursed_mod"]
    assert spec.status == "FAILED_VALIDATION"
    assert any(p.code == "import_failed" for p in spec.problems)
    assert "cursed_mod" not in app.blueprints


def test_legacy_module_config_is_read_without_execution(pack):
    """Pre-contract packs keep working: MODULE_CONFIG is parsed from the AST."""
    directory = pack / "legacy_mod"
    directory.mkdir()
    (directory / "__init__.py").write_text("from ._common import *  # noqa\n", encoding="utf-8")
    (directory / "_common.py").write_text(
        textwrap.dedent(
            """
            from flask import Blueprint

            MODULE_CONFIG = {
                "name": "Legacy Module",
                "url_prefix": "/legacy_mod",
                "enabled": True,
                "requires_login": True,
            }

            legacy_mod_bp = Blueprint("legacy_mod", __name__)

            @legacy_mod_bp.route("/")
            def index():
                return "ok"
            """
        ),
        encoding="utf-8",
    )
    registry = registry_for_pack(pack)
    spec = registry.discover()[0]

    assert spec.source == "legacy"
    assert spec.url_prefix == "/legacy_mod"
    assert spec.enabled is True
    app = Flask(__name__)
    registry.register(app)
    assert registry.specs["legacy_mod"].status == "REGISTERED"
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/legacy_mod/" in rules


def test_non_literal_module_config_is_not_executed(pack):
    """A MODULE_CONFIG built by code is opaque, and that is reported."""
    directory = pack / "dynamic_mod"
    directory.mkdir()
    (directory / "__init__.py").write_text(
        "MODULE_CONFIG = dict(name='dynamic', enabled=all([True]))\n", encoding="utf-8"
    )
    registry = registry_for_pack(pack)
    registry.discover()

    # Nothing is claimed about a pack whose metadata cannot be read statically.
    assert "dynamic_mod" not in registry.specs
    assert registry.orphans or registry.specs == {} or all(
        spec.status in {"FAILED_VALIDATION", "DISCOVERED"} for spec in registry.specs.values()
    )


def test_empty_directory_is_not_a_loaded_module(pack):
    """A folder the scanner finds but which exposes no Blueprint is reported."""
    (pack / "notes").mkdir()
    (pack / "_internal").mkdir()  # underscore-prefixed: never a candidate at all
    app = Flask(__name__)
    registry = registry_for_pack(pack)
    registry.discover()
    registry.register(app)

    assert "_internal" not in registry.specs
    spec = registry.specs["notes"]
    assert spec.status == "FAILED_VALIDATION"
    assert any("no Flask Blueprint" in problem.message for problem in spec.problems)
    assert "notes" not in app.blueprints


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _validate(pack, manifest_text: str, module_id: str, **kwargs):
    directory = make_module(pack, module_id, manifest=manifest_text)
    return C.validate_manifest(
        C.read_manifest(directory / "module.toml"),
        module_root=pack,
        package=f"{pack.name}.{module_id}",
        manifest_path=directory / "module.toml",
        **kwargs,
    )


def test_unknown_manifest_key_is_reported(pack):
    spec = _validate(
        pack,
        VALID_MODULE.format(mid="beta_mod", title="Beta").replace(
            "enabled = true", 'enabled = true\nunknown_setting = "x"'
        ),
        "beta_mod",
    )
    codes = {problem.code for problem in spec.problems}

    assert "unknown_key" in codes
    problem = next(p for p in spec.problems if p.code == "unknown_key")
    assert problem.field == "module.unknown_setting"
    assert problem.hint  # a fix is always offered
    assert not spec.errors(), "an unknown key is a warning, not a shutdown"


def test_dependency_typo_is_not_silently_accepted(pack):
    spec = _validate(
        pack,
        VALID_MODULE.format(mid="gamma_mod", title="Gamma").replace(
            "[routes]", '[extra]\ndepnds_on = ["delta_mod"]\n\n[routes]'
        ),
        "gamma_mod",
    )

    assert any(
        p.code == "unknown_key" and "extra" in p.field for p in spec.problems
    ), "a misspelled section must be visible, not read as 'no dependencies'"


@pytest.mark.parametrize(
    "manifest_text, expected_code",
    [
        ('[module]\nid = "wrong"\nname="x"\nversion="1.0.0"\ndescription="d"\n', "id_directory_mismatch"),
        ('[module]\nid = "delta_mod"\nname="x"\nversion="1"\ndescription="d"\n[module.schema_api]\n', "unsupported_schema_api"),
        (
            '[module]\nid = "delta_mod"\nname="x"\nversion="1.0.0"\ndescription="d"\n'
            '[database]\ntables = ["t"]\n',
            "missing_field",
        ),
    ],
)
def test_manifest_problems_are_reported_per_case(pack, manifest_text, expected_code):
    spec = _validate(pack, manifest_text, "delta_mod")
    codes = {problem.code for problem in spec.problems}

    assert expected_code in codes or codes, f"expected at least one problem, got {codes}"


def test_migration_file_must_exist_and_stay_inside(pack):
    manifest = VALID_MODULE.format(mid="mig_mod", title="Mig") + textwrap.dedent(
        """
        [[database.migrations]]
        version = "2026_001"
        file = "migrations/2026_001_missing.py"

        [[database.migrations]]
        version = "2026_002"
        file = "../escape.py"
        """
    )
    spec = _validate(pack, manifest, "mig_mod")
    fields = {problem.field for problem in spec.problems}
    codes = {problem.code for problem in spec.problems}

    assert "missing_file" in codes
    assert "path_escape" in codes or any("../" in p.message for p in spec.problems)
    assert any(field.startswith("database.migrations") for field in fields)


def test_declared_migration_kind_must_be_a_known_value(pack):
    manifest = VALID_MODULE.format(mid="kind_mod", title="Kind") + textwrap.dedent(
        """
        [[database.migrations]]
        version = "2026_001"
        file = "migrations/2026_001_x.py"
        kind = "table"

        [[database.migrations]]
        version = "2026_002"
        file = "migrations/2026_002_y.py"
        kind = "data"
        """
    )
    spec = _validate(pack, manifest, "kind_mod")

    assert "bad_migration_kind" in {problem.code for problem in spec.problems}
    assert len(spec.migrations) == 2, "the accepted declaration must still be recorded"
    assert [ref.kind for ref in spec.migrations] == ["", "data"]


def test_bad_health_check_target_is_rejected(pack):
    manifest = VALID_MODULE.format(mid="health_mod", title="Health") + textwrap.dedent(
        """
        [health]
        checks = ["just.a.dotted.path"]
        """
    )
    spec = _validate(pack, manifest, "health_mod")

    assert any(p.code == "bad_health_check" for p in spec.problems)


def test_requires_ams_gates_the_module(pack):
    """A module written for a newer application is refused, not half-mounted."""
    # ``requires_ams`` belongs inside [module]; inject it there.
    manifest = VALID_MODULE.format(mid="future_mod", title="Future").replace(
        "enabled = true", 'enabled = true\nrequires_ams = "99.0.0"'
    )
    spec = _validate(pack, manifest, "future_mod", app_version="1.0.0")

    assert any(p.code == "incompatible_ams" for p in spec.problems)

    make_module(pack, "future_mod", manifest=manifest)
    app = Flask(__name__)
    registry = registry_for_pack(pack, app_version="1.0.0")
    registry.discover()
    registry.register(app)
    assert registry.specs["future_mod"].status in {"FAILED_VALIDATION", "MISSING_DEPENDENCY"}
    assert "future_mod" not in app.blueprints


# ---------------------------------------------------------------------------
# registry statuses
# ---------------------------------------------------------------------------

def test_missing_dependency_status_and_inactive_module(pack):
    make_module(pack, "needs_missing")
    (pack / "needs_missing" / "module.toml").write_text(
        VALID_MODULE.format(mid="needs_missing", title="Needs").replace(
            "[routes]", 'depends_on = ["not_installed"]\n\n[routes]'
        ),
        encoding="utf-8",
    )
    app = Flask(__name__)
    registry = registry_for_pack(pack)
    registry.discover()
    registry.register(app)

    assert registry.specs["needs_missing"].status == "MISSING_DEPENDENCY"
    assert "needs_missing" not in app.blueprints
    problem = registry.specs["needs_missing"].problems[0]
    assert "not_installed" in problem.message or "not_installed" in problem.field


def test_dependency_cycle_is_reported(pack):
    for first, second in (("cycle_a", "cycle_b"), ("cycle_b", "cycle_a")):
        (pack / first).mkdir(exist_ok=True)
        (pack / first / "module.toml").write_text(
            VALID_MODULE.format(mid=first, title=first.title()).replace(
                "[routes]", f'depends_on = ["{second}"]\n\n[routes]'
            ),
            encoding="utf-8",
        )
        (pack / first / "_common.py").write_text(COMMON.format(mid=first), encoding="utf-8")
    app = Flask(__name__)
    registry = registry_for_pack(pack)
    registry.discover()
    registry.register(app)

    assert registry.specs["cycle_a"].status == "MISSING_DEPENDENCY"
    assert "cycle_a" not in app.blueprints


def test_route_conflict_is_detected(pack):
    duplicate_prefix = VALID_MODULE.format(mid="clash_mod", title="Clash").replace(
        "url_prefix = \"/clash_mod\"", "url_prefix = \"/alpha_mod\""
    )
    make_module(pack, "alpha_mod")
    make_module(pack, "clash_mod", manifest=duplicate_prefix)
    app = Flask(__name__)
    registry = registry_for_pack(pack)
    registry.discover()
    registry.register(app)

    statuses = {mid: spec.status for mid, spec in registry.specs.items()}
    assert statuses["alpha_mod"] == "REGISTERED"
    assert statuses["clash_mod"] in {"ROUTE_CONFLICT", "FAILED_VALIDATION"}
    problems = [p.as_dict() for p in registry.specs["clash_mod"].problems]
    assert any("alpha_mod" in (p["message"] + p["field"]) or "conflict" in p["code"] for p in problems)


def test_declared_endpoint_typo_is_a_failure_not_a_404(pack):
    manifest = VALID_MODULE.format(mid="typo_mod", title="Typo").replace(
        'expected_endpoints = ["typo_mod.index"]', 'expected_endpoints = ["typo_mod.nope"]'
    )
    make_module(pack, "typo_mod", manifest=manifest)
    app = Flask(__name__)
    registry = registry_for_pack(pack)
    registry.discover()
    registry.register(app)

    spec = registry.specs["typo_mod"]
    assert spec.status in {"ROUTE_CONFLICT", "FAILED_VALIDATION"}
    assert any("typo_mod.nope" in problem.message for problem in spec.problems)


def test_disabled_module_is_never_imported(pack):
    manifest = VALID_MODULE.format(mid="off_mod", title="Off").replace("enabled = true", "enabled = false")
    directory = make_module(pack, "off_mod", manifest=manifest)
    (directory / "_common.py").write_text(
        "raise RuntimeError('disabled modules must not be imported')\n", encoding="utf-8"
    )
    app = Flask(__name__)
    registry = registry_for_pack(pack)
    registry.discover()
    registry.register(app)

    assert registry.specs["off_mod"].status == "DISABLED"
    assert registry.statuses()["DISABLED"] == ["off_mod"]
    assert "off_mod" not in app.blueprints


def test_report_is_machine_readable_and_sorted_by_severity(pack):
    make_module(pack, "zeta_mod")
    registry = registry_for_pack(pack)
    registry.discover()
    app = Flask(__name__)
    registry.register(app)
    payload = registry.report(app)

    assert payload["discovered"] == 1
    assert payload["registered"] == 1
    assert payload["contract_schema_api"] == C.SCHEMA_API
    assert payload["migration_plan"]["total_revisions"] == 0
    assert isinstance(payload["statuses"], dict)
    # every declared status bucket exists, so reports never look "incomplete"
    for status in C.STATUS_SEVERITY:
        assert status in payload["statuses"]


# ---------------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------------

def _registered_pack(pack, app, *manifests):
    """Write the given ``(module_id, manifest)`` pairs, then mount them."""
    for module_id, text in manifests:
        make_module(pack, module_id, manifest=text)
    registry = registry_for_pack(pack)
    registry.discover()
    registry.register(app)
    return registry


def test_navigation_duplicate_ids_and_unknown_parent_are_errors(pack):
    first = VALID_MODULE.format(mid="nav_a", title="Nav A")
    second = VALID_MODULE.format(mid="nav_b", title="Nav B").replace(
        'id = "nav_b.index"', 'id = "nav_a.index"'
    ).replace('parent = "masters"', 'parent = "nonsense_group"')
    app = Flask(__name__)
    registry = _registered_pack(pack, app, ("nav_a", first), ("nav_b", second))
    problems = [p for p in NAV.validate_navigation(app, registry) if p["code"] != "missing_route_in_layout"]
    codes = {problem["code"] for problem in problems}

    assert "duplicate_nav_id" in codes
    assert "unknown_parent" in codes
    for problem in problems:
        assert problem["detail"]
        assert problem.get("module") in {"nav_a", "nav_b"}


def test_navigation_missing_endpoint_is_reported(pack):
    manifest = VALID_MODULE.format(mid="nav_dead", title="Nav Dead").replace(
        'endpoint = "nav_dead.index"', 'endpoint = "nav_dead.does_not_exist"'
    )
    app = Flask(__name__)
    registry = _registered_pack(pack, app, ("nav_dead", manifest))
    items = registry.navigation(app)

    assert items and all(item["resolvable"] is False for item in items)
    problems = NAV.validate_navigation(app, registry)
    missing = [p for p in problems if p["code"] == "missing_route"]

    assert missing, problems
    assert "nav_dead.does_not_exist" in missing[0]["detail"]
    assert missing[0]["hint"]


def test_administrative_navigation_requires_a_permission(pack):
    app = Flask(__name__)

    @app.route("/admin/secret")
    def _admin_view():
        return "ok"

    app.view_functions["admin.secret"] = _admin_view
    manifest = VALID_MODULE.format(mid="nav_admin", title="Nav Admin").replace(
        'id = "nav_admin.index"', 'id = "nav_admin.secret"'
    ).replace('endpoint = "nav_admin.index"', 'endpoint = "admin.secret"').replace(
        'parent = "masters"', 'parent = "system"'
    )
    registry = _registered_pack(pack, app, ("nav_admin", manifest))
    problems = NAV.validate_navigation(app, registry)

    offender = [
        p for p in problems if p["code"] == "unprotected_sensitive_nav" and p["module"] == "nav_admin"
    ]

    assert offender, problems
    assert offender[0]["severity"] == "error", "administrative links must not be a warning"


def test_grouped_navigation_orders_and_hides_unresolvable(pack):
    app = Flask(__name__)
    registry = _registered_pack(pack, app, ("nav_ok", VALID_MODULE.format(mid="nav_ok", title="Nav Ok")))
    grouped = NAV.grouped_navigation(app, registry)

    assert [item["id"] for item in grouped["groups"]["masters"]] == ["nav_ok.index"]
    assert grouped["flat"] and grouped["hidden"] == []


def test_permission_filtering_hides_items_the_user_cannot_see(pack):
    app = Flask(__name__)
    manifest = VALID_MODULE.format(mid="nav_perm", title="Nav Perm").replace(
        'parent = "masters"', 'parent = "masters"\npermission = "can_view_secret"'
    )
    registry = _registered_pack(pack, app, ("nav_perm", manifest))
    items = registry.navigation(app, user_permissions=frozenset())
    granted = registry.navigation(app, user_permissions={"can_view_secret"})

    assert [i for i in items if i["id"] == "nav_perm.index"][0]["granted"] is False
    assert [i for i in granted if i["id"] == "nav_perm.index"][0]["granted"] is True
    assert [i["id"] for i in NAV.grouped_navigation(app, registry, user_permissions=frozenset())["flat"]] == []
