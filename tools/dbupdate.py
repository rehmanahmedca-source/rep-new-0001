#!/usr/bin/env python3
"""``dbupdate`` — the one command surface for modules, schema and data updates.

Safety classes are explicit, and nothing destructive is ever the default:

=========================  =========================================
Class                      Commands
=========================  =========================================
**CHECK ONLY** (read-only) ``check`` (default), ``discover``,
                           ``audit-schema``, ``validate-migrations``,
                           ``status``, ``history``, ``integrity``
**PREVIEW**                ``plan``  (add ``--rehearse`` to apply the
                           pending revisions against a throw-away copy)
**APPLY** (dev / CI)       ``apply``, ``full-update --apply``
**PRODUCTION DEPLOY**      ``full-update --apply --yes`` with
                           ``AMS_ENV=production`` (policy ``guarded``);
                           refuses to run without a verified backup
**TEST**                   ``tests`` (declared module suites),
                           ``tests --all`` (full regression suite)
=========================  =========================================

Examples::

    python tools/dbupdate.py                        # what is pending? exit 0/1
    python tools/dbupdate.py discover --json
    python tools/dbupdate.py audit-schema
    python tools/dbupdate.py plan --rehearse
    python tools/dbupdate.py apply                  # backup, migrate, verify
    python tools/dbupdate.py full-update --apply --yes
    AMS_ENV=production python tools/dbupdate.py check   # refuse unsafe states

Exit codes: 0 ok / ready, 1 pending work or warnings, 2 failure needing
attention, 3 the command itself could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXIT_OK = 0
EXIT_PENDING = 1
EXIT_FAILED = 2
EXIT_ERROR = 3


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------

def _emit(payload: dict, args, *, human) -> int:
    if getattr(args, "json", False):
        print(json.dumps(_redact(payload), indent=2, default=str))
    else:
        human(payload)
    return _exit_code(payload)


def _redact(value):
    from app.services.dbupdate.reports import redact

    return redact(value)


def _exit_code(payload: dict) -> int:
    status = str(payload.get("final_status") or payload.get("status") or "").upper()
    if "REQUIRES ATTENTION" in status or status in {"FAIL", "ERROR"}:
        return EXIT_FAILED
    if "PENDING" in status or status in {"MIGRATION_REQUIRED", "SCHEMA_DRIFT", "WARN", "ATTENTION"}:
        return EXIT_PENDING
    return EXIT_OK


def _rule(title: str = "") -> None:
    width = 78
    if title:
        print("=" * width)
        print(f"  {title}")
        print("=" * width)
    else:
        print("-" * width)


def _status_line(label: str, value) -> None:
    print(f"  {str(label):<32}{value}")


def print_pipeline(report: dict) -> None:
    from app.services.dbupdate.reports import render_health_report

    print(render_health_report(report))
    steps = report.get("pipeline") or []
    if steps:
        print("PIPELINE STEPS")
        for step in steps:
            icon = {"PASS": "ok  ", "WARN": "warn", "FAIL": "FAIL", "SKIPPED": "skip"}.get(step["status"], "-   ")
            print(f"  [{icon:<4}] {step['label']:<52} {step.get('duration_ms', 0):>5} ms  {step.get('detail', '')[:64]}")
    for blocker in report.get("blockers") or []:
        print()
        print(f"BLOCKER [{blocker.get('step')}]")
        for key, label in (
            ("what", "what failed"),
            ("why", "why"),
            ("where", "where"),
            ("module", "affected module"),
            ("database_object", "database object"),
            ("data_risk", "data risk"),
            ("next_action", "recommended fix"),
        ):
            if blocker.get(key):
                print(f"    {label:<18}: {blocker[key]}")
    files = report.get("report_files") or {}
    if files:
        print()
        print("REPORT FILES")
        for key, path in files.items():
            print(f"  {key:<16}: {path}")


# ---------------------------------------------------------------------------
# app construction
# ---------------------------------------------------------------------------

def _build_app(args):
    """Apply CLI overrides to the environment *before* the factory runs."""
    if getattr(args, "db", None):
        os.environ["APP_DB_PATH"] = str(Path(args.db).expanduser().resolve())
    if getattr(args, "policy", None):
        os.environ["AMS_UPDATE_POLICY"] = args.policy
    if getattr(args, "env", None):
        os.environ["AMS_ENV"] = args.env
    if getattr(args, "report_dir", None):
        os.environ["UPDATE_REPORT_DIR"] = str(Path(args.report_dir).expanduser().resolve())
    from app import create_app

    app = create_app(
        {
            # A CLI run must never start the backup scheduler or fight the web
            # process for the lock.
            "BACKUP_EMBEDDED_SCHEDULER": False,
            "AMS_CLI_RUN": True,
        }
    )
    return app


def _registry(app):
    registry = app.extensions.get("ams_modules")
    if registry is None:
        from app.services.module_system import registry_for

        registry = registry_for(app, app.config.get("AMS_MODULE_ROOT") or str(ROOT / "blueprints"))
    return registry


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_check(app, args) -> int:
    from app.services.dbupdate import run_update
    from app.services.dbupdate.runner import MODE_CHECK, MODE_PLAN

    mode = MODE_PLAN if getattr(args, "rehearse", False) else MODE_CHECK
    report = run_update(app, mode=mode, trigger=f"cli:{args.command}", with_tests=getattr(args, "with_tests", False))
    if getattr(args, "rehearse", False):
        from app.services.dbupdate.runner import preview_on_copy

        report["rehearsal"] = preview_on_copy(app, registry=_registry(app))
    return _emit(report, args, human=print_pipeline)


def cmd_discover(app, args) -> int:
    registry = _registry(app)
    payload = registry.report(app)
    if args.json:
        print(json.dumps(_redact(payload), indent=2, default=str))
        return _exit_code({"status": "FAIL" if payload["failed"] else "OK"})
    _rule("MODULE DISCOVERY")
    _status_line("module root", payload["module_root"])
    _status_line("contract", f"schema_api {payload['contract_schema_api']}")
    _status_line("discovered", payload["discovered"])
    _status_line("registered", payload["registered"])
    print()
    for status, module_ids in payload["statuses"].items():
        if module_ids:
            _status_line(status, ", ".join(module_ids))
    if payload["failed"]:
        print()
        print("FAILURES (each module stays inactive until fixed)")
        for entry in payload["failed"]:
            print(f"  {entry['module']}  [{entry['status']}]")
            for problem in entry["problems"]:
                where = problem.get("field") or "manifest"
                print(f"      {problem['code']} @ {where}: {problem['message']}")
                if problem.get("hint"):
                    print(f"      fix: {problem['hint']}")
    if payload["orphans"]:
        print()
        print("REJECTED BEFORE VALIDATION")
        for entry in payload["orphans"]:
            print(f"  {entry['path']}: {entry['reason']}")
    return _exit_code({"status": "FAIL" if payload["failed"] or payload["orphans"] else "OK"})


def cmd_audit_schema(app, args) -> int:
    from app.services.dbupdate import schema_audit
    from app.services.dbupdate.runner import _audit

    with app.app_context():
        payload = _audit(app, _registry(app))
    if args.json:
        print(json.dumps(_redact(payload), indent=2, default=str))
        return _exit_code({"status": payload["status"]})
    _rule("SCHEMA AUDIT  (models vs ledger vs database)")
    _status_line("database", Path(str(app.config.get("APP_DB_PATH") or "")).name)
    _status_line("status", payload["status"])
    _status_line("current version", payload["current_schema_version"])
    _status_line("expected version", payload["expected_schema_version"])
    _status_line("pending migrations", payload.get("known_revisions", 0) - payload.get("applied_revisions", 0))
    _status_line("tables", f"{payload['tables_present']} present / {payload['tables_expected']} expected")
    _status_line("counts", json.dumps(payload["counts"]))
    issues = payload["issues"]
    if not issues:
        print("\n  No differences: the database matches what the models declare.")
    for severity in ("ADDITIVE", "MANUAL", "DESTRUCTIVE"):
        subset = [item for item in issues if item["severity"] == severity]
        if not subset:
            continue
        print(f"\n  {severity} ({len(subset)})")
        for item in subset[: args.limit]:
            print(f"    - {item['kind']:<24} {item['object']}  [{item['owner']}]")
            print(f"      {item['detail']}")
            if args.verbose and item.get("fix"):
                print(f"      fix: {item['fix']}")
        if len(subset) > args.limit:
            print(f"    … {len(subset) - args.limit} more (raise with --limit)")
    return _exit_code({"status": "OK" if not [i for i in issues if i["severity"] != "OK"] else "SCHEMA_DRIFT"})


def cmd_validate_migrations(app, args) -> int:
    from app.services.dbupdate import ledger
    from app.services.dbupdate import migrations as MIG
    from app.services.dbupdate.policy import resolve

    registry = _registry(app)
    revisions = MIG.collect(app, registry=registry)
    with app.app_context():
        ledger.ensure_ledger(allow_create=False)
        applied = ledger.applied_revisions()
    revisions = MIG.validate(revisions, policy=resolve(app), applied=applied)
    plan = MIG.plan(revisions)
    payload = {"plan": plan, "revisions": [revision.as_dict() for revision in revisions], "status": "FAIL" if plan["requires_attention"] else "OK"}
    if args.json:
        print(json.dumps(_redact(payload), indent=2, default=str))
        return _exit_code(payload)
    _rule("MIGRATION VALIDATION")
    _status_line("revisions", plan["total_revisions"])
    _status_line("applied", plan["applied"])
    _status_line("pending", plan["pending"])
    _status_line("requires attention", plan["requires_attention"])
    for revision in revisions:
        mark = {"APPLIED": "applied ", "PENDING": "pending ", "MODIFIED": "MODIFIED", "REQUIRES_ATTENTION": "BLOCKED "}[revision.status]
        print(f"\n  [{mark}] {revision.global_revision}  {revision.title}  ({revision.kind})")
        print(f"           {revision.path}")
        for problem in revision.problems:
            print(f"           ! {problem['code']}: {problem['message']}")
            if problem.get("hint"):
                print(f"             fix: {problem['hint']}")
    return _exit_code(payload)


def cmd_plan(app, args) -> int:
    return cmd_check(app, args)


def cmd_backup(app, args) -> int:
    from app.services.maintenance import create_backup

    with app.app_context():
        result = create_backup(app, reason=f"cli-backup:{args.reason}")
    payload = {"status": "OK", "backup": result}
    if args.json:
        print(json.dumps(_redact(payload), indent=2, default=str))
    else:
        _rule("DATABASE BACKUP")
        _status_line("path", result.get("path"))
        _status_line("size", f"{int(result.get('size_bytes') or 0) / 1_048_576:.2f} MiB")
        _status_line("validated", result.get("validated", True))
        _status_line("pruned", ", ".join(result.get("pruned") or []) or "none")
    return EXIT_OK


def cmd_apply(app, args) -> int:
    from app.services.dbupdate import run_update
    from app.services.dbupdate.policy import ENV_PRODUCTION, resolve
    from app.services.dbupdate.runner import MODE_APPLY

    policy = resolve(app)
    pending = _pending_count(app)
    if args.dry_run:
        print("DRY RUN — nothing was applied. Use 'plan' for the full preview.")
        return _exit_code({"status": "PENDING" if pending else "OK"})
    if policy.is_production and not args.yes:
        print(
            "REFUSED: this is a production environment. Re-run with --yes to confirm\n"
            "you have read `plan` output; the pipeline will still take and verify a\n"
            "backup first, and will stop if verification fails.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    if pending and policy.policy in ("audit", "manual") and not args.force:
        print(
            f"REFUSED: policy '{policy.policy}' never writes schema. Re-run with --force\n"
            "to override (it still backs up first), or use 'apply --policy guarded'.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    report = run_update(
        app,
        mode=MODE_APPLY,
        trigger=f"cli:apply:{args.only or 'all'}",
        registry=_registry(app),
        with_tests=args.with_tests,
        only=[name.strip() for name in args.only.split(",")] if args.only else None,
    )
    if report.get("migrations", {}).get("failed"):
        report.setdefault("blockers", []).append(
            {
                "step": "apply_migrations",
                "what": "one or more revisions failed",
                "next_action": "the failed revision was rolled back; fix it and re-run",
            }
        )
    return _emit(report, args, human=print_pipeline)


def _pending_count(app) -> int:
    from app.services.dbupdate import ledger
    from app.services.dbupdate import migrations as MIG

    registry = _registry(app)
    revisions = MIG.collect(app, registry=registry)
    with app.app_context():
        ledger.ensure_ledger(allow_create=False)
        applied = ledger.applied_revisions()
    return len([r for r in revisions if r.global_revision not in applied])


def cmd_integrity(app, args) -> int:
    from app.services.dbupdate import integrity

    with app.app_context():
        payload = integrity.run_integrity(app, deep=not args.quick)
    if args.json:
        print(json.dumps(_redact(payload), indent=2, default=str))
    else:
        _rule("DATA INTEGRITY")
        _status_line("verdict", payload["status"])
        for name, layer in (payload.get("layers") or {}).items():
            detail = layer.get("detail") if isinstance(layer, dict) else ""
            _status_line(name, f"{layer.get('status', '')}  {str(detail)[:48]}")
    return _exit_code({"status": "OK" if payload["status"] == "PASS" else "FAIL"})


def cmd_tests(app, args) -> int:
    import subprocess

    from app.services.module_system.health import run_module_health

    registry = _registry(app)
    with app.app_context():
        health = run_module_health(app, registry)
    paths = sorted({str(ROOT / rel) for spec in registry.specs.values() if spec.enabled for rel in spec.test_paths})
    if args.all or not paths:
        paths = [str(ROOT / "tests")]
    command = [sys.executable, "-m", "pytest", "-q", *paths]
    if not args.json:
        _rule("TESTS")
        _status_line("module health", health.get("status"))
        _status_line("command", " ".join(command))
    completed = subprocess.run(command, cwd=str(ROOT))
    payload = {
        "status": "OK" if completed.returncode == 0 else "FAIL",
        "module_health": {
            "status": health.get("status"),
            "failed": health.get("failed"),
            "modules": {
                mid: {"status": item["status"], "failed": item.get("failed")}
                for mid, item in (health.get("modules") or {}).items()
            },
        },
        "pytest": {"command": command, "returncode": completed.returncode},
    }
    if args.json:
        print(json.dumps(_redact(payload), indent=2, default=str))
    return EXIT_OK if completed.returncode == 0 else EXIT_FAILED


def cmd_full_update(app, args) -> int:
    """Everything, in the standard order: discover → audit → validate → back up
    → apply → verify → integrity → tests → report."""
    from app.services.dbupdate import run_update, schema_audit
    from app.services.dbupdate.runner import MODE_APPLY, MODE_PLAN

    registry = _registry(app)
    if not registry.ready():
        print(
            "MODULE REGISTRY IS NOT CLEAN — failing modules must be fixed first.\n"
            "Run 'dbupdate.py discover' for the reasons; the update was not started.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    mode = MODE_APPLY if args.apply else MODE_PLAN
    report = run_update(
        app,
        mode=mode,
        trigger="cli:full-update",
        registry=registry,
        with_tests=args.with_tests,
    )
    if args.rehearse and mode != MODE_APPLY:
        from app.services.dbupdate.runner import preview_on_copy

        report["rehearsal"] = preview_on_copy(app, registry=registry)
    if args.docs:
        from app.services.dbupdate import generate_docs

        report["docs"] = generate_docs(app, registry=registry)
    if mode == MODE_APPLY and report.get("final_status") == "READY":
        report["deploy_note"] = "update verified; safe to reload the web process"
    return _emit(report, args, human=print_pipeline)


def cmd_status(app, args) -> int:
    from app.services.dbupdate import ledger
    from app.services.dbupdate import schema_audit
    from app.services.dbupdate.installs import list_installs
    from app.services.dbupdate.policy import resolve
    from app.services.dbupdate.runner import _audit

    policy = resolve(app)
    with app.app_context():
        ledger.ensure_ledger(allow_create=False)
        version = ledger.read_schema_version()
        runs = ledger.recent_runs(limit=5)
        history = ledger.history(limit=5)
        installs = list_installs()
        audit = _audit(app, _registry(app))
        quick = schema_audit.quick_summary()
    payload = {
        "status": audit["status"],
        "environment": policy.environment,
        "policy": policy.policy,
        "policy_detail": policy.as_dict(),
        "database": str(app.config.get("APP_DB_PATH")),
        "schema_version": version,
        "expected_schema_version": audit.get("expected_schema_version"),
        "audit": {
            "status": audit["status"],
            "counts": audit["counts"],
            "issue_count": len(audit["issues"]),
        },
        "database_summary": quick,
        "modules": _registry(app).statuses(),
        "installs": installs,
        "recent_update_runs": runs,
        "recent_revisions": history,
    }
    if args.json:
        print(json.dumps(_redact(payload), indent=2, default=str))
        return _exit_code(payload)
    _rule(f"AMS UPDATE STATUS   [{policy.environment} / {policy.policy}]")
    _status_line("database", Path(str(app.config.get("APP_DB_PATH") or "")).name)
    _status_line("schema version", f"{version} (expected {audit.get('expected_schema_version')})")
    _status_line("audit status", audit["status"])
    _status_line("additive / manual / risky", f"{audit['counts']['additive']} / {audit['counts']['manual']} / {audit['counts']['destructive']}")
    _status_line("tables / indexes", f"{quick['tables']} / {quick['indexes']}")
    _status_line("integrity", f"{quick['integrity_check']}, FK violations {quick['foreign_key_violations']}")
    print("\n  MODULES")
    for status, module_ids in payload["modules"].items():
        if module_ids:
            _status_line(f"  {status}", ", ".join(module_ids))
    if installs:
        print("\n  REGISTERED INSTANCES")
        for item in installs:
            _status_line(f"  {item['module_id']}", f"v{item['version']} [{item['status']}] health={item['last_health'] or '-'}")
    if runs:
        print("\n  RECENT UPDATE RUNS")
        for row in runs:
            _status_line(
                f"  {row.get('started_at', '')[:19]}",
                f"{row.get('mode')}/{row.get('final_status')} applied={row.get('migrations_applied')} failed={row.get('migrations_failed')}",
            )
    return _exit_code({"status": audit["status"]})


def cmd_history(app, args) -> int:
    from app.services.dbupdate import ledger

    with app.app_context():
        ledger.ensure_ledger(allow_create=False)
        payload = {
            "status": "OK",
            "revisions": ledger.history(limit=args.limit),
            "runs": ledger.recent_runs(limit=min(args.limit, 20)),
        }
    if args.json:
        print(json.dumps(_redact(payload), indent=2, default=str))
        return EXIT_OK
    _rule("UPDATE HISTORY")
    if not payload["revisions"]:
        print("  No revisions recorded yet.")
    for row in payload["revisions"]:
        _status_line(
            row.get("revision") or "?",
            f"{row.get('status')} {row.get('kind')} module={row.get('module_id')} "
            f"at={row.get('completed_at') or row.get('attempted_at')} backup={Path(str(row.get('backup_path') or '')).name or '-'}",
        )
        if row.get("error"):
            print(f"      error: {row['error']}")
    if payload["runs"]:
        print("\n  PIPELINE RUNS")
        for row in payload["runs"]:
            _status_line(
                row.get("run_key") or "?",
                f"{row.get('mode')} → {row.get('final_status')} "
                f"(applied {row.get('migrations_applied')}, failed {row.get('migrations_failed')})",
            )
    return EXIT_OK


def cmd_docs(app, args) -> int:
    from app.services.dbupdate import generate_docs

    result = generate_docs(app, registry=_registry(app), write=not args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _rule("MODULE DOCUMENTATION")
        _status_line("path", result["path"])
        _status_line("changed", result["changed"])
        _status_line("modules", result["modules"])
        _status_line("written", result["written"])
    return EXIT_OK


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

_GLOBAL_DEFAULTS = {
    "db": None,
    "policy": None,
    "env": None,
    "report_dir": None,
    "json": False,
    "verbose": False,
}


def _common_parser() -> argparse.ArgumentParser:
    """Flags accepted both before and after the sub-command.

    ``argparse`` would otherwise let ``dbupdate.py check --json`` fail while
    ``dbupdate.py --json check`` works, and the SUPPRESS defaults keep the
    sub-parser from overwriting a value given on the global side.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help="path to the SQLite database (defaults to APP_DB_PATH/instance)")
    common.add_argument("--policy", choices=["auto", "guarded", "audit", "manual"], default=argparse.SUPPRESS, help="override the update policy for this run")
    common.add_argument("--env", choices=["development", "test", "production"], default=argparse.SUPPRESS, help="override the detected environment")
    common.add_argument("--report-dir", default=argparse.SUPPRESS, help="where to write instance/logs/* reports")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="more detail")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="dbupdate",
        description="AMS module + schema + data auto-upgrade control",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "CHECK ONLY: check discover audit-schema validate-migrations status history integrity\n"
            "PREVIEW   : plan [--rehearse]\n"
            "APPLY     : apply / full-update --apply   (production also needs --yes)\n"
            "TEST      : tests [--all]\n"
            "Nothing destructive is a default, and 'check' never writes to the database.\n"
        ),
        parents=[common],
    )

    sub = parser.add_subparsers(dest="command")
    sub.required = True

    add = lambda name, **kw: sub.add_parser(name, parents=[common], **kw)
    check = add("check", help="read-only: what would happen (DEFAULT, no writes)")
    check.add_argument("--rehearse", action="store_true", help="also apply pending revisions to a throw-away copy")
    check.add_argument("--with-tests", action="store_true", help="also run the declared module test suites")

    add("discover", help="read-only: module discovery + validation report")

    audit = add("audit-schema", help="read-only: models vs ledger vs database")
    audit.add_argument("--limit", type=int, default=25, help="issues shown per severity (default %(default)s)")

    add("validate-migrations", help="read-only: lint, checksum and dependency check")

    plan = add("plan", help="preview: pending revisions + what the pipeline will do")
    plan.add_argument("--rehearse", action="store_true", help="apply them against a copy of the database")

    backup = add("backup", help="create + validate a backup (retention applies)")
    backup.add_argument("--reason", default="cli")

    apply_ = add("apply", help="APPLY: backup, run pending revisions, verify, report")
    apply_.add_argument("--yes", action="store_true", help="required acknowledgement in production")
    apply_.add_argument("--force", action="store_true", help="apply even when the policy is audit/manual")
    apply_.add_argument("--with-tests", action="store_true", help="run the affected modules' tests as part of the update")
    apply_.add_argument("--only", help="comma-separated legacy ensure-steps to run instead of the whole chain")
    apply_.add_argument("--dry-run", action="store_true", help="print the pending count and exit")

    integrity = add("integrity", help="read-only: SQLite + preflight + business consistency")
    integrity.add_argument("--quick", action="store_true", help="SQLite-level checks only")

    tests = add("tests", help="TEST: run module-declared suites (or --all for the full suite)")
    tests.add_argument("--all", action="store_true", help="run the whole regression suite")

    full = add("full-update", help="discover → audit → validate → backup → apply → verify → report")
    full.add_argument("--apply", action="store_true", help="write changes (default is preview)")
    full.add_argument("--yes", action="store_true", help="acknowledge a production write")
    full.add_argument("--with-tests", action="store_true")
    full.add_argument("--rehearse", action="store_true", help="preview on a copy of the database")
    full.add_argument("--docs", action="store_true", help="regenerate docs/MODULE_REGISTRY.md")

    add("status", help="read-only: environment, policy, schema and module summary")

    history = add("history", help="read-only: what was applied, when, and with what backup")
    history.add_argument("--limit", type=int, default=25)

    docs = add("docs", help="regenerate docs/MODULE_REGISTRY.md from live metadata")
    docs.add_argument("--dry-run", action="store_true", help="report what would change")
    return parser


HANDLERS = {
    "check": cmd_check,
    "plan": cmd_plan,
    "discover": cmd_discover,
    "audit-schema": cmd_audit_schema,
    "validate-migrations": cmd_validate_migrations,
    "backup": cmd_backup,
    "apply": cmd_apply,
    "integrity": cmd_integrity,
    "tests": cmd_tests,
    "full-update": cmd_full_update,
    "status": cmd_status,
    "history": cmd_history,
    "docs": cmd_docs,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Bare invocation means the safest possible command.
    known = set(HANDLERS)
    if not argv or (argv[0] not in known and argv[0].startswith("-") and not any(a in known for a in argv)):
        argv = ["check", *argv]
    parser = build_parser()
    args = parser.parse_args(argv)
    for key, value in _GLOBAL_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    try:
        app = _build_app(args)
    except Exception as exc:
        print(f"could not start the application: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    handler = HANDLERS[args.command]
    try:
        return handler(app, args)
    except SystemExit:
        raise
    except Exception as exc:
        import traceback

        print(f"command '{args.command}' raised {type(exc).__name__}: {exc}", file=sys.stderr)
        if os.environ.get("AMS_DEBUG_TRACEBACK"):
            traceback.print_exc()
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
