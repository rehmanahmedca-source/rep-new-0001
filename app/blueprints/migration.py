from __future__ import annotations
import io, json
from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from openpyxl import Workbook
from models import MigrationRun, MigrationRow
from app.services.legacy_migration import TEMPLATES, template_workbook, validate_upload, import_run, analyze_upload, rerun_prepare

bp=Blueprint('legacy_migration',__name__,url_prefix='/legacy-migration')
def allowed(): return current_user.role in {'admin','root'} or bool(getattr(current_user,'can_import_export',False))
def guard():
    if not allowed(): abort(403)
@bp.route('/')
@login_required
def dashboard():
    guard(); runs=MigrationRun.query.order_by(MigrationRun.created_at.desc()).limit(50).all()
    completed={r.template_type for r in runs if r.status=='COMPLETED'}
    return render_template('legacy_migration.html',templates=TEMPLATES,runs=runs,completed=completed)
@bp.route('/template/<kind>')
@login_required
def download(kind):
    guard()
    if kind not in TEMPLATES: abort(404)
    buf=io.BytesIO(); template_workbook(kind).save(buf); buf.seek(0)
    return send_file(buf,as_attachment=True,download_name=TEMPLATES[kind]['file'],mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
@bp.route('/upload',methods=['POST'])
@login_required
def upload():
    guard(); kind=(request.form.get('template_type') or 'AUTO').upper(); file=request.files.get('file')
    if not file or not file.filename.lower().endswith('.xlsx'): flash('Choose an .xlsx workbook (official template or legacy export — AUTO analysis will discover it).', 'danger'); return redirect(url_for('.dashboard'))
    try:
        raw=file.read()
        if kind=='AUTO':
            run=analyze_upload(raw,file.filename,current_user.username)
            summary=json.loads(run.summary_json or '{}')
            flash(f"Workbook analysed: {summary.get('SOURCE ROWS FOUND',0)} data row(s) across {len(summary.get('SHEETS',[]))} sheet(s). Review the discovery report before importing.", 'success')
            return redirect(url_for('.preview',run_id=run.id))
        run,issues=validate_upload(kind,raw,file.filename,current_user.username)
        summary=json.loads(run.summary_json or '{}')
        if summary.get('Total Rows',0)==0:
            # Never silent: explain the zero instead of celebrating an empty success.
            flash(f"No rows could be mapped for {kind} from this workbook ({summary.get('SOURCE ROWS FOUND',0)} source rows were found but not mapped). {len(issues)} issue(s) listed in run #{run.id} preview.", 'warning')
        else:
            extra=f" {len(issues)} workbook note(s)." if issues else ""
            flash(f"Validation complete for run #{run.id}: {summary.get('MAPPED',0)} row(s) mapped from {summary.get('SOURCE ROWS FOUND',0)} found. Review the preview before importing.{extra}", 'success')
        return redirect(url_for('.preview',run_id=run.id))
    except ValueError as exc: flash(str(exc),'danger'); return redirect(url_for('.dashboard'))
@bp.route('/run/<int:run_id>')
@login_required
def preview(run_id):
    guard(); run=MigrationRun.query.get_or_404(run_id); rows=MigrationRow.query.filter_by(run_id=run.id).order_by(MigrationRow.source_sheet,MigrationRow.source_row).all()
    return render_template('legacy_migration_preview.html',run=run,rows=rows,summary=json.loads(run.summary_json or '{}'),templates=TEMPLATES)
@bp.route('/run/<int:run_id>/prepare/<kind>',methods=['POST'])
@login_required
def prepare(run_id,kind):
    """Wizard step: turn one detected entity of an ANALYSIS run into a real validation run."""
    guard()
    try:
        child,issues=rerun_prepare(run_id,kind,current_user.username)
        summary=json.loads(child.summary_json or '{}')
        flash(f"{kind} validation run #{child.id} created from the analysed workbook: {summary.get('MAPPED',0)} of {summary.get('SOURCE ROWS FOUND',0)} source rows mapped. Review before import.",'success')
        return redirect(url_for('.preview',run_id=child.id))
    except ValueError as exc: flash(str(exc),'danger'); return redirect(url_for('.preview',run_id=run_id))
@bp.route('/run/<int:run_id>/dry-run',methods=['POST'])
@login_required
def dry_run(run_id):
    guard(); run=MigrationRun.query.get_or_404(run_id)
    s=json.loads(run.summary_json or '{}')
    flash('Dry run is non-destructive — no production records were changed. Workbook rows found: '+str(s.get('SOURCE ROWS FOUND','?'))+' · mapped: '+str(s.get('MAPPED','?'))+' · ready: '+str(s.get('READY','?'))+' · warning: '+str(s.get('WARNING','?'))+' · invalid: '+str(s.get('INVALID','?'))+' · duplicate: '+str(s.get('EXACT_DUPLICATE','?'))+' · orphan: '+str(s.get('ORPHAN','?'))+' · blocked: '+str(s.get('BLOCKED','?'))+'. Full sheet-by-sheet accounting is shown below.','info')
    return redirect(url_for('.preview',run_id=run.id))
@bp.route('/run/<int:run_id>/import',methods=['POST'])
@login_required
def do_import(run_id):
    guard(); run=MigrationRun.query.get_or_404(run_id)
    try:
        flash(f'Import completed: {import_run(run)} records created; reconcile report is in the run summary.','success')
    except ValueError as exc: flash(str(exc),'warning')
    return redirect(url_for('.preview',run_id=run.id))
@bp.route('/run/<int:run_id>/errors.xlsx')
@login_required
def errors(run_id):
    guard(); run=MigrationRun.query.get_or_404(run_id); wb=Workbook(); ws=wb.active; ws.title='MIGRATION_ERRORS'; ws.append(['Source File','Sheet','Excel Row','Column','Problem','Suggested Action','Status'])
    summary=json.loads(run.summary_json or '{}')
    for e in summary.get('ISSUES',[]): ws.append([run.filename,e.get('sheet'),e.get('row'),e.get('column'),e.get('problem'),e.get('suggested_action'),e.get('status')])
    for row in run.rows:
        for e in json.loads(row.error_json or '[]'): ws.append([run.filename,e.get('sheet') or row.source_sheet,e.get('row') or row.source_row,e.get('column'),e.get('problem'),e.get('suggested_action'),e.get('status')])
    buf=io.BytesIO(); wb.save(buf); buf.seek(0); return send_file(buf,as_attachment=True,download_name=f'migration_run_{run.id}_errors.xlsx')
