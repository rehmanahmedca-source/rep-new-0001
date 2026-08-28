"""Plant & Equipment Register — shared blueprint + helpers.

A deliberately small module that follows the AMS module contract
(``docs/MODULE_CONTRACT.md``): manifest, models, migration, navigation,
permissions, health check and tests all declared in ``module.toml``.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint
from flask_login import current_user

PK_TZ = ZoneInfo("Asia/Karachi")

#: Legacy echo of module.toml for tools that still read MODULE_CONFIG.
#: The manifest is authoritative; the registry never imports this module to
#: find out whether it is enabled.
MODULE_CONFIG = {
    "id": "plant_registry",
    "name": "Plant & Equipment Register",
    "description": "Register of site plant and equipment with status and location.",
    "url_prefix": "/plant_registry",
    "enabled": True,
    "version": "1.0.0",
    "requires_login": True,
    "allowed_roles": ["admin", "manager", "user"],
}

plant_registry_bp = Blueprint(
    "plant_registry",
    __name__,
    template_folder="templates",
    static_folder="static",
)

STATUS_CHOICES = ("available", "on-site", "under-maintenance", "retired")
STATUS_LABELS = {
    "available": "Available",
    "on-site": "On site",
    "under-maintenance": "Under maintenance",
    "retired": "Retired",
}


def pk_now() -> datetime:
    return datetime.now(PK_TZ).replace(tzinfo=None)


def actor() -> str:
    return str(getattr(current_user, "username", None) or "system")


def normalise_code(value: str) -> str:
    """Stable upper-case code from a free-text site/yard name."""
    text = (value or "").strip().upper()
    kept = "".join(ch if ch.isalnum() else " " for ch in text)
    parts = [part for part in kept.split() if part]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:12]
    return ("".join(part[0] for part in parts) + parts[0][:6])[:12]
