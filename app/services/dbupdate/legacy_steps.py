"""The pre-existing, implicit bootstrap chain, made explicit and auditable.

``app/services/schema.py::_bootstrap_database()`` has always been the ERP's real
"migration system": ``db.create_all()`` followed by ~20 idempotent
``_ensure_*`` upgrades and a few one-off backfills, each wrapped in
``try/except: pass``.  That chain is *kept*, because a lot of behaviour
depends on it, but it is now:

* enumerated here, in the same order, so it can be reviewed as a list;
* executed either as one baseline (``mode="chain"``, byte-identical to today)
  or step by step (``mode="steps"``, per-step result recorded);
* verified afterwards by :mod:`app.services.dbupdate.schema_audit`, which is how
  a swallowed ``except: pass`` finally becomes visible.
"""
from __future__ import annotations

import logging
import time

LOG = logging.getLogger("ams.dbupdate.legacy")

MODE_CHAIN = "chain"
MODE_STEPS = "steps"

#: (ledger step name, function in app.services.schema) in execution order.
#: ``create_all`` is handled separately because it is not a function there.
STEPS: tuple[tuple[str, str], ...] = (
    ("create_all", "__create_all__"),
    ("release_stale_system_locks", "_release_stale_system_locks"),
    ("ensure_user_password_column", "_ensure_user_password_column"),
    ("ensure_model_columns", "_ensure_model_columns"),
    ("ensure_default_admin", "_ensure_default_admin"),
    ("ensure_account_type_compat", "_ensure_account_type_compat"),
    ("backfill_accounting_integrity_columns", "_backfill_accounting_integrity_columns"),
    ("ensure_account_classification_columns", "_ensure_account_classification_columns"),
    ("ensure_material_categories", "_ensure_material_categories"),
    ("ensure_discount_columns", "_ensure_discount_columns"),
    ("ensure_bill_counter_namespace_defaults", "_ensure_bill_counter_namespace_defaults"),
    ("ensure_waive_off_table", "_ensure_waive_off_table"),
    ("ensure_delivery_person_payments_table", "_ensure_delivery_person_payments_table"),
    ("backfill_legacy_payment_discounts_to_waive_off", "_backfill_legacy_payment_discounts_to_waive_off"),
    ("backfill_sale_delivery_persons_from_legacy", "_backfill_sale_delivery_persons_from_legacy"),
    ("ensure_user_permission_defaults", "_ensure_user_permission_defaults"),
    ("ensure_direct_sale_idempotency_index", "_ensure_direct_sale_idempotency_index"),
    ("ensure_auto_bill_unique_indexes", "_ensure_auto_bill_unique_indexes"),
    ("ensure_open_khata_client", "ensure_open_khata_client"),
    ("ensure_performance_indexes", "_ensure_performance_indexes"),
    ("bootstrap_tenancy", "bootstrap_tenancy"),
)


def step_names() -> list[str]:
    return [name for name, _ in STEPS]


def verify_against_bootstrap_source() -> list[str]:
    """Names called by ``_bootstrap_database`` — used to detect list drift.

    Matches the bare ``some_step()`` lines of the legacy chain (a call on an
    object such as ``db.create_all()`` is deliberately not matched).
    """
    import inspect
    import re

    from app.services import schema

    source = inspect.getsource(schema._bootstrap_database)
    return re.findall(r"^\s+([a-z_][a-z0-9_]*)\(\)$", source, re.MULTILINE)


def undocumented_steps() -> list[str]:
    """Ensure-steps present in ``_bootstrap_database`` but missing from STEPS.

    The inventory in :data:`STEPS` is what makes the historical chain auditable:
    a step nobody declared would otherwise run (or be skipped) without ever
    appearing in a report.  CI fails if this list is non-empty.
    """
    declared = {function_name for _, function_name in STEPS}
    return sorted(set(verify_against_bootstrap_source()) - declared)


def unrun_steps() -> list[str]:
    """Declared steps that no longer exist in the bootstrap source."""
    present = set(verify_against_bootstrap_source())
    declared = {function_name for _, function_name in STEPS}
    # ``create_all`` is the ORM baseline the pipeline always performs, so it is
    # intentionally not spelled out in the legacy source.
    return sorted(declared - present - {"__create_all__"})


def _call(schema_module, function_name: str) -> None:
    if function_name == "__create_all__":
        from models import db

        db.create_all()
        return
    target = getattr(schema_module, function_name, None)
    if target is None:
        for module_name in ("app.services.schema",):
            candidate = getattr(__import__(module_name, fromlist=[function_name]), function_name, None)
            if candidate is not None:
                target = candidate
                break
    if not callable(target):
        raise LookupError(f"legacy bootstrap step '{function_name}' is not callable")
    target()


def run(app, *, mode: str = MODE_CHAIN, only: list[str] | None = None) -> dict:
    """Execute the legacy bootstrap under the requested granularity."""
    from models import db
    from app.services import schema

    started = time.time()
    results: list[dict] = []
    failures: list[str] = []

    if only:
        wanted = set(only)
        steps = [(name, fn) for name, fn in STEPS if name in wanted]
    elif mode == MODE_STEPS:
        steps = list(STEPS)
    else:
        steps = []

    if steps:
        for name, function_name in steps:
            step_started = time.time()
            try:
                _call(schema, function_name)
                results.append({"step": name, "status": "OK", "duration_ms": int((time.time() - step_started) * 1000)})
            except Exception as exc:
                failures.append(name)
                try:
                    db.session.rollback()
                except Exception:
                    pass
                LOG.error("legacy bootstrap step '%s' failed: %s: %s", name, type(exc).__name__, exc)
                results.append(
                    {
                        "step": name,
                        "status": "FAILED",
                        "error": f"{type(exc).__name__}: {exc}",
                        "duration_ms": int((time.time() - step_started) * 1000),
                    }
                )
        executed = "steps"
    else:
        try:
            schema._bootstrap_database()
            results.append({"step": "legacy_bootstrap_chain", "status": "OK"})
            executed = "chain"
        except Exception as exc:  # the chain swallows step errors, so this is rare
            failures.append("legacy_bootstrap_chain")
            LOG.exception("legacy bootstrap chain raised")
            results.append({"step": "legacy_bootstrap_chain", "status": "FAILED", "error": f"{type(exc).__name__}: {exc}"})
            executed = "chain"

    return {
        "executed": executed,
        "duration_ms": int((time.time() - started) * 1000),
        "steps": results,
        "failures": failures,
        "database": str(db.engine.url.database),
    }
