"""STEP B deep-QA harness: shared plumbing.

Boots the real Flask app against a throw-away SQLite file, logs in through the
real ``/login`` route, and exposes a CSRF-aware client plus a bug/coverage
recorder.  Nothing here stubs application code - every assertion below is made
against genuine HTTP responses and genuine ORM rows.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ADMIN_USER = "Admin"
ADMIN_PASS = "Admin@fbm12345"


# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------
def build_app(db_path: Path, workdir: Path):
    """Create a real app instance bound to *db_path*.

    Called more than once against the same file on purpose: that is how we
    simulate a genuine browser/server reload in Phase 9.
    """
    os.environ.update(
        APP_DB_PATH=str(db_path),
        DB_HEALTH_SNAPSHOT_PATH=str(workdir / "health_snapshot.json"),
        ALLOW_EMPTY_DB="1",
        BACKUP_EMBEDDED_SCHEDULER="0",
        AMS_SCHEMA_VERSION="v44",
        SQLITE_JOURNAL_MODE="DELETE",
        DEFAULT_ADMIN_USER=ADMIN_USER,
        DEFAULT_ADMIN_PASSWORD=ADMIN_PASS,
    )
    for mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        del sys.modules[mod]
    app_pkg = importlib.import_module("app")
    return app_pkg.create_app()


class Browser:
    """CSRF-aware wrapper that behaves like a logged-in human's browser."""

    def __init__(self, app):
        self.app = app
        self.raw = app.test_client()

    # -- session -----------------------------------------------------------
    def token(self) -> str:
        with self.raw.session_transaction() as sess:
            tok = sess.get("_csrf_token")
        if not tok:
            tok = "qa-stepb-token"
            with self.raw.session_transaction() as sess:
                sess["_csrf_token"] = tok
        return tok

    def login(self, username=ADMIN_USER, password=ADMIN_PASS):
        # Prime the session/CSRF token exactly as loading the login page would.
        self.raw.get("/login")
        return self.raw.post(
            "/login",
            data={
                "username": username,
                "password": password,
                "_csrf_token": self.token(),
            },
            follow_redirects=False,
        )

    # -- verbs -------------------------------------------------------------
    def get(self, path, **kw):
        kw.setdefault("follow_redirects", True)
        return self.raw.get(path, **kw)

    def post(self, path, data=None, **kw):
        kw.setdefault("follow_redirects", True)
        data = dict(data or {})
        data.setdefault("_csrf_token", self.token())
        return self.raw.post(path, data=data, **kw)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------
_TAG = re.compile(r"<[^>]+>")


def flashes(resp) -> str:
    """Extract human-readable flash/alert text from a rendered page."""
    body = resp.get_data(as_text=True)
    msgs = re.findall(r'class="[^"]*alert[^"]*"[^>]*>(.{0,400}?)</div>', body, re.S)
    if not msgs:
        msgs = re.findall(r"alert[^>]*>(.{0,300}?)</div>", body, re.S)
    out = " | ".join(re.sub(r"\s+", " ", _TAG.sub(" ", m)).strip() for m in msgs)
    return out[-600:]


def said(resp, *needles) -> bool:
    body = resp.get_data(as_text=True).lower()
    return any(n.lower() in body for n in needles)


def money(value) -> float:
    """Round the way the ERP presents currency, so comparisons are fair."""
    return round(float(value or 0) + 1e-9, 2)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------
@dataclass
class Bug:
    bug_id: str
    module: str
    page: str
    severity: str
    test_client: str = "-"
    transaction: str = "-"
    steps: str = ""
    expected: str = ""
    actual: str = ""
    route: str = ""
    db_impact: str = "-"
    financial_impact: str = "-"
    inventory_impact: str = "-"
    ledger_impact: str = "-"
    data_loss_risk: str = "No"
    duplication_risk: str = "No"
    consistency_risk: str = "No"
    root_cause: str = "Needs investigation"
    evidence: str = ""
    status: str = "Reproduced"


@dataclass
class Check:
    area: str
    item: str
    status: str  # PASSED / FAILED / BLOCKED / SKIPPED
    detail: str = ""


class Recorder:
    def __init__(self):
        self.bugs: list[Bug] = []
        self.checks: list[Check] = []
        self.pages: dict[str, dict] = {}
        self.counters: dict[str, int] = {}
        self._seq = 0

    # -- coverage ----------------------------------------------------------
    def check(self, area, item, ok, detail="") -> bool:
        self.checks.append(Check(area, item, "PASSED" if ok else "FAILED", detail))
        return bool(ok)

    def skip(self, area, item, detail=""):
        self.checks.append(Check(area, item, "SKIPPED", detail))

    def blocked(self, area, item, detail=""):
        self.checks.append(Check(area, item, "BLOCKED", detail))

    def page(self, route, status, note=""):
        self.pages[route] = {"status": status, "note": note}

    def bump(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    # -- bugs --------------------------------------------------------------
    def bug(self, **kw) -> Bug:
        # The same defect can be hit by more than one probe (e.g. both crawl
        # passes). Report it once.
        fingerprint = (kw.get("module"), kw.get("page"), kw.get("actual"))
        for existing in self.bugs:
            if (existing.module, existing.page, existing.actual) == fingerprint:
                return existing
        self._seq += 1
        kw.setdefault("bug_id", f"BUG-{self._seq:03d}")
        b = Bug(**kw)
        self.bugs.append(b)
        return b

    # -- output ------------------------------------------------------------
    def dump(self, path: Path):
        path.write_text(
            json.dumps(
                {
                    "bugs": [asdict(b) for b in self.bugs],
                    "checks": [asdict(c) for c in self.checks],
                    "pages": self.pages,
                    "counters": self.counters,
                },
                indent=2,
            )
        )

    # -- summaries ---------------------------------------------------------
    @property
    def failed(self):
        return [c for c in self.checks if c.status == "FAILED"]

    @property
    def passed(self):
        return [c for c in self.checks if c.status == "PASSED"]

    def severity_counts(self):
        out = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0}
        for b in self.bugs:
            out[b.severity] = out.get(b.severity, 0) + 1
        return out
