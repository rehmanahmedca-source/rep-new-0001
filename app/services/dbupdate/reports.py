"""Update health reports: human text, machine JSON, and a bounded history.

Written after every schema/module update (startup, CLI, deploy gate):

``instance/logs/update-health-report.json``   the one report to read
``instance/logs/schema-audit.json``           expected vs actual schema detail
``instance/logs/module-registry-report.json``   discovery/validation outcome
``instance/logs/migration-report.json``         per-revision applied/failed

Every file also gets a timestamped copy under
``instance/logs/update-history/`` — kept to ``UPDATE_REPORT_HISTORY`` entries so
the log directory can never fill the disk.

Secrets are stripped before anything is written: keys that look like
credentials are replaced with ``[redacted]``, and any ``user:pass@`` inside a
database URL is collapsed.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("ams.dbupdate.reports")

LATEST_NAME = "update-health-report.json"
SCHEMA_AUDIT_NAME = "schema-audit.json"
MODULE_REGISTRY_NAME = "module-registry-report.json"
MIGRATION_NAME = "migration-report.json"
MARKDOWN_NAME = "UPDATE_HEALTH_REPORT.md"
ARCHIVE_DIR = "update-history"

_SENSITIVE_KEY_RE = re.compile(r"(password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key)", re.I)
_URL_CREDENTIALS_RE = re.compile(r"([a-z][a-z0-9+.-]*://)([^/@\s]+)@", re.I)
_MAX_HISTORY_DEFAULT = 12


def report_dir(app=None) -> Path:
    if app is not None:
        configured = app.config.get("UPDATE_REPORT_DIR")
        if configured:
            return Path(configured)
        return Path(app.instance_path) / "logs"
    return Path(__file__).resolve().parents[3] / "instance" / "logs"


def redact(value):
    """Recursively remove anything that looks like a credential."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _SENSITIVE_KEY_RE.search(str(key)):
                out[key] = "[redacted]"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _URL_CREDENTIALS_RE.sub(r"\1[redacted]@", value)
    return value


def _dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(redact(payload), indent=2, sort_keys=False, default=str)
    path.write_text(text + "\n", encoding="utf-8")


def read_latest(app=None, *, name: str = LATEST_NAME) -> dict:
    """The most recent report as written, or ``{}`` when there is none.

    Read-only and forgiving by design: an admin page or a CI gate must be able
    to ask "what did the last update do?" even on an instance where the pipeline
    has never run.
    """
    path = report_dir(app) / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "UNREADABLE", "detail": f"{type(exc).__name__}: {exc}", "path": str(path)}


def list_archive(app=None, *, limit: int = 20) -> list[dict]:
    """Summaries of archived runs, newest first.

    Archive files are flat and share a UTC stamp
    (``update-health-report.20260828T080106Z.json``); retention has already
    capped the directory, so this stays bounded no matter how often updates run.
    """
    directory = report_dir(app) / ARCHIVE_DIR
    if not directory.is_dir():
        return []
    prefix = Path(LATEST_NAME).stem + "."
    entries: list[dict] = []
    for path in sorted(directory.glob(f"{prefix}*.json"), reverse=True)[: max(1, int(limit))]:
        stamp = path.name[len(prefix) : -len(".json")]
        entry = {"stamp": stamp, "path": str(path)}
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            entry["run_key"] = report.get("run_key")
            entry["final_status"] = report.get("final_status")
            entry["generated_at"] = report.get("generated_at")
            entry["migrations_applied"] = (report.get("migrations") or {}).get("applied")
        except Exception as exc:
            entry["status"] = "UNREADABLE"
            entry["detail"] = f"{type(exc).__name__}: {exc}"
        entries.append(entry)
    return entries




def _line(label: str, value) -> str:
    return f"{label:<22}{value}"


def render_health_report(report: dict) -> str:
    """The one page an operator reads after an update (Phase 14 format).

    Deliberately flat and scannable: module counts, migration counts, each
    verification result, and the final status.  No secrets, no stack traces.
    """
    report = report or {}
    modules = report.get("modules") or {}
    migrations = report.get("migrations") or {}
    checks = report.get("checks") or {}
    audit = report.get("schema_audit") or {}
    backup = report.get("backup") or {}

    def check(name: str, default: str = "NOT_RUN") -> str:
        entry = checks.get(name)
        if isinstance(entry, dict):
            return str(entry.get("status") or default)
        return str(entry or default)

    lines: list[str] = [
        "=" * 44,
        "       ERP UPDATE HEALTH REPORT",
        "=" * 44,
        _line("GENERATED:", report.get("generated_at", "")),
        _line("RUN:", report.get("run_key", "")),
        _line("TRIGGER:", f"{report.get('trigger', '')}   MODE: {str(report.get('mode', '')).upper()}"),
        _line("ENVIRONMENT:", f"{report.get('environment', '')}   POLICY: {report.get('policy', '')}"),
        _line("APPLICATION VERSION:", report.get("app_version", "")),
        "",
        _line("MODULES DISCOVERED:", modules.get("discovered", 0)),
        _line("MODULES REGISTERED:", modules.get("registered", modules.get("registered_count", 0))),
        _line("MODULES FAILED:", modules.get("failed", 0)),
        _line("MODULES DISABLED:", len(modules.get("disabled") or []) if isinstance(modules.get("disabled"), list) else modules.get("disabled", 0)),
        "",
        _line("DATABASE CURRENT VERSION:", report.get("schema_version_current", audit.get("current_schema_version", 0))),
        _line("DATABASE TARGET VERSION:", report.get("schema_version_expected", audit.get("expected_schema_version", 0))),
        _line("MIGRATIONS DETECTED:", migrations.get("total_revisions", migrations.get("detected", 0))),
        _line("MIGRATIONS PENDING:", migrations.get("pending", 0)),
        _line("MIGRATIONS APPLIED:", migrations.get("applied", 0)),
        _line("MIGRATIONS FAILED:", migrations.get("failed", 0)),
        _line("MIGRATIONS BLOCKED:", migrations.get("blocked", 0)),
        "",
        _line("BACKUP CREATED:", f"{backup.get('status', 'NOT_REQUIRED')} ({backup.get('path', '')})" if backup.get("path") or backup.get("status") else "NOT_REQUIRED"),
        _line("SCHEMA VALIDATION:", check("schema_validation")),
        _line("DATA MIGRATION STATUS:", report.get("data_migration_status", "NO DATA REVISIONS")),
        _line("DATA INTEGRITY STATUS:", check("data_integrity")),
        _line("REGRESSION TESTS:", check("regression")),
        _line("HEALTH CHECKS:", check("module_health")),
        "",
        _line("FINAL STATUS:", report.get("final_status", "UNKNOWN")),
        "=" * 44,
    ]
    blockers = report.get("blockers") or []
    if blockers:
        lines.append("")
        lines.append(f"{len(blockers)} ITEM(S) REQUIRE ATTENTION")
        for blocker in blockers:
            lines.append(
                "  - [{}] {}".format(
                    blocker.get("step", "?"),
                    blocker.get("what") or blocker.get("detail") or "failed",
                )
            )
            if blocker.get("next_action"):
                lines.append(f"      next: {blocker['next_action']}")
    notes = report.get("notes") or []
    if notes:
        lines.append("")
        lines.append("NOTES")
        lines.extend(f"  - {note}" for note in notes)
    lines.append("")
    lines.append(
        "Machine-readable: {}".format(
            (report.get("report_files") or {}).get("latest") or "instance/logs/update-health-report.json"
        )
    )
    return "\n".join(lines)


def write(app=None, report: dict | None = None, *, archive: bool = True) -> dict:
    """Persist the report set; returns ``{status, files, directory}``.

    ``archive=False`` is used by check-only runs: the latest report is refreshed,
    but the bounded history is not filled with entries nobody acted on.
    """
    report = report if isinstance(report, dict) else {}
    directory = report_dir(app)
    history_limit = _MAX_HISTORY_DEFAULT
    if app is not None:
        try:
            history_limit = int(app.config.get("UPDATE_REPORT_HISTORY", _MAX_HISTORY_DEFAULT) or _MAX_HISTORY_DEFAULT)
        except (TypeError, ValueError):
            history_limit = _MAX_HISTORY_DEFAULT
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = report.get("full_report") or report

    written: dict[str, str] = {}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _dump(directory / LATEST_NAME, payload)
        written["latest"] = str(directory / LATEST_NAME)
        if report.get("schema_audit"):
            _dump(directory / SCHEMA_AUDIT_NAME, report["schema_audit"])
            written["schema_audit"] = str(directory / SCHEMA_AUDIT_NAME)
        if report.get("modules") is not None or report.get("module_registry"):
            _dump(directory / MODULE_REGISTRY_NAME, report.get("module_registry") or report.get("modules"))
            written["module_registry"] = str(directory / MODULE_REGISTRY_NAME)
        if report.get("migrations") is not None:
            _dump(directory / MIGRATION_NAME, report.get("migration_report") or report.get("migrations"))
            written["migrations"] = str(directory / MIGRATION_NAME)
        (directory / MARKDOWN_NAME).write_text(render_health_report(payload) + "\n", encoding="utf-8")
        written["markdown"] = str(directory / MARKDOWN_NAME)
    except OSError:
        LOG.exception("update reports could not be written to %s", directory)
        return {"status": "SKIPPED", "detail": f"unwritable report directory: {directory}", "files": written}

    if archive:
        archive_dir = directory / ARCHIVE_DIR
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            for name in (LATEST_NAME, SCHEMA_AUDIT_NAME, MODULE_REGISTRY_NAME, MIGRATION_NAME, MARKDOWN_NAME):
                source = directory / name
                if source.is_file():
                    shutil.copy2(source, archive_dir / f"{source.stem}.{stamp}{source.suffix}")
            _prune(archive_dir, keep=max(1, history_limit))
            written["archive_dir"] = str(archive_dir)
        except OSError:
            LOG.warning("update report archive could not be written", exc_info=True)
    report.setdefault("report_files", written)
    return {"status": "PASS", "files": written, "directory": str(directory)}


def _prune(archive_dir: Path, *, keep: int) -> None:
    """Keep only the newest *keep* runs (grouped by their shared stamp).

    Report retention is a disk-space guarantee, not a courtesy: an instance that
    updates on every boot must not accumulate an unbounded history.
    """
    stamps: dict[str, list[Path]] = {}
    for path in sorted(archive_dir.iterdir()):
        if not path.is_file():
            continue
        parts = path.name.split(".")
        if len(parts) >= 3:
            stamps.setdefault(parts[1], []).append(path)
    ordered = sorted(stamps.items(), key=lambda item: item[0], reverse=True)
    for _stamp, paths in ordered[max(1, keep):]:
        for path in paths:
            try:
                path.unlink()
            except OSError:
                LOG.warning("archived report could not be removed: %s", path)
