"""AMS module + database auto-upgrade subsystem.

Two cooperating halves live here and in :mod:`app.services.module_system`:

``app.services.module_system``
    contract / discovery / registry / navigation / module health.
``app.services.dbupdate``      (this package)
    environment policy, schema audit, revision discovery + validation, the
    twelve-step update pipeline, integrity checks, and the reports.

The application factory calls :func:`startup_bootstrap`; operators and CI call
``tools/dbupdate.py``; both end up in :func:`run_update` so there is exactly one
code path that can change the database.
"""
from __future__ import annotations

import logging
import os

from app.services.dbupdate.policy import (  # noqa: F401
    ENV_DEVELOPMENT,
    ENV_PRODUCTION,
    ENV_TEST,
    POLICY_AUDIT,
    POLICY_AUTO,
    POLICY_GUARDED,
    POLICY_MANUAL,
    UpdatePolicy,
    detect_environment,
    resolve as resolve_policy,
)

LOG = logging.getLogger("ams.dbupdate")

MODE_CHECK = "check"
MODE_PLAN = "plan"
MODE_APPLY = "apply"

__all__ = [
    "MODE_APPLY",
    "MODE_CHECK",
    "MODE_PLAN",
    "UpdatePolicy",
    "detect_environment",
    "docs_path",
    "generate_docs",
    "resolve_policy",
    "run_update",
    "startup_bootstrap",
]


def run_update(app, **kwargs) -> dict:
    """Lazily import the pipeline (keeps ``import app.services.dbupdate`` cheap)."""
    from app.services.dbupdate.runner import run_update as _run

    return _run(app, **kwargs)


def generate_docs(app=None, registry=None, *, write: bool = True) -> dict:
    """Render ``docs/MODULE_REGISTRY.md`` from live, validated module metadata.

    ``write=False`` is a dry run: the document is built (so a rendering bug is
    still visible) but nothing on disk changes.
    """
    from app.services.dbupdate import docs

    return docs.generate(app, registry=registry, write=write)


def docs_path(app=None):
    from pathlib import Path

    if app is not None:
        return Path(app.root_path).parent / "docs" / "MODULE_REGISTRY.md"
    return None


def _check_only_under_test(app) -> bool:
    """Should an automated test run downgrade the startup update to check-only?

    Pytest creates an application per test.  Booting the full pipeline each
    time (backup + integrity + archives) makes the suite slow enough that
    developers stop running it, which is its own correctness risk — so under
    pytest the startup call *reports* and the factory keeps using the historical
    bootstrap, exactly as before.  Pipeline behaviour itself is covered by
    ``tests/test_dbupdate_pipeline.py``, which calls ``run_update`` directly.

    Deliberately narrow: any explicit ``AMS_UPDATE_POLICY`` (or
    ``AMS_UPDATE_UNDER_TESTS=1``) restores the real behaviour, so a test can opt
    in to the full path.
    """
    if (os.environ.get("AMS_UPDATE_UNDER_TESTS") or "").strip().lower() in ("1", "true", "yes"):
        return False
    configured = ""
    if app is not None:
        configured = str(app.config.get("AMS_UPDATE_POLICY") or "").strip()
    if not configured:
        configured = (os.environ.get("AMS_UPDATE_POLICY") or "").strip()
    if configured:
        return False
    return bool(os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("PYTEST_XDIST_WORKER"))


def startup_bootstrap(app) -> dict:
    """One entry point for the factory: bring the database up to code.

    Behaviour is decided by :mod:`app.services.dbupdate.policy`, and the whole
    call is fail-safe: if the update pipeline itself breaks, the ERP falls back
    to the historical bootstrap so a management subsystem can never take the
    business offline.  The failure is recorded either way.

    The returned report carries ``handled``: whether this call already brought
    the schema up to date.  When it is false the factory must still run the
    legacy ``_bootstrap_database()`` — a check-only pass never touches the
    database, so the tables have to come from somewhere.
    """
    from app.services.module_system import get_registry
    from app.services.dbupdate.runner import MODE_APPLY, MODE_CHECK

    policy = resolve_policy(app)
    registry = get_registry(app)
    mode = MODE_APPLY if policy.auto_apply else MODE_CHECK
    reason = "check-only policy"
    if mode == MODE_APPLY and _check_only_under_test(app):
        mode = MODE_CHECK
        reason = "automated test run"
    report: dict = {}
    fallback_completed = False
    try:
        report = run_update(app, mode=mode, trigger="startup", registry=registry)
    except Exception:
        LOG.critical("update pipeline failed during startup; falling back to the legacy bootstrap", exc_info=True)
        app.config["AMS_UPDATE_PIPELINE_ERROR"] = "update pipeline raised; legacy bootstrap used instead"
        try:
            from app.services.schema import _bootstrap_database

            _bootstrap_database()
            fallback_completed = True
        except Exception:
            LOG.critical("legacy bootstrap also failed", exc_info=True)
            raise
    finally:
        report = report if isinstance(report, dict) else {}
        report["mode_requested"] = mode
        report["check_only_reason"] = "" if mode == MODE_APPLY else reason
        # Only an apply pass (or a policy that intentionally owns startup) may
        # claim the bootstrap; see ``handled`` below.
        report["handled"] = bool(report) and (
            mode == MODE_APPLY or policy.policy in (POLICY_AUDIT, POLICY_MANUAL)
        ) or fallback_completed
        app.config["AMS_BOOTSTRAP_HANDLED_BY_DBUPDATE"] = bool(report["handled"])
        if report:
            app.config["AMS_UPDATE_FINAL_STATUS"] = report.get("final_status", "")
            app.config["AMS_UPDATE_REPORT_FILES"] = report.get("report_files", {})
            blockers = report.get("blockers") or []
            for blocker in blockers:
                LOG.error(
                    "update blocker [%s] %s — why: %s | fix: %s",
                    blocker.get("step"),
                    blocker.get("what"),
                    blocker.get("why") or "n/a",
                    blocker.get("next_action") or "n/a",
                )
    return report
