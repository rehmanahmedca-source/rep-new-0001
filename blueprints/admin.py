"""
Admin module for monitoring and managing the application modules.
Provides dashboard to view loaded modules and their configuration.
"""

from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import login_required, current_user
from utils.module_loader import get_modules_info
from datetime import datetime
from zoneinfo import ZoneInfo

# Module configuration
MODULE_CONFIG = {
    'name': 'Admin Module',
    'description': 'System administration and module management',
    'url_prefix': '/admin',
    'enabled': True,
    'requires_login': True,
    'allowed_roles': ['admin']
}

admin_bp = Blueprint('admin', __name__)
PK_TZ = ZoneInfo('Asia/Karachi')


def pk_now():
    return datetime.now(PK_TZ).replace(tzinfo=None)


def is_admin():
    """Check if current user is admin."""
    return current_user.is_authenticated and current_user.role == 'admin'


@admin_bp.before_request
def check_admin():
    """Ensure only admins can access admin routes."""
    if not is_admin():
        from flask import abort
        abort(403)


@admin_bp.route('/')
@login_required
def dashboard():
    """Admin dashboard with system overview."""
    modules = get_modules_info('blueprints')
    module_count = len(modules)
    blueprint_count = sum(len(bps) for _, bps in modules)
    
    context = {
        'module_count': module_count,
        'blueprint_count': blueprint_count,
        'system_time': pk_now(),
        'modules': modules
    }
    
    return render_template('admin_dashboard.html', **context)


@admin_bp.route('/modules')
@login_required
def modules_list():
    """Module registry: every discovered module, its status and why.

    Replaces the old "list of loaded blueprints" view with the validated
    registry the update pipeline uses, while keeping ``modules`` in the
    template context so the previous table still renders if included.
    """
    modules = get_modules_info('blueprints')
    modules_data = [
        {
            "name": module_name,
            "blueprints": blueprints,
            "blueprint_count": len(blueprints),
            "status": "Loaded",
        }
        for module_name, blueprints in modules
    ]
    registry = current_app.extensions.get("ams_modules")
    payload = registry.report(current_app._get_current_object()) if registry is not None else None
    return render_template(
        "admin_module_registry.html",
        modules=modules_data,
        registry=payload,
        health=(registry.health(current_app._get_current_object()) if registry is not None else {}),
    )


@admin_bp.route('/modules/updates')
@login_required
def module_updates():
    """The latest update-pipeline health report, in human-readable form."""
    from app.services.dbupdate import reports as UPDATE_REPORTS

    latest = UPDATE_REPORTS.read_latest(current_app)
    runs = []
    try:
        from app.services.dbupdate import ledger

        ledger.ensure_ledger(allow_create=False)
        runs = ledger.recent_runs(limit=10)
    except Exception:
        runs = []
    return render_template(
        "admin_updates.html",
        report=latest,
        runs=runs,
        history=_revision_history(limit=25),
        policy=_policy_summary(),
    )


def _revision_history(limit: int = 25):
    try:
        from app.services.dbupdate import ledger

        return ledger.history(limit=limit)
    except Exception:
        return []


def _policy_summary():
    try:
        from app.services.dbupdate.policy import resolve

        return resolve(current_app._get_current_object()).as_dict()
    except Exception:
        return {}


@admin_bp.route('/api/modules')
@login_required
def api_get_modules():
    """All modules as JSON — registry statuses plus the legacy shape."""
    modules = get_modules_info('blueprints')
    registry = current_app.extensions.get("ams_modules")
    payload = registry.report(current_app._get_current_object()) if registry is not None else None
    return jsonify(
        {
            "success": True,
            "timestamp": pk_now().isoformat(),
            "total_modules": len(modules),
            "modules": [
                {"name": module_name, "blueprints": blueprints, "status": "active"}
                for module_name, blueprints in modules
            ],
            "registry": payload,
        }
    )


@admin_bp.route('/api/modules/schema')
@login_required
def api_module_schema():
    """Schema audit for the module tables (read-only)."""
    from app.services.dbupdate import schema_audit
    from app.services.dbupdate.runner import _audit

    registry = current_app.extensions.get("ams_modules")
    return jsonify(
        _audit(current_app._get_current_object(), registry)
        if registry is not None
        else {"status": "OK", "issues": [], "module_findings": {}}
    )


@admin_bp.route('/api/modules/updates')
@login_required
def api_module_updates():
    """Latest update-run report + ledger, or a fresh check when ?run=1."""
    from app.services.dbupdate import reports as UPDATE_REPORTS

    if request.args.get("run") == "1":
        from app.services.dbupdate import run_update
        from app.services.dbupdate.runner import MODE_CHECK

        report = run_update(
            current_app._get_current_object(),
            mode=MODE_CHECK,
            trigger="admin",
            registry=current_app.extensions.get("ams_modules"),
        )
    else:
        report = UPDATE_REPORTS.read_latest(current_app) or {}
    return jsonify({"success": bool(report), "report": report, "history": _revision_history(15), "policy": _policy_summary()})


@admin_bp.route('/api/health')
@login_required
def api_health_check():
    """API endpoint for system health check."""
    return jsonify({
        'status': 'healthy',
        'timestamp': pk_now().isoformat(),
        'modules_loaded': len(get_modules_info('blueprints'))
    })


@admin_bp.context_processor
def inject_admin_context():
    """Inject admin-specific data into all admin templates."""
    return {
        'admin_section': True,
        'current_user_role': current_user.role if current_user.is_authenticated else 'guest'
    }
