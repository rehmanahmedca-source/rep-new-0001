# AMS Module Contract (schema API v1)

Status: **authoritative**. `docs/MODULE_REGISTRY.md` is generated from this
contract, so read this first and the generated file for the current inventory.

The contract exists so that a new module can be added by **creating a folder**
instead of editing five registration files, while keeping the rule that the
application never executes code it has not validated:

> A folder inside `blueprints/` is only a *candidate*. It becomes a module when
> its manifest parses, passes validation, and its declared interfaces resolve.
> Anything that fails is recorded with a reason and stays inactive — it is never
> partially mounted, and it never aborts the ERP.

---

## 1. What the discovery scan does (and does not) do

`app/services/module_system/contract.py::discover_manifest_paths()` walks
`blueprints/*/` (root configurable with `AMS_MODULE_ROOT`) and looks for, in order:

1. `module.toml` — the manifest (preferred);
2. `_common.py` → `__init__.py` → `module.py` — read **as text and parsed with
   `ast`** to lift a literal `MODULE_CONFIG = {...}` dict.

Discovery **never imports anything**. The implicit `MODULE_CONFIG` path exists so
the pre-existing `accounts`, `inventory` and `import_export` packs keep behaving
exactly as they do today without being rewritten; the literal is read from the
AST, so a `MODULE_CONFIG` built by executing code is rejected rather than run.
Blueprint objects and models are imported later, only after validation passes and
only inside a guarded import (Phase: registry `_mount`).

Skipped or rejected before validation (recorded in `registry.orphans`, never
fatal to the application):

* directories starting with `_` or `.`, and `__pycache__` — never candidates;
* a `module.toml` that is malformed, is not a table, or contains a non-table
  array element;
* a symlinked module directory pointing outside the module root.

A directory that has *neither* manifest style is still discovered (that is how
the pre-contract packs keep working) but it must expose a `Blueprint`: a folder
with nothing mountable is recorded as `FAILED_VALIDATION` with
`no Flask Blueprint`, because "discovered" must never be reported as "loaded".

## 2. Manifest grammar

`blueprints/<module_id>/module.toml`. Sections the contract does not know, and keys inside a known section, are
reported as an `unknown_key` **warning** listing the accepted keys. It is a
warning rather than an error so a module written for a newer contract can still
load, but it is never silent: a typo such as `depnds_on` would otherwise be read
as "this module has no dependencies".

| Key | Type | Meaning / validation |
|---|---|---|
| `module.schema_api` | int | contract version, currently `1`; anything else → status `FAILED_VALIDATION` |
| `module.id` | str | must equal the directory name, snake_case |
| `module.name`, `module.description` | str | display metadata (empty description = warning) |
| `module.version` | `X.Y[.Z]` | dotted version; recorded in the install ledger |
| `module.enabled` | bool | `false` → status `DISABLED`, nothing is imported |
| `module.depends_on` | list[str] | other module ids **or** core blueprints (`accounts`, `inventory`, `admin`, …) |
| `module.requires_ams` | str | minimum `APP_VERSION`; unsatisfied → `FAILED_VALIDATION` |
| `routes.order` | int | mount order (ties broken by id) |
| `routes.blueprint_variable` | str | name of the `Blueprint`, default `<module_id>_bp`; the package's `__init__.py` may also simply re-export it |
| `routes.url_prefix` | str | must start with `/`; `/` (or empty) is normalised to `/<module_id>` |
| `routes.expected_endpoints` | list[str] | `<blueprint>.<view>`; every one must exist after mount |
| `[navigation] items` | tables | see §3 |
| `[permissions] required` | str | a permission name; enforced through the existing `ENDPOINT_PERMISSION_MAP` / blueprint rules |
| `[permissions] allowed_roles` | list[str] | legacy compatibility, mapped onto the existing role rules |
| `[permissions].defaults` | map | `{can_view_x = true}` → the permission defaults a new role inherits |
| `[database] tables` | list[str] | tables the module owns — the audit's unit of attribution; a module may not claim a core table |
| `[database] models_import` | str | dotted path of model declarations, e.g. `blueprints.plant_registry.models` |
| `[[database.migrations]]` | tables | see §4 |
| `[[database.data_migrations]]` | tables | same shape, `KIND = "data"` |
| `[health] checks` | list[str] | `package.module:function` callables taking `app` and returning `{status, detail}` |
| `[tests] paths` | list[str] | test files relative to the **module root**, run by `dbupdate.py tests` |
| `[features]` | map | free-form flags for module-owned switches (the only open namespace) |

## 3. Navigation

```toml
[navigation]
items = [
  { id = "plant_registry.index", label = "Plant Register", endpoint = "plant_registry.index",
    icon = "bi-buildings", parent = "masters", order = 60, permission = "can_view_plant_registry" },
]
```

Validated by `app/services/module_system/navigation.py::validate_navigation()`:

* duplicate item `id` (across all modules **and** the core sidebar) → error;
* duplicate `endpoint` → error;
* `parent` must be one of the sidebar groups (`core`, `inventory`, `transactions`,
  `finance`, `ops`, `masters`, `reports`, `system`, or an id another module
  declares as a group) → error otherwise;
* `endpoint` must exist in `app.view_functions` → error;
* an item pointing at an *administrative* endpoint (`admin.*`, `system.*`,
  `settings.*`, deploy/maintenance blueprints) **must** declare a `permission` —
  that is an **error** and blocks the update; an endpoint that merely *looks*
  sensitive by name (`backup`, `migration`, …) is a warning, so a heuristic can
  never lock a legitimate module out (this is the "do not expose sensitive
  modules to all users" rule, enforced mechanically);
* `url_for()` on the item must resolve; `label`/`icon` must be non-empty;
* every `url_for('…')` written in `templates/layout.html` is checked too, so the
  shared sidebar cannot ship a dead link.

Rendering is metadata-driven: the factory's `inject_module_navigation` context
processor supplies `module_nav` and `module_nav_groups`, and the sidebar loop
sits immediately before the *System* section of `templates/layout.html`. Each
item is filtered through the **existing** permission engine
(`app.services.permissions._user_can`) — the module system defines no second
authorisation model. Declaring no permission still requires an authenticated
user; it never means "public".

## 4. Migrations declared by a module

```toml
[[database.migrations]]
version = "2026_001"
file = "migrations/2026_001_create_plant_registry_tables.py"
destructive = false
requires_data_validation = false
```

`file` must stay inside the module directory (no `..`), must exist, and must
follow the revision contract in `app/migrations/README.md`. A module revision is
only ever run by the update pipeline — never at import time, never by
`create_all`.

## 5. Statuses

| Status | Meaning | Blocks? |
|---|---|---|
| `DISCOVERED` | manifest read, not yet validated | — |
| `VALID` | validated, not mounted (dry-run states) | — |
| `DISABLED` | `enabled = false`, or env override `AMS_DISABLED_MODULES` | not imported |
| `FAILED_VALIDATION` | manifest invalid | module inactive, run blocked |
| `MISSING_DEPENDENCY` | depends on a missing/disabled/failed module or a newer app | module inactive, run blocked |
| `ROUTE_CONFLICT` | its blueprint name is owned by another module/core, or an expected endpoint resolves elsewhere | module inactive, run blocked |
| `REGISTERED` | mounted: blueprint + models active | — |
| `MIGRATION_REQUIRED` | registered, but pending revisions must run before use | update pipeline will apply |
| `READY` | registered, verified, nothing pending | — |
| `FAILED_HEALTH` | registered, but its own health check failed | update run reports it |

Statuses are ordered by severity in every report, so the worst thing about a
deployment is always at the top.

## 6. Adding a module (the whole recipe)

1. `blueprints/<my_module>/module.toml` — copy `blueprints/plant_registry/module.toml`
   and adjust `id`, `name`, `url_prefix`, tables, endpoints.
2. `_common.py` with `<my_module>_bp = Blueprint("<my_module>", __name__)` plus the
   shared helpers the pages need.
3. `models.py` (declare `db.Model`s with `__tablename__` inside your table list)
   and `pages.py` for routes. Import the models from `app/models/__init__.py`? —
   **No**: the manifest's `database.models_import` is wired by the registry, which
   is why no core file needs editing.
4. `migrations/<YYYY>_<NNN>_<slug>.py` for anything the models cannot express
   (data transforms, indexes on existing tables, backfills).
5. `health.py` with `def check(app) -> dict` (optional but recommended).
6. `tests/test_<my_module>.py` and declare it under `[tests]`.
7. `python tools/dbupdate.py check` (read-only, shows exactly what would run),
   then `python tools/dbupdate.py apply`. Nothing else — no factory edit, no
   layout edit, no permission-registry edit.

Reference implementation: `blueprints/plant_registry/` (a module with tables,
permissions, navigation, a schema revision, a data revision with an exception
report, and its own tests).

## 6a. What a revision may and may not do

`app/migrations/README.md` documents the revision contract for **core** changes;
module revisions follow the same rules. The parts that come from the module
system specifically:

* a revision receives a SQLAlchemy `Connection` inside the pipeline's
  transaction, and must be idempotent in effect (`CREATE TABLE IF NOT EXISTS`,
  guarded `ALTER`s);
* SQLite commits DDL implicitly, so a `CREATE`/`DROP` performed before a later
  failure in the same revision **cannot** always be rolled back. A revision that
  creates or changes objects may therefore define:

  ```python
  def undo(connection):
      connection.exec_driver_sql("DROP TABLE IF EXISTS plant_asset")
  ```

  The pipeline runs `undo()` when the revision fails and records
  `compensated: true`. Without `undo()`, the run is reported as needing
  restoration **from the backup the pipeline already took** — never as a silent
  success;
* data revisions must define `verify(connection)`; refusing to commit an
  unverified data change is deliberate;
* a revision may not delete rows or tables as a side effect of "cleanup". If a
  record cannot be transformed it is written to the exception report and kept.

## 7. Prohibitions the contract enforces

* no `create_all()` against a production database — the additive baseline is
  restricted to the policy states documented in
  `docs/DATABASE_UPDATE_PIPELINE.md`;
* no automatic `DROP TABLE` / `DROP COLUMN` / `ALTER COLUMN`: destructive
  revisions are refused unless `ALLOW_DESTRUCTIVE_MIGRATIONS` is explicitly on,
  and are always blocked in production unless the operator both enables it and
  passes `--yes`;
* no seeding over real data: seed paths remain opt-in and never run from the
  pipeline;
* no auto-execution of untrusted code: only validated, manifest-declared
  callables are imported, and only inside guarded steps;
* no activation with unresolved dependencies;
* no success claim without verification — `READY` is only written after the
  post-apply audit, integrity checks and regression verification pass.
