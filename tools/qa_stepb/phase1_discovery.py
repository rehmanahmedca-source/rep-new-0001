"""STEP B / Phase 1: login + recursive application discovery.

Walks every GET rule in the real url_map.  Parameterised rules are filled from
IDs that actually exist in the database, so we exercise real detail pages
rather than 404 stubs.  Anything that 500s is a bug.
"""
from __future__ import annotations

import re

SKIP_PREFIX = (
    "/static/", "/uploads/",
)
# Routes that legitimately mutate/destroy state or hang on network I/O.
# They are recorded as SKIPPED with a reason rather than silently ignored.
DESTRUCTIVE = {
    "/logout",
    "/generate_dummy_data",
    "/fix_system_issues",
    "/debug/db",
}

ID_HINTS = {
    "id", "client_id", "account_id", "mat_id", "booking_id", "sale_id",
    "payment_id", "bill_id", "history_id", "record_id", "tx_id", "alloc_id",
    "draft_id", "category_id", "trans_id",
}

# Legacy multi-tenant / root endpoints that ``require_root()`` deliberately
# disables in single-store mode (app/services/permissions.py aborts 404 with
# the comment "Keep this guard to avoid exposing legacy endpoints").
# A 404 here is the *correct* answer, so we assert it stays that way.
INTENTIONALLY_DISABLED = {
    "/tenants",
    "/root/recovery",
    "/root/recovery_codes",
    "/root/backup-settings",
    "/import_export/tenant_db_export",
}


def _fill(rule, ids):
    """Substitute concrete values into a parameterised rule, or return None."""
    path = str(rule)
    for name, conv in rule._converters.items():
        kind = conv.__class__.__name__
        # Resolution order matters: a specific parameter name wins, then the
        # entity the route is about, and only then the generic "id" fallback.
        # (Looking up "id" first would send a client id to /supplier_ledger/.)
        val = ids.get(name) if name != "id" else None
        if val in (None, ""):
            val = _by_route(path, name, ids)
        if val in (None, "") and (name in ID_HINTS or kind == "IntegerConverter"):
            val = ids.get("id")
        if val in (None, "") and kind == "PathConverter":
            val = ids.get("bill_no")
        if val in (None, ""):
            return None
        path = re.sub(r"<[^:<>]+:%s>|<%s>" % (re.escape(name), re.escape(name)),
                      str(val), path)
    return None if "<" in path else path


# Route-substring -> which id in the pool actually addresses that entity.
_ENTITY_HINTS = (
    ("supplier_payment", "supplier_payment_id"),
    ("supplier", "supplier_id"),
    ("delivery_person", "delivery_person_id"),
    ("delivery_ledger", "delivery_person_id"),
    ("delivery_rent", "delivery_rent_id"),
    ("pending_bill", "pending_bill_id"),
    ("cash_flow", "cash_flow_entry_id"),
    ("material", "material_id"),
    ("grn", "grn_id"),
    ("booking", "booking_id"),
    ("direct_sale", "sale_id"),
    ("payments/clients", "payment_id"),
    ("payments/suppliers", "supplier_payment_id"),
    ("view_bill_detail", "booking_id"),
    ("edit_bill", "booking_id"),
    ("notification", "pending_bill_id"),
    ("account", "account_id"),
    ("client", "client_id"),
)


def _by_route(path, name, ids):
    low = path.lower()
    for needle, key in _ENTITY_HINTS:
        if needle in low:
            v = ids.get(key)
            if v not in (None, ""):
                return v
    return None


def discover(app, br, rec, ids, label="Phase1-Discovery"):
    """Open every reachable GET page and record the outcome."""
    rules = sorted(
        {str(r): r for r in app.url_map.iter_rules() if "GET" in r.methods}.values(),
        key=str,
    )
    for rule in rules:
        raw = str(rule)
        if raw.startswith(SKIP_PREFIX) or rule.endpoint == "static":
            continue
        if raw in DESTRUCTIVE:
            rec.skip(label, raw, "destructive/side-effecting route - excluded by policy")
            rec.page(f"{label}|{raw}", "SKIPPED", "destructive")
            continue

        path = raw if "<" not in raw else _fill(rule, ids)
        if path is None:
            rec.blocked(label, raw, "no live record id available to instantiate this route")
            rec.page(f"{label}|{raw}", "BLOCKED", "no sample id")
            continue

        try:
            resp = br.get(path)
        except Exception as exc:  # unhandled server exception
            rec.check(label, raw, False, f"raised {type(exc).__name__}: {exc}")
            rec.page(f"{label}|{raw}", "FAILED", f"exception {type(exc).__name__}")
            rec.bug(
                module="Navigation", page=path, severity="High", route=raw,
                steps=f"Login as Admin, GET {path}",
                expected="Page renders (2xx) or redirects",
                actual=f"Unhandled exception {type(exc).__name__}: {exc}",
                evidence=str(exc)[:400],
                consistency_risk="Yes",
            )
            continue

        code = resp.status_code
        rec.bump("pages_opened")
        if raw in INTENTIONALLY_DISABLED:
            # Security assertion: the disabled legacy surface must stay closed.
            ok = code == 404
            rec.check(label, f"{raw} (legacy root-only endpoint) stays disabled", ok,
                      f"HTTP {code} - expected 404")
            rec.page(f"{label}|{raw}", "PASSED" if ok else "FAILED",
                     "intentionally disabled (404)" if ok else f"HTTP {code} - now exposed!")
            if not ok:
                rec.bug(
                    module="Security", page=path, severity="High", route=raw,
                    steps=f"Log in as a non-root admin and open {path}",
                    expected="404 - the legacy multi-tenant surface is disabled",
                    actual=f"HTTP {code} - the endpoint responded",
                    root_cause="require_root() guard missing or bypassed",
                    consistency_risk="Yes",
                )
        elif code >= 500:
            rec.check(label, raw, False, f"HTTP {code}")
            rec.page(f"{label}|{raw}", "FAILED", f"HTTP {code}")
            rec.bug(
                module="Navigation", page=path, severity="High", route=raw,
                steps=f"Login as Admin, GET {path}",
                expected="HTTP 200",
                actual=f"HTTP {code} server error",
                evidence=resp.get_data(as_text=True)[:400],
                consistency_risk="Yes",
            )
        elif code == 404:
            if "<" in raw:
                # Parameterised: the QA dataset simply has no record of this
                # kind, so the route is unproven rather than broken.
                rec.blocked(label, raw,
                            f"HTTP 404 for {path} - no matching record exists in the QA dataset")
                rec.page(f"{label}|{raw}", "BLOCKED", "404 - no sample record")
            else:
                rec.check(label, raw, False, f"HTTP 404 for {path}")
                rec.page(f"{label}|{raw}", "FAILED", "HTTP 404")
                rec.bug(
                    module="Navigation", page=path, severity="Low", route=raw,
                    steps=f"Login as Admin and open {path}",
                    expected="The registered page renders, or redirects with a clear "
                             "'permission denied' message",
                    actual="HTTP 404 - the route is registered but not reachable for an admin",
                    root_cause="Route is gated (root-only / feature-flagged) and answers 404 "
                               "instead of 403, so it is indistinguishable from a dead link",
                    consistency_risk="No",
                )
        else:
            rec.check(label, raw, True, f"HTTP {code}")
            rec.page(f"{label}|{raw}", "PASSED", f"HTTP {code}")
    return rec
