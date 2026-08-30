"""Data Center run history (exports, restores, legacy merges, snapshots)."""
from .__base import *  # noqa
from .helpers import *  # noqa


class DataTransferRun(db.Model):
    __tablename__ = 'data_transfer_run'

    id = db.Column(db.Integer, primary_key=True)
    #: export | restore | legacy | snapshot
    kind = db.Column(db.String(20), nullable=False, index=True)
    #: json | xlsx | db
    format = db.Column(db.String(10), nullable=False, default='json')
    filename = db.Column(db.String(255), nullable=True)
    file_sha256 = db.Column(db.String(64), nullable=True)
    format_version_in = db.Column(db.String(20), nullable=True)
    format_version_out = db.Column(db.String(20), nullable=True)
    #: ok | dry_run | aborted | error
    status = db.Column(db.String(20), nullable=False, default='ok', index=True)
    tables = db.Column(db.Integer, default=0)
    rows = db.Column(db.Integer, default=0)
    summary_json = db.Column(db.JSON)
    actor = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, nullable=False, index=True)

    def __repr__(self):
        return f'<DataTransferRun #{self.id} {self.kind}/{self.format} {self.status}>'
