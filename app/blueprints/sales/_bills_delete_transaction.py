"""bills — split from sales.py."""
from ._common import *  # noqa
from app.services.void_rebuild import hard_delete_transaction


@bp.route('/delete_transaction/<string:type>/<int:id>', methods=['POST'])
@login_required
def delete_transaction(type, id):
    """Permanently remove a transaction and reverse its downstream effects.

    Deletion here is intentionally **hard**: the row and its children are
    removed and stock / ledger / account balances are rebuilt by
    ``hard_delete_transaction``.  There is no soft "void" state for these
    entities and therefore no way back — the only trace left is the
    ``AuditLog`` row written below.  The UI must present this as a permanent
    delete, never as a reversible "void".
    """
    if not _user_can('can_manage_sales') and getattr(current_user, 'role', '') != 'admin':
        flash('Permission denied', 'danger')
        return redirect(request.referrer or url_for('index'))

    try:
        ok = hard_delete_transaction(type, id)
        if ok:
            db.session.add(AuditLog(
                user_id=getattr(current_user, 'id', None),
                action=f'transaction.delete.{type}',
                details=f'id={id}, reason={(request.form.get("reason") or "").strip()}'
            ))
            db.session.commit()
            flash(f'{type} permanently deleted', 'success')
        else:
            flash(f'{type} not found', 'warning')
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).exception('Hard delete failed')
        flash('Unable to delete: the record could not be deleted. Please try again.', 'danger')
    return redirect(request.referrer or url_for('index'))
