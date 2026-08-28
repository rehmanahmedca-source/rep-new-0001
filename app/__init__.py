"""Application factory — modular AMS ERP."""
from __future__ import annotations

import os
import secrets
import logging
import traceback
from logging.handlers import RotatingFileHandler
from datetime import timedelta
from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from sqlalchemy import event

from models import db
from utils.module_loader import load_modules


_UPDATE_FALLBACK = {
    "environment": "",
    "policy": "",
    "allow_destructive": False,
    "require_backup": True,
    "run_integrity": True,
    "run_module_tests": False,
    "regenerate_docs": True,
    "report_history": 12,
}


#: Sidebar heading for each navigation parent a module may declare.
NAV_GROUP_LABELS = {
    "": "Modules",
    "core": "Modules",
    "masters": "Directory",
    "inventory": "Inventory",
    "transactions": "Transactions",
    "finance": "Finance",
    "ops": "Operations",
    "reports": "Reports",
    "system": "System",
}


def _register_module_navigation(app: Flask) -> None:
    """Expose validated module navigation to the shared layout.

    The core sidebar stays hand-tuned in ``templates/layout.html``; what a new
    module gets for free is its own entry, ordered and grouped by its manifest
    and filtered through the application's *existing* permission rules
    (``_user_can``).  A module that declares no permission is still gated on
    authentication, and an item whose endpoint does not resolve is dropped here
    and reported by the update pipeline instead of exploding the layout.
    """

    @app.context_processor
    def inject_module_navigation():
        empty = {"module_nav": [], "module_nav_groups": {}}
        registry = app.extensions.get("ams_modules")
        if registry is None:
            return empty
        try:
            from flask import url_for
            from flask_login import current_user

            from app.services.permissions import _user_can
        except Exception:  # pragma: no cover - defensive
            return empty

        items: list[dict] = []
        for item in registry.navigation(app):
            if not item.get("resolvable"):
                continue
            permission = item.get("permission") or ""
            if permission or item.get("visible_to") == "permissioned":
                if not getattr(current_user, "is_authenticated", False):
                    continue
                if permission and not _user_can(permission):
                    continue
            try:
                item["href"] = url_for(item["endpoint"])
            except Exception:
                continue
            items.append(item)

        groups: dict[str, list[dict]] = {}
        for item in items:
            label = NAV_GROUP_LABELS.get(str(item.get("parent") or ""), str(item.get("parent") or "Modules").title())
            groups.setdefault(label, []).append(item)
        for entries in groups.values():
            entries.sort(key=lambda entry: int(entry.get("order") or 0))
        return {"module_nav": items, "module_nav_groups": groups}


def _resolve_update_defaults() -> dict:
    """Read the update policy block from config.py without ever failing boot.

    ``config.py`` is the single source of truth for deployment *and* update
    behaviour; a broken/unimportable config must not stop the ERP, so the
    documented fallbacks are used and the import error is logged.
    """
    try:
        import config as project_config

        update = dict(project_config.get_config().get("update") or {})
    except Exception:
        logging.getLogger(__name__).warning(
            "config.py update section unavailable; using built-in update defaults",
            exc_info=True,
        )
        return dict(_UPDATE_FALLBACK)
    merged = dict(_UPDATE_FALLBACK)
    for key, value in update.items():
        if value is not None:
            merged[key] = value
    return merged


def create_app(test_config: dict | None = None) -> Flask:
    root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
        instance_path=str(root / "instance"),
        instance_relative_config=True,
    )

    instance_dir = root / "instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = instance_dir / ".tmp"
    (tmp_dir / "import_uploads").mkdir(parents=True, exist_ok=True)
    (tmp_dir / "import_reports").mkdir(parents=True, exist_ok=True)

    schema_version = (os.environ.get("AMS_SCHEMA_VERSION") or "v44").strip().lower()
    if schema_version in {"legacy", "v3", "live"}:
        default_db_name = "ahmed_cement.db"
    else:
        # v4.4 is the only supported runtime schema.
        default_db_name = "ahmed_cement_v44_fresh.db"
        schema_version = "v44"
    db_path = os.environ.get("APP_DB_PATH") or str(instance_dir / default_db_name)
    # Never silently reopen the retired live/migrated databases.
    if Path(db_path).name in {"ahmed_cement.db", "ahmed_cement_v44.db"} and schema_version == "v44":
        db_path = str(instance_dir / default_db_name)
    os.environ["AMS_SCHEMA_VERSION"] = schema_version
    os.environ["APP_DB_PATH"] = db_path
    app_schema_version = schema_version
    # SQLite creates the database file on first connection, but it does not
    # create a missing custom parent directory.  Make a configured database
    # path just as safe as the default instance path on a fresh installation.
    db_parent = Path(db_path).expanduser().parent
    db_parent.mkdir(parents=True, exist_ok=True)
    max_upload_mb = int(os.environ.get("MAX_UPLOAD_MB", "256") or "256")
    _update_defaults = _resolve_update_defaults()
    journal_mode = _resolve_sqlite_journal_mode(db_path)

    # Empty instance / deleted database → create a fresh empty SQLite file.
    # Existing database → leave it alone (no wipe, no replace, no extra locks).
    from app.services.instance_files import ensure_instance_runtime

    runtime_status = ensure_instance_runtime(
        instance_dir=instance_dir,
        db_path=Path(db_path),
        journal_mode=journal_mode,
    )
    snapshot_path = Path(
        os.environ.get("DB_HEALTH_SNAPSHOT_PATH")
        or str(Path(db_path).parent / "health_snapshot.json")
    )

    secret_file = instance_dir / "secret_key"
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        if secret_file.exists():
            secret = secret_file.read_text(encoding="utf-8").strip()
        if not secret:
            secret = secrets.token_hex(32)
            secret_file.write_text(secret, encoding="utf-8")

    # Yard PCs use plain HTTP (http://192.168.x.x:5000). Secure + SameSite=None
    # cookies are dropped on HTTP, so POST /login 302 then GET / bounces to login.
    # For HTTPS/iframe set AMS_HTTPS=1 (or SESSION_COOKIE_SECURE=1 + SAMESITE=None).
    env_secure = os.environ.get("SESSION_COOKIE_SECURE")
    env_samesite = os.environ.get("SESSION_COOKIE_SAMESITE")
    use_https = (os.environ.get("AMS_HTTPS") or "").strip() == "1"
    if env_secure is None:
        cookie_secure = bool(use_https)
    else:
        cookie_secure = env_secure.strip() not in ("0", "false", "False", "")
    cookie_samesite = (env_samesite or ("None" if cookie_secure else "Lax")).strip() or "Lax"
    if str(cookie_samesite).lower() == "none" and not cookie_secure:
        cookie_samesite = "Lax"

    app.config.update(
        SECRET_KEY=secret,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{db_path}",
        AMS_SCHEMA_VERSION=app_schema_version,
        APP_DB_PATH=db_path,
        AMS_V44_SCHEMA_PATH=str(root / "v44" / "SCHEMA_v4_4.sql"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={
            "connect_args": {"timeout": 30, "check_same_thread": False},
        },
        SQLITE_JOURNAL_MODE=journal_mode,
        AMS_RUNTIME_DB_CREATED=bool(runtime_status.created),
        DB_HEALTH_SNAPSHOT_PATH=str(snapshot_path),
        MAX_CONTENT_LENGTH=max_upload_mb * 1024 * 1024,
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        REMEMBER_COOKIE_DURATION=timedelta(days=30),
        SESSION_COOKIE_NAME="ams_session",
        SESSION_COOKIE_PATH="/",
        SESSION_COOKIE_DOMAIN=None,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=cookie_samesite,
        SESSION_COOKIE_SECURE=cookie_secure,
        SESSION_REFRESH_EACH_REQUEST=True,
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE=cookie_samesite,
        REMEMBER_COOKIE_SECURE=cookie_secure,
        PREFERRED_URL_SCHEME="https" if cookie_secure else "http",
        FULL_RAW_IMPORT_ENABLED="1",
        IMPORT_TMP_DIR=str(tmp_dir),
        IMPORT_UPLOADS_DIR=str(tmp_dir / "import_uploads"),
        IMPORT_REPORTS_DIR=str(tmp_dir / "import_reports"),
        IMPORT_ARTIFACT_RETENTION_SECONDS=int(
            os.environ.get("IMPORT_ARTIFACT_RETENTION_SECONDS", str(7 * 24 * 3600)) or "0"
        ),
        UPLOAD_DIR=os.environ.get("UPLOAD_DIR", str(root / "static" / "uploads")),
        BACKUP_DIR=os.environ.get("BACKUP_DIR", str(instance_dir / "storage" / "backups")),
        MAINTENANCE_TEMP_DIR=os.environ.get("MAINTENANCE_TEMP_DIR", str(instance_dir / "storage" / "temp")),
        BACKUP_INTERVAL_SECONDS=int(os.environ.get("BACKUP_INTERVAL_SECONDS", "3600") or "3600"),
        BACKUP_RETENTION=int(os.environ.get("BACKUP_RETENTION", "3") or "3"),
        BACKUP_LOCK_STALE_SECONDS=int(os.environ.get("BACKUP_LOCK_STALE_SECONDS", "7200") or "7200"),
        TEMP_RETENTION_SECONDS=int(os.environ.get("TEMP_RETENTION_SECONDS", "86400") or "86400"),
        # ---- module + database update subsystem ------------------------------
        # Policy values come from config.py (which itself prefers AMS_* env
        # overrides), so there is one place to change them and no host can
        # accidentally inherit development behaviour in production.
        APP_VERSION=(os.environ.get("AMS_APP_VERSION") or "1.0.0").strip() or "1.0.0",
        AMS_ENV=(os.environ.get("AMS_ENV") or _update_defaults["environment"] or "").strip(),
        AMS_UPDATE_POLICY=(
            os.environ.get("AMS_UPDATE_POLICY") or _update_defaults["policy"] or ""
        ).strip(),
        AMS_ALLOW_DESTRUCTIVE_MIGRATIONS=_update_defaults["allow_destructive"],
        AMS_REQUIRE_BACKUP_BEFORE_UPDATE=_update_defaults["require_backup"],
        AMS_RUN_REGRESSION_ON_UPDATE=_update_defaults["run_integrity"],
        MIGRATIONS_DIR=str(root / "app" / "migrations"),
        UPDATE_REPORT_DIR=os.environ.get(
            "UPDATE_REPORT_DIR", str(instance_dir / "logs")
        ),
        UPDATE_REPORT_HISTORY=int(
            os.environ.get("UPDATE_REPORT_HISTORY", str(_update_defaults["report_history"])) or "12"
        ),
        AMS_MODULE_ROOT=os.environ.get("AMS_MODULE_ROOT") or str(root / "blueprints"),
        MIN_FREE_DISK_BYTES=int(os.environ.get("MIN_FREE_DISK_BYTES", str(100 * 1024 * 1024)) or "0"),
        # Do not create backup database files from the web process. Backups
        # remain available only through an explicit maintenance operation.
        BACKUP_EMBEDDED_SCHEDULER=(os.environ.get("BACKUP_EMBEDDED_SCHEDULER", "0").strip().lower() not in ("0", "false", "no")),
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)

    _configure_logging(app)
    db.init_app(app)

    # SQLite does not enforce declared foreign keys unless each connection
    # explicitly enables them. Register this before bootstrap opens the first
    # connection so future lifecycle regressions fail transactionally instead
    # of accumulating silent dangling rows.
    with app.app_context():
        engine = db.engine
        if engine.dialect.name == "sqlite" and not getattr(engine, "_ams_fk_listener", False):
            @event.listens_for(engine, "connect")
            def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA foreign_keys=ON")
                    cursor.execute("PRAGMA busy_timeout=8000")
                    enabled = cursor.execute("PRAGMA foreign_keys").fetchone()
                    if not enabled or enabled[0] != 1:
                        raise RuntimeError("SQLite foreign-key enforcement could not be enabled")
                finally:
                    cursor.close()

            engine._ams_fk_listener = True

    login_manager = LoginManager()
    login_manager.login_view = "login"
    # None: same user (or several managers) may stay logged in from many IPs/PCs.
    # "basic"/"strong" can drop a session when IP or User-Agent differs.
    login_manager.session_protection = None
    login_manager.init_app(app)

    from app.services.permissions import load_user

    login_manager.user_loader(load_user)

    # Core domain routes first so short names (clients, login, …) are not
    # stolen by later feature packs such as fbm_rentals.clients.
    from app.blueprints.core import bp as core_bp
    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.sales import bp as sales_bp
    from app.blueprints.masters import bp as masters_bp
    from app.blueprints.ledgers import bp as ledgers_bp
    from app.blueprints.ops import bp as ops_bp
    from app.blueprints.reports import bp as reports_bp
    from app.blueprints.api import bp as api_bp
    from app.blueprints.system import bp as system_bp
    from app.blueprints.misc import bp as misc_bp
    from app.blueprints.migration import bp as migration_bp

    for bp in (
        core_bp,
        auth_bp,
        sales_bp,
        masters_bp,
        ledgers_bp,
        ops_bp,
        reports_bp,
        api_bp,
        system_bp,
        misc_bp,
        migration_bp,
    ):
        if bp.name not in app.blueprints:
            app.register_blueprint(bp)

    _alias_unprefixed_endpoints(app)

    # ``AMS_MODULE_ROOT`` (config.py / environment) lets a deployment stage the
    # module pack somewhere other than blueprints/ — and lets the pipeline tests
    # point discovery at a fixture directory.  Empty falls back to the default.
    load_modules(
        app,
        blueprint_dir=str(app.config.get("AMS_MODULE_ROOT") or (root / "blueprints")),
    )

    _register_module_navigation(app)

    from app.hooks import register_hooks

    register_hooks(app)

    from app.services.import_jobs import register_import_job_routes

    register_import_job_routes(app)

    # Public health probe and the config-driven GitHub auto-deploy webhook
    # (/health, /git-auto-pull). Registered here so they are present under
    # both wsgi.py (PythonAnywhere) and main.py (local) entry points.
    try:
        from app.deploy_routes import register_deploy_routes

        register_deploy_routes(app)
    except Exception:
        logging.getLogger(__name__).warning(
            "Deploy routes not registered (config/deploy package issue).",
            exc_info=True,
        )

    # Compile every Jinja template once at startup so the first request after a
    # worker (re)start doesn't pay a ~200ms+ compile cost for layout.html +
    # the large module pages.  Compilation is side-effect free and the bytecode
    # cache then serves every later request.
    _warm_template_cache(app)

    with app.app_context():
        try:
            from app.services import health as health_service
            from app.services import constants as constants_svc
            # Health protection and service helpers must follow the configured
            # v4.4 file, not a retired live path captured at import time.
            health_service.db_path = db_path
            constants_svc.db_path = db_path
            health_service._DB_HEALTH_SNAPSHOT_PATH = str(snapshot_path)
            constants_svc._DB_HEALTH_SNAPSHOT_PATH = str(snapshot_path)
            from app.services import schema as schema_svc
            schema_svc.db_path = db_path
            schema_svc._DB_HEALTH_SNAPSHOT_PATH = str(snapshot_path)
            from app.services.health import (
                _guard_db_file_before_bootstrap,
                _db_health_check_after_bootstrap,
            )
            from app.services.schema import _bootstrap_database
            if runtime_status.created:
                # Recreating a deleted/missing file must not be treated as
                # accidental data loss against a leftover health snapshot.
                app.config["_DB_FILE_WAS_MISSING"] = True
                try:
                    snapshot_path.unlink(missing_ok=True)
                except OSError:
                    pass

            if app.config.get("TESTING"):
                from app.services.schema import _ensure_default_admin, _ensure_model_columns
                db.create_all()
                _ensure_model_columns()
                # Keep a fresh test database usable in the same way as a fresh
                # production database.  The smoke tests and local developers
                # rely on the documented Admin login even when no rows exist.
                _ensure_default_admin()
            else:
                if app.config.get("AMS_SCHEMA_VERSION") == "v44":
                    from app.services.v44_schema import (
                        initialize_v44_database,
                        retire_legacy_database_files,
                    )
                    retire_legacy_database_files(instance_dir, extra_dirs=[root / "v44"])
                    _retire_stale_live_health_snapshot(snapshot_path)
                _guard_db_file_before_bootstrap()
                if app.config.get("AMS_SCHEMA_VERSION") == "v44":
                    # The optional v4.4 SQL bundle is a *nice to have*: it seeds
                    # the new roles/permissions tables.  It must never be able to
                    # abort the ORM bootstrap below, because that leaves the
                    # database without the `user` table and every login POST
                    # then dies with "no such table: user" (HTTP 500).
                    try:
                        initialize_v44_database(
                            db_path,
                            default_user=(os.environ.get("DEFAULT_ADMIN_USER") or "Admin").strip() or "Admin",
                            default_password=(os.environ.get("DEFAULT_ADMIN_PASSWORD") or "Admin@fbm12345").strip() or "Admin@fbm12345",
                        )
                    except Exception:
                        logging.getLogger(__name__).warning(
                            "v4.4 schema bootstrap skipped; continuing with the "
                            "ORM schema bootstrap.",
                            exc_info=True,
                        )
                app.config["AMS_BOOTSTRAP_HANDLED_BY_DBUPDATE"] = False
                if (os.environ.get("AMS_SKIP_UPDATE_PIPELINE") or "").strip().lower() not in ("1", "true", "yes"):
                    # The database update subsystem owns the bootstrap: it runs the
                    # historical additive ensure-chain *and* any versioned module or
                    # core revision, verifies the outcome, checks integrity and
                    # writes the update report.  Policy comes from config.py, so a
                    # production host can never inherit development behaviour.
                    try:
                        from app.services.dbupdate import startup_bootstrap

                        update_report = startup_bootstrap(app) or {}
                        # ``handled`` is the pipeline's own statement that it
                        # brought the schema up to date; anything else (a
                        # check-only pass, an automated test run, a skipped
                        # subsystem) leaves the historical bootstrap in charge.
                        app.config["AMS_BOOTSTRAP_HANDLED_BY_DBUPDATE"] = bool(
                            update_report.get("handled")
                        )
                    except Exception:
                        logging.getLogger(__name__).critical(
                            "database update subsystem failed; falling back to the legacy bootstrap",
                            exc_info=True,
                        )
                if not app.config.get("AMS_BOOTSTRAP_HANDLED_BY_DBUPDATE"):
                    # Either the operator skipped the pipeline
                    # (AMS_SKIP_UPDATE_PIPELINE), the policy is check-only, or the
                    # pipeline failed: keep the ERP on the bootstrap path it has
                    # always used.
                    _bootstrap_database()
                try:
                    _db_health_check_after_bootstrap()
                except Exception:
                    # A health *report* failure must not leave the app running
                    # against a half-bootstrapped database.
                    logging.getLogger(__name__).warning(
                        "Post-bootstrap DB health check failed", exc_info=True
                    )
        except Exception:
            logging.getLogger(__name__).critical(
                "DATABASE BOOTSTRAP FAILED — the application will return HTTP 500 "
                "on any page that touches the database.",
                exc_info=True,
            )
            app.config["AMS_BOOTSTRAP_ERROR"] = traceback.format_exc()

    # Start once at application startup, never from a user request. The
    # cross-process filesystem lock prevents duplicate work under Gunicorn.
    from app.services.maintenance import start_embedded_scheduler
    start_embedded_scheduler(app)

    return app


_NETWORK_FILESYSTEMS = {
    "nfs", "nfs4", "cifs", "smb", "smb2", "smbfs", "afs", "fuse.sshfs",
    "9p", "glusterfs", "lustre", "ceph", "beegfs", "afpfs", "ncpfs",
}


def _on_network_filesystem(path: str) -> bool:
    """Best-effort detection of a network-mounted filesystem.

    SQLite's WAL journal needs POSIX shared memory, which network filesystems
    do not provide.  Shared hosting such as PythonAnywhere serves /home over
    NFS-like storage, so a WAL database there fails with "unable to open
    database file" / "disk I/O error" on every request.
    """
    try:
        target = Path(path).expanduser().resolve()
    except Exception:
        return False
    candidate = target if target.exists() else target.parent
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    best_point, best_type = "", ""
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        point, fstype = parts[1].replace("\\040", " "), parts[2]
        try:
            mount_path = Path(point)
        except Exception:
            continue
        if candidate == mount_path or mount_path in candidate.parents:
            if len(point) >= len(best_point):
                best_point, best_type = point, fstype
    return best_type.lower() in _NETWORK_FILESYSTEMS


def _retire_stale_live_health_snapshot(snapshot_path: Path) -> None:
    """Drop a health snapshot that still describes the retired live database."""
    if not snapshot_path.exists():
        return
    try:
        import json
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception:
        snapshot_path.unlink(missing_ok=True)
        return
    db_name = Path(str(payload.get("db_path") or "")).name
    if db_name in {"ahmed_cement.db", "ahmed_cement_v44.db"}:
        snapshot_path.unlink(missing_ok=True)


def _resolve_sqlite_journal_mode(db_path: str) -> str:
    """Pick a journal mode that actually works on this host.

    Override explicitly with SQLITE_JOURNAL_MODE=WAL|DELETE|TRUNCATE.
    """
    configured = (os.environ.get("SQLITE_JOURNAL_MODE") or "").strip().upper()
    allowed = {"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}
    if configured in allowed:
        return configured
    # PythonAnywhere exports these markers in the web-app environment.
    on_pythonanywhere = any(
        key in os.environ
        for key in ("PYTHONANYWHERE_DOMAIN", "PYTHONANYWHERE_SITE")
    )
    if on_pythonanywhere or _on_network_filesystem(db_path):
        return "DELETE"
    return "WAL"


def _warm_template_cache(app: Flask) -> None:
    """Pre-compile templates so the first page hit after boot is already warm.

    Only compiles (``jinja_env.get_template``); it never renders, so no request
    context or context-processor data is needed.
    """
    try:
        templates_root = Path(app.template_folder)
        count = 0
        for path in templates_root.rglob("*.html"):
            rel = path.relative_to(templates_root).as_posix()
            try:
                app.jinja_env.get_template(rel)
                count += 1
            except Exception:
                # A broken/optional template must not block startup.
                continue
        logging.getLogger(__name__).info("Warmed Jinja cache for %d templates", count)
    except Exception:
        logging.getLogger(__name__).warning("Template warm-up skipped", exc_info=True)


def _alias_unprefixed_endpoints(app: Flask) -> None:
    """Keep legacy url_for('login') / templates working after blueprint split."""
    existing = set(app.view_functions)
    extras = []
    for rule in list(app.url_map.iter_rules()):
        if "." not in rule.endpoint:
            continue
        short = rule.endpoint.split(".", 1)[1]
        if short in existing:
            continue
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            continue
        app.view_functions[short] = view
        extras.append((rule.rule, short, view, sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})))
        existing.add(short)
    for rule, short, view, methods in extras:
        try:
            app.add_url_rule(rule, endpoint=short, view_func=view, methods=methods or None)
        except Exception:
            pass


def _configure_logging(app: Flask) -> None:
    """Configure console output and a bounded technical diagnostic log."""
    root = logging.getLogger()
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s]: %(message)s")
    if not any(getattr(handler, "_ams_console", False) for handler in root.handlers):
        console = logging.StreamHandler()
        console._ams_console = True
        console.setLevel(logging.INFO)
        console.setFormatter(formatter)
        root.addHandler(console)

    log_dir = Path(app.instance_path) / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = (log_dir / "errorlog.txt").resolve()
        existing = any(
            isinstance(handler, RotatingFileHandler)
            and Path(getattr(handler, "baseFilename", "")).resolve() == log_path
            for handler in root.handlers
        )
        if not existing:
            rotating = RotatingFileHandler(
                log_path,
                maxBytes=int(os.environ.get("ERROR_LOG_MAX_BYTES", str(2 * 1024 * 1024))),
                backupCount=int(os.environ.get("ERROR_LOG_BACKUP_COUNT", "3")),
                encoding="utf-8",
            )
            rotating.setLevel(logging.WARNING)
            rotating.setFormatter(formatter)
            root.addHandler(rotating)
    except OSError:
        # A read-only log directory must not prevent the application starting.
        root.exception("Unable to configure rotating file logging")
    root.setLevel(logging.INFO)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
