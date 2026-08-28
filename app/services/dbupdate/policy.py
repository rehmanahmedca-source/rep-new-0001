"""Environment + update policy: what the ERP may do to its own database, where.

Two independent knobs, resolved once per process:

``AMS_ENV``      ``development`` (default) | ``test`` | ``production``
                 Production is *auto-detected* from the PythonAnywhere markers
                 the WSGI file already uses, so a live server cannot forget to
                 declare itself.

``AMS_UPDATE_POLICY``
    ``auto``     discover, validate, audit, back up, then apply pending
                 **non-destructive** migrations at startup.  Default in
                 development/test.
    ``guarded``  everything ``auto`` does, except that migrations are applied
                 only when they are additive, checksummed and lint-clean, and a
                 verified backup exists first.  Default in production — this is
                 the policy the GitHub auto-deploy relies on, because that
                 pipeline migrates by importing the app.
    ``audit``    detect, validate and report.  Never writes schema.
    ``manual``   report only; an operator runs ``tools/dbupdate.py apply``.

Safety invariants that no policy may weaken
-------------------------------------------
* ``create_all`` / additive ALTERs are never run against a *destructive*
  migration; destructive revisions are refused unless
  ``AMS_ALLOW_DESTRUCTIVE_MIGRATIONS=1`` **and** a verified backup exists.
* a production database is never reset, dropped, or re-seeded;
* ``AMS_ENV=production`` ignores any policy that would skip the backup.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

LOG = logging.getLogger("ams.dbupdate.policy")

ENV_DEVELOPMENT = "development"
ENV_TEST = "test"
ENV_PRODUCTION = "production"

POLICY_AUTO = "auto"
POLICY_GUARDED = "guarded"
POLICY_AUDIT = "audit"
POLICY_MANUAL = "manual"
POLICIES = (POLICY_AUTO, POLICY_GUARDED, POLICY_AUDIT, POLICY_MANUAL)

_PRODUCTION_MARKERS = ("PYTHONANYWHERE_DOMAIN", "PYTHONANYWHERE_SITE")


def detect_environment(app=None) -> str:
    """Explicit ``AMS_ENV`` wins; hosting markers then infer ``production``."""
    configured = ""
    if app is not None:
        configured = str(app.config.get("AMS_ENV") or "").strip().lower()
    if not configured:
        configured = (os.environ.get("AMS_ENV") or "").strip().lower()
    if configured in (ENV_DEVELOPMENT, ENV_TEST, ENV_PRODUCTION):
        return configured
    if configured in ("prod", "live"):
        return ENV_PRODUCTION
    if configured in ("dev", "local"):
        return ENV_DEVELOPMENT
    if any(marker in os.environ for marker in _PRODUCTION_MARKERS):
        return ENV_PRODUCTION
    if app is not None and bool(app.config.get("TESTING")):
        return ENV_TEST
    return ENV_DEVELOPMENT


def default_policy(environment: str) -> str:
    return POLICY_GUARDED if environment == ENV_PRODUCTION else POLICY_AUTO


@dataclass(frozen=True)
class UpdatePolicy:
    environment: str
    policy: str
    allow_destructive: bool
    require_backup: bool
    auto_apply: bool
    allow_create_all_on_populated: bool
    run_regression: bool
    reset_allowed: bool
    regenerate_docs: bool = True
    notes: tuple[str, ...] = ()

    @property
    def is_production(self) -> bool:
        return self.environment == ENV_PRODUCTION

    def as_dict(self) -> dict:
        return {
            "environment": self.environment,
            "policy": self.policy,
            "allow_destructive": self.allow_destructive,
            "require_backup_before_schema_change": self.require_backup,
            "auto_apply_safe_migrations": self.auto_apply,
            "create_all_allowed_on_populated_db": self.allow_create_all_on_populated,
            "regenerate_module_docs": self.regenerate_docs,
            "run_regression_after_update": self.run_regression,
            "database_reset_allowed": self.reset_allowed,
            "notes": list(self.notes),
        }


def _flag(name: str, default: bool, app=None) -> bool:
    """``AMS_*`` environment variable, else the committed config.py default."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        if app is not None:
            configured = app.config.get(name)
            if configured is not None:
                return bool(configured) if isinstance(configured, bool) else str(configured).strip().lower() not in (
                    "0",
                    "false",
                    "no",
                    "off",
                    "",
                )
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _docs_default() -> bool:
    """``REGENERATE_MODULE_DOCS`` from the deployment config (default: on)."""
    try:
        from config import deployment_config

        return bool(deployment_config().get("update", {}).get("regenerate_docs", True))
    except Exception:
        return True


def resolve(app=None) -> UpdatePolicy:
    """Build the effective policy for this process (env overrides config)."""
    environment = detect_environment(app)
    configured = ""
    if app is not None:
        configured = str(app.config.get("AMS_UPDATE_POLICY") or "").strip().lower()
    if not configured:
        configured = (os.environ.get("AMS_UPDATE_POLICY") or "").strip().lower()
    if configured not in POLICIES:
        configured = default_policy(environment)

    notes: list[str] = []
    is_production = environment == ENV_PRODUCTION
    auto_apply = configured in (POLICY_AUTO, POLICY_GUARDED)
    allow_destructive = _flag("AMS_ALLOW_DESTRUCTIVE_MIGRATIONS", False, app)
    require_backup = _flag("AMS_REQUIRE_BACKUP_BEFORE_UPDATE", True, app)
    allow_create_all_populated = _flag("AMS_ALLOW_CREATE_ALL_ON_POPULATED", not is_production, app)
    run_regression = _flag("AMS_RUN_REGRESSION_ON_UPDATE", True, app)
    reset_allowed = _flag("AMS_ALLOW_DB_RESET", not is_production, app)
    regenerate_docs = _flag("AMS_REGENERATE_MODULE_DOCS", _docs_default(), app)

    if is_production and allow_destructive:
        notes.append(
            "AMS_ALLOW_DESTRUCTIVE_MIGRATIONS=1 is set on a production environment; "
            "destructive revisions will still require a verified backup"
        )
    if is_production and not require_backup:
        # Refuse to be talked out of the backup on a live ledger.
        require_backup = True
        notes.append("AMS_REQUIRE_BACKUP_BEFORE_UPDATE=0 is ignored in production")
    if is_production and reset_allowed:
        reset_allowed = False
        notes.append("database reset is disabled in production regardless of AMS_ALLOW_DB_RESET")
    if configured == POLICY_GUARDED:
        notes.append("guarded: additive migrations only; lint + checksum + backup + verification mandatory")
    if configured in (POLICY_AUDIT, POLICY_MANUAL):
        auto_apply = False
        notes.append(f"{configured}: schema is reported, not modified, at startup")

    return UpdatePolicy(
        environment=environment,
        policy=configured,
        allow_destructive=allow_destructive,
        require_backup=require_backup,
        auto_apply=auto_apply,
        allow_create_all_on_populated=allow_create_all_populated,
        run_regression=run_regression,
        reset_allowed=reset_allowed,
        regenerate_docs=regenerate_docs,
        notes=tuple(notes),
    )
