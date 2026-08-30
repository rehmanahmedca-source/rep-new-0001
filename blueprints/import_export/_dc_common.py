"""Data Center page helpers: permission guard, shared context, run views."""
from __future__ import annotations

from flask import abort, redirect, url_for
from flask_login import current_user

from ._common import import_export_bp  # noqa


def dc_guard() -> bool:
    """CSRF'ed mutating routes call this; raises 403 for non-privileged users."""
    if current_user.is_authenticated and (
        current_user.role in {"admin", "root"}
        or bool(getattr(current_user, "can_import_export", False))
    ):
        return True
    abort(403)


def _error(message: str, *, to: str = "dc_restore_page") -> None:
    from flask import flash

    flash(message, "danger")


def back_home() -> str:
    return url_for("import_export.import_export_page")
